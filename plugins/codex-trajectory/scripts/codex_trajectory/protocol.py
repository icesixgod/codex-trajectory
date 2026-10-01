"""MCP JSON-RPC dispatch and stdio transport."""

from __future__ import annotations

import json
import math
import queue
import sys
import threading
from typing import Any

from .json_support import strict_json_loads
from .projection import (
    SERVER_NAME,
    SERVER_VERSION,
    UI_URI,
    ui_html,
)
from .tools import call_tool, tool_definitions

SUPPORTED_PROTOCOL_VERSION = "2025-06-18"
MAX_RPC_LINE_BYTES = 8 * 1024 * 1024
MAX_RPC_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_RPC_IDENTIFIER_LENGTH = 256
MAX_PENDING_REQUESTS = 8
_WRITE_LOCK = threading.Lock()
LEGACY_UI_URI = "ui://codex-trajectory/trajectory-v1.html"

_Inbound = tuple[str, Any]


class JsonRpcError(ValueError):
    """A public JSON-RPC failure with a protocol-defined error code."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


class _ResponseTooLarge(Exception):
    """An outbound JSON-RPC message exceeded the fixed wire budget."""


class _CancellationState:
    """Thread-safe cancellation markers shared by the reader and dispatcher."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._outstanding_ids: set[Any] = set()
        self._cancelled_ids: set[Any] = set()

    def register(self, request_id: Any) -> bool:
        with self._lock:
            if request_id in self._outstanding_ids:
                return False
            self._outstanding_ids.add(request_id)
            return True

    def cancel(self, request_id: Any) -> None:
        with self._lock:
            if request_id in self._outstanding_ids:
                self._cancelled_ids.add(request_id)

    def consume(self, request_id: Any) -> bool:
        with self._lock:
            if request_id not in self._cancelled_ids:
                return False
            self._cancelled_ids.remove(request_id)
            return True

    def finish(self, request_id: Any) -> None:
        with self._lock:
            self._outstanding_ids.discard(request_id)
            self._cancelled_ids.discard(request_id)


def _validate_initialize_params(values: dict[str, Any]) -> str:
    requested = values.get("protocolVersion")
    if not isinstance(requested, str):
        raise JsonRpcError(-32602, "Initialize protocolVersion must be a string.")

    capabilities = values.get("capabilities")
    if not isinstance(capabilities, dict):
        raise JsonRpcError(-32602, "Initialize capabilities must be an object.")
    for capability_name in ("experimental", "roots", "sampling", "elicitation"):
        if capability_name in capabilities and not isinstance(capabilities[capability_name], dict):
            raise JsonRpcError(
                -32602,
                f"Initialize capability {capability_name} must be an object.",
            )
    roots = capabilities.get("roots")
    if (
        isinstance(roots, dict)
        and "listChanged" in roots
        and not isinstance(roots["listChanged"], bool)
    ):
        raise JsonRpcError(-32602, "Initialize roots.listChanged must be a boolean.")

    client_info = values.get("clientInfo")
    if not isinstance(client_info, dict):
        raise JsonRpcError(-32602, "Initialize clientInfo must be an object.")
    for field in ("name", "version"):
        value = client_info.get(field)
        if not isinstance(value, str):
            raise JsonRpcError(
                -32602,
                f"Initialize clientInfo.{field} must be a string.",
            )
    title = client_info.get("title")
    if "title" in client_info and not isinstance(title, str):
        raise JsonRpcError(-32602, "Initialize clientInfo.title must be a string.")
    return requested


def handle(method: str, params: Any) -> dict[str, Any]:
    """Handle one MCP JSON-RPC request."""
    if params is not None and not isinstance(params, dict):
        raise JsonRpcError(-32602, "Request parameters must be an object.")
    values = params if isinstance(params, dict) else {}
    if method == "initialize":
        requested = _validate_initialize_params(values)
        protocol = (
            requested if requested == SUPPORTED_PROTOCOL_VERSION else SUPPORTED_PROTOCOL_VERSION
        )
        return {
            "protocolVersion": protocol,
            "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    if method == "ping":
        return {}
    if method.endswith("/list") and "cursor" in values:
        raise JsonRpcError(-32602, "This server has not issued a pagination cursor.")
    if method == "tools/list":
        return {"tools": tool_definitions()}
    if method == "tools/call":
        name = values.get("name")
        arguments = values.get("arguments", {})
        if not isinstance(name, str) or not name or len(name) > MAX_RPC_IDENTIFIER_LENGTH:
            raise JsonRpcError(-32602, "Tool name must be a non-empty string.")
        if name not in {tool["name"] for tool in tool_definitions()}:
            raise JsonRpcError(-32602, "Unknown tool name.")
        if not isinstance(arguments, dict):
            raise JsonRpcError(-32602, "Tool arguments must be an object.")
        metadata = values.get("_meta")
        if "_meta" in values and not isinstance(metadata, dict):
            raise JsonRpcError(-32602, "Tool metadata must be an object.")
        return call_tool(name, arguments, metadata=metadata)
    if method == "resources/list":
        return {
            "resources": [
                {
                    "uri": UI_URI,
                    "name": "Codex trajectory viewer",
                    "description": "Interactive task timing overview and event ledger.",
                    "mimeType": "text/html;profile=mcp-app",
                }
            ]
        }
    if method == "resources/read":
        resource_uri = values.get("uri")
        if not isinstance(resource_uri, str):
            raise JsonRpcError(-32602, "Resource URI must be a string.")
        if resource_uri not in {UI_URI, LEGACY_UI_URI}:
            raise JsonRpcError(-32002, "Resource not found.")
        return {
            "contents": [
                {
                    "uri": resource_uri,
                    "mimeType": "text/html;profile=mcp-app",
                    "text": ui_html(),
                    "_meta": {"ui": {"prefersBorder": True}},
                }
            ]
        }
    if method == "resources/templates/list":
        return {"resourceTemplates": []}
    if method == "prompts/list":
        return {"prompts": []}
    raise JsonRpcError(-32601, "Method not found.")


def send(message: dict[str, Any]) -> None:
    """Write one newline-delimited JSON-RPC message."""
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    encoded = bytearray()
    payload_budget = MAX_RPC_RESPONSE_BYTES - 1
    if payload_budget < 0:
        raise _ResponseTooLarge
    for chunk in encoder.iterencode(message):
        remaining = payload_budget - len(encoded)
        # UTF-8 uses at least one byte per code point. Reject an oversized chunk before
        # allocating a second, equally large bytes object.
        if len(chunk) > remaining:
            raise _ResponseTooLarge
        for start in range(0, len(chunk), 64 * 1024):
            chunk_bytes = chunk[start : start + 64 * 1024].encode(
                "utf-8", errors="backslashreplace"
            )
            if len(chunk_bytes) > payload_budget - len(encoded):
                raise _ResponseTooLarge
            encoded.extend(chunk_bytes)
    encoded.append(0x0A)
    try:
        with _WRITE_LOCK:
            sys.stdout.buffer.write(encoded)
            sys.stdout.buffer.flush()
    except BrokenPipeError as error:
        raise SystemExit(0) from error


def _send_error(request_id: Any, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def _send_bounded_error(request_id: Any, code: int, message: str) -> None:
    try:
        _send_error(request_id, code, message)
    except (_ResponseTooLarge, TypeError, ValueError, RecursionError):
        # A pathological request id must not make the error itself exceed the wire budget.
        _send_error(None, -32603, "Internal server error.")


def _valid_request_id(value: Any) -> bool:
    """Accept the finite String or Number identifiers allowed by MCP."""
    if value is None:
        return False
    if isinstance(value, str):
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if not isinstance(value, float):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def _cancelled_request_id(message: Any) -> tuple[bool, Any]:
    """Return a validated cancellation target from an MCP notification."""
    if (
        not isinstance(message, dict)
        or message.get("jsonrpc") != "2.0"
        or "id" in message
        or message.get("method") != "notifications/cancelled"
    ):
        return False, None
    params = message.get("params")
    if not isinstance(params, dict):
        return False, None
    request_id = params.get("requestId")
    if not _valid_request_id(request_id):
        return False, None
    reason = params.get("reason")
    if "reason" in params and not isinstance(reason, str):
        return False, None
    return True, request_id


def _read_inbound() -> _Inbound | None:
    encoded_line = sys.stdin.buffer.readline(MAX_RPC_LINE_BYTES + 1)
    if not encoded_line:
        return None
    if len(encoded_line) > MAX_RPC_LINE_BYTES:
        while encoded_line and not encoded_line.endswith(b"\n"):
            encoded_line = sys.stdin.buffer.readline(MAX_RPC_LINE_BYTES + 1)
        return "error", (-32700, "Parse error.")
    try:
        return "message", strict_json_loads(encoded_line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return "error", (-32700, "Parse error.")


def _dispatch(message: dict[str, Any], cancellations: _CancellationState) -> bool:
    """Dispatch one request; keep wire failures bounded and internal errors private."""
    request_id = message["id"]
    try:
        if cancellations.consume(request_id):
            return False
        try:
            result = handle(message["method"], message.get("params"))
            response = {"jsonrpc": "2.0", "id": request_id, "result": result}
        except JsonRpcError as error:
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": error.code, "message": str(error)},
            }
        except Exception:
            response = {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32603, "message": "Internal server error."},
            }
        if cancellations.consume(request_id):
            return False
        try:
            send(response)
        except _ResponseTooLarge:
            _send_bounded_error(request_id, -32603, "Response exceeds the server size limit.")
            return False
        except (TypeError, ValueError, RecursionError):
            _send_bounded_error(request_id, -32603, "Internal server error.")
            return False
        return "result" in response
    finally:
        cancellations.finish(request_id)


def _work_loop(
    inbox: queue.Queue[dict[str, Any] | None], cancellations: _CancellationState
) -> None:
    disconnected = False
    while (message := inbox.get()) is not None:
        if not disconnected:
            try:
                _dispatch(message, cancellations)
            except SystemExit:
                disconnected = True


def main() -> None:
    """Read control traffic promptly; execute bounded queued business requests serially."""
    inbox: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=MAX_PENDING_REQUESTS)
    cancellations = _CancellationState()
    worker = threading.Thread(
        target=_work_loop,
        args=(inbox, cancellations),
        name="codex-trajectory-mcp-worker",
        daemon=True,
    )
    worker.start()
    phase = "new"
    try:
        while (inbound := _read_inbound()) is not None:
            if inbound[0] == "error":
                code, error_message = inbound[1]
                _send_bounded_error(None, code, error_message)
                continue
            message = inbound[1]
            if not isinstance(message, dict):
                _send_bounded_error(None, -32600, "Invalid Request.")
                continue
            has_id = "id" in message
            request_id = message.get("id")
            method = message.get("method")
            valid_id = not has_id or _valid_request_id(request_id)
            if (
                message.get("jsonrpc") != "2.0"
                or not valid_id
                or not isinstance(method, str)
                or not method
                or len(method) > MAX_RPC_IDENTIFIER_LENGTH
            ):
                _send_bounded_error(
                    request_id if has_id and valid_id else None, -32600, "Invalid Request."
                )
                continue
            valid_params = "params" not in message or isinstance(message["params"], dict)
            if not has_id:
                # Request-only methods must never execute as unconfirmable notifications.
                if not valid_params:
                    continue
                if method == "notifications/initialized" and phase == "initializing":
                    phase = "ready"
                is_cancel, target = _cancelled_request_id(message)
                if is_cancel and phase == "ready":
                    cancellations.cancel(target)
                continue
            if not valid_params:
                _send_bounded_error(request_id, -32602, "Request parameters must be an object.")
                continue
            if method == "initialize":
                if phase != "new":
                    _send_bounded_error(request_id, -32600, "Already initialized.")
                elif _dispatch(message, cancellations):
                    phase = "initializing"
                continue
            if method == "ping":
                if cancellations.register(request_id):
                    _dispatch(message, cancellations)
                else:
                    _send_bounded_error(request_id, -32600, "Request ID is already in flight.")
                continue
            if phase != "ready":
                _send_bounded_error(request_id, -32002, "Server is not initialized.")
                continue
            if not cancellations.register(request_id):
                _send_bounded_error(request_id, -32600, "Request ID is already in flight.")
                continue
            try:
                inbox.put_nowait(message)
            except queue.Full:
                cancellations.finish(request_id)
                _send_bounded_error(
                    request_id, -32000, "Server request queue is full; retry later."
                )
    finally:
        inbox.put(None)
        worker.join()


__all__ = ["handle", "main"]

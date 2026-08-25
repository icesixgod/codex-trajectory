"""MCP JSON-RPC dispatch and stdio transport."""

from __future__ import annotations

import json
import math
import queue
import sys
import threading
from contextlib import suppress
from typing import Any

from .json_support import strict_json_loads
from .projection import (
    SERVER_NAME,
    SERVER_VERSION,
    UI_URI,
    call_tool,
    tool_definitions,
    ui_html,
)

SUPPORTED_PROTOCOL_VERSION = "2025-06-18"
MAX_RPC_LINE_BYTES = 8 * 1024 * 1024
MAX_RPC_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_RPC_IDENTIFIER_LENGTH = 256
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

    def register(self, request_id: Any) -> None:
        with self._lock:
            self._outstanding_ids.add(request_id)

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
    if title is not None and not isinstance(title, str):
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
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    if method == "ping":
        return {}
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
        return call_tool(name, arguments)
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
    if reason is not None and not isinstance(reason, str):
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


def _read_loop(inbox: queue.Queue[_Inbound | None], cancellations: _CancellationState) -> None:
    try:
        while True:
            inbound = _read_inbound()
            if inbound is None:
                break
            if inbound[0] == "message":
                message = inbound[1]
                is_cancellation, request_id = _cancelled_request_id(message)
                if is_cancellation:
                    cancellations.cancel(request_id)
                    continue
                if (
                    isinstance(message, dict)
                    and "id" in message
                    and _valid_request_id(message.get("id"))
                ):
                    cancellations.register(message["id"])
            inbox.put(inbound)
    except Exception:
        pass
    finally:
        inbox.put(None)


def main() -> None:
    """Run the stdio MCP loop."""
    # One queued message bounds read-ahead memory while still allowing the reader to
    # observe a cancellation notification during a long-running request.
    inbox: queue.Queue[_Inbound | None] = queue.Queue(maxsize=1)
    cancellations = _CancellationState()
    reader = threading.Thread(
        target=_read_loop,
        args=(inbox, cancellations),
        name="codex-trajectory-mcp-reader",
        daemon=True,
    )
    reader.start()
    while True:
        inbound = inbox.get()
        if inbound is None:
            break
        if inbound[0] == "error":
            code, message = inbound[1]
            _send_bounded_error(None, code, message)
            continue
        message = inbound[1]
        if not isinstance(message, dict):
            _send_bounded_error(None, -32600, "Invalid Request.")
            continue
        has_request_id = "id" in message
        request_id = message.get("id")
        method = message.get("method")
        valid_id = not has_request_id or _valid_request_id(request_id)
        if (
            message.get("jsonrpc") != "2.0"
            or not valid_id
            or not isinstance(method, str)
            or not method
            or len(method) > MAX_RPC_IDENTIFIER_LENGTH
        ):
            response_id = request_id if has_request_id and valid_id else None
            _send_bounded_error(response_id, -32600, "Invalid Request.")
            if has_request_id and valid_id:
                cancellations.finish(request_id)
            continue
        if not has_request_id:
            # JSON-RPC notifications never receive an error response and must not stop the server.
            with suppress(Exception):
                handle(method, message.get("params"))
            continue
        if cancellations.consume(request_id):
            cancellations.finish(request_id)
            continue
        try:
            try:
                result = handle(method, message.get("params"))
                response = {"jsonrpc": "2.0", "id": request_id, "result": result}
            except JsonRpcError as error:
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": error.code, "message": str(error)},
                }
            except ValueError:
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32602, "message": "Invalid params."},
                }
            except Exception:  # Never expose local paths or implementation details on the wire.
                response = {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32603, "message": "Internal server error."},
                }
            if cancellations.consume(request_id):
                continue
            try:
                send(response)
            except _ResponseTooLarge:
                if not cancellations.consume(request_id):
                    _send_bounded_error(
                        request_id,
                        -32603,
                        "Response exceeds the server size limit.",
                    )
            except (TypeError, ValueError, RecursionError):
                _send_bounded_error(request_id, -32603, "Internal server error.")
        finally:
            cancellations.finish(request_id)
    reader.join()


__all__ = ["handle", "main"]

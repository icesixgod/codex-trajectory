"""MCP JSON-RPC dispatch and stdio transport."""

from __future__ import annotations

import json
import math
import sys
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
MAX_RPC_IDENTIFIER_LENGTH = 256


class JsonRpcError(ValueError):
    """A public JSON-RPC failure with a protocol-defined error code."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code


def handle(method: str, params: Any) -> dict[str, Any]:
    """Handle one MCP JSON-RPC request."""
    if params is not None and not isinstance(params, dict):
        raise JsonRpcError(-32602, "Request parameters must be an object.")
    values = params if isinstance(params, dict) else {}
    if method == "initialize":
        requested = values.get("protocolVersion")
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
        if values.get("uri") != UI_URI:
            raise JsonRpcError(-32602, "Unknown resource URI.")
        return {
            "contents": [
                {
                    "uri": UI_URI,
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
    if method == "logging/setLevel":
        return {}
    raise JsonRpcError(-32601, "Method not found.")


def send(message: dict[str, Any]) -> None:
    """Write one newline-delimited JSON-RPC message."""
    serialized = json.dumps(message, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    try:
        encoded = (serialized + "\n").encode("utf-8")
    except UnicodeEncodeError:
        encoded = (
            json.dumps(message, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n"
        ).encode("ascii")
    try:
        sys.stdout.buffer.write(encoded)
        sys.stdout.buffer.flush()
    except BrokenPipeError as error:
        raise SystemExit(0) from error


def _send_error(request_id: Any, code: int, message: str) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


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


def main() -> None:
    """Run the stdio MCP loop."""
    while True:
        encoded_line = sys.stdin.buffer.readline(MAX_RPC_LINE_BYTES + 1)
        if not encoded_line:
            break
        if len(encoded_line) > MAX_RPC_LINE_BYTES:
            while encoded_line and not encoded_line.endswith(b"\n"):
                encoded_line = sys.stdin.buffer.readline(MAX_RPC_LINE_BYTES + 1)
            _send_error(None, -32700, "Parse error.")
            continue
        try:
            message = strict_json_loads(encoded_line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            _send_error(None, -32700, "Parse error.")
            continue
        if not isinstance(message, dict):
            _send_error(None, -32600, "Invalid Request.")
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
            _send_error(response_id, -32600, "Invalid Request.")
            continue
        if not has_request_id:
            # JSON-RPC notifications never receive an error response and must not stop the server.
            with suppress(Exception):
                handle(method, message.get("params"))
            continue
        try:
            result = handle(method, message.get("params"))
            send({"jsonrpc": "2.0", "id": request_id, "result": result})
        except JsonRpcError as error:
            _send_error(request_id, error.code, str(error))
        except ValueError as error:
            _send_error(request_id, -32602, str(error))
        except Exception:  # Never expose local paths or implementation details on the wire.
            _send_error(request_id, -32603, "Internal server error.")


__all__ = ["handle", "main"]

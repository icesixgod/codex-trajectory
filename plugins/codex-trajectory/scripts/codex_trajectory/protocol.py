"""MCP JSON-RPC dispatch and stdio transport."""

from __future__ import annotations

import json
import sys
from typing import Any

from .projection import (
    SERVER_NAME,
    SERVER_VERSION,
    UI_URI,
    call_tool,
    tool_definitions,
    ui_html,
)


def handle(method: str, params: Any) -> dict[str, Any]:
    """Handle one MCP JSON-RPC request."""
    values = params if isinstance(params, dict) else {}
    if method == "initialize":
        requested = values.get("protocolVersion")
        protocol = requested if isinstance(requested, str) else "2025-06-18"
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
        return call_tool(str(values.get("name") or ""), values.get("arguments"))
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
            raise ValueError(f"Unknown resource {values.get('uri')!r}.")
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
    raise ValueError(f"Unsupported MCP method {method!r}.")


def send(message: dict[str, Any]) -> None:
    """Write one newline-delimited JSON-RPC message."""
    encoded = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def main() -> None:
    """Run the stdio MCP loop."""
    for encoded_line in sys.stdin.buffer:
        try:
            message = json.loads(encoded_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(message, dict):
            continue
        request_id = message.get("id")
        method = message.get("method")
        if request_id is None or not isinstance(method, str):
            continue
        try:
            result = handle(method, message.get("params"))
            send({"jsonrpc": "2.0", "id": request_id, "result": result})
        except Exception as error:  # MCP converts handler failures into JSON-RPC errors.
            send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32603, "message": str(error)},
                }
            )


__all__ = ["handle", "main"]

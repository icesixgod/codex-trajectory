"""MCP protocol and input-validation tests."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from codex_trajectory.projection import UI_URI, call_tool, tool_definitions
from codex_trajectory.protocol import handle


def test_tool_definitions_are_read_only_and_expose_detail_level() -> None:
    tools = tool_definitions()
    assert [tool["name"] for tool in tools] == [
        "list_codex_sessions",
        "get_codex_trajectory",
        "show_codex_trajectory",
    ]
    assert all(tool["annotations"]["readOnlyHint"] for tool in tools)
    assert tools[1]["inputSchema"]["properties"]["detailLevel"]["default"] == "summary"
    assert tools[2]["_meta"]["ui"]["resourceUri"] == UI_URI


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        ("list_codex_sessions", {"limit": 0}, "between 1 and 100"),
        ("list_codex_sessions", {"limit": True}, "integer"),
        ("list_codex_sessions", {"query": 1}, "string"),
        ("list_codex_sessions", {"includeArchived": "yes"}, "boolean"),
        ("list_codex_sessions", {"extra": 1}, "Unknown argument"),
        ("get_codex_trajectory", {"maxRecords": 49}, "between 50 and 1000"),
        ("get_codex_trajectory", {"maxRecords": True}, "integer"),
        ("get_codex_trajectory", {"sessionId": 4}, "string"),
        ("get_codex_trajectory", {"includeArchived": 1}, "boolean"),
        ("get_codex_trajectory", {"detailLevel": "verbose"}, "detailLevel"),
        ("unknown", {}, "Unknown tool"),
    ],
)
def test_tool_validation(name: str, arguments: dict[str, object], message: str) -> None:
    result = call_tool(name, arguments)
    assert result["isError"] is True
    assert message in result["content"][0]["text"]


def test_protocol_methods_and_resource(codex_home: Path) -> None:
    initialized = handle("initialize", {"protocolVersion": "2025-06-18"})
    assert initialized["serverInfo"]["version"] == "0.1.0"
    assert handle("ping", {}) == {}
    assert len(handle("tools/list", {})["tools"]) == 3
    assert handle("resources/list", {})["resources"][0]["uri"] == UI_URI
    resource = handle("resources/read", {"uri": UI_URI})["contents"][0]
    assert resource["mimeType"] == "text/html;profile=mcp-app"
    assert "Safe summary" in resource["text"]
    assert handle("resources/templates/list", {}) == {"resourceTemplates": []}
    assert handle("prompts/list", {}) == {"prompts": []}
    assert handle("logging/setLevel", {}) == {}
    listed = handle("tools/call", {"name": "list_codex_sessions", "arguments": {}})
    assert listed["structuredContent"]["count"] == 1
    trajectory = handle(
        "tools/call",
        {
            "name": "get_codex_trajectory",
            "arguments": {"sessionId": "session-alpha", "detailLevel": "summary"},
        },
    )
    shown = handle(
        "tools/call",
        {
            "name": "show_codex_trajectory",
            "arguments": {"sessionId": "session-alpha", "detailLevel": "full"},
        },
    )
    assert trajectory["structuredContent"]["detailLevel"] == "summary"
    assert shown["structuredContent"]["detailLevel"] == "full"
    assert shown["_meta"]["ui"]["resourceUri"] == UI_URI
    with pytest.raises(ValueError, match="Unknown resource"):
        handle("resources/read", {"uri": "ui://unknown"})
    with pytest.raises(ValueError, match="Unsupported"):
        handle("unknown/method", {})


def test_stdio_server_handshake_and_unicode(codex_home: Path) -> None:
    script = Path(__file__).parents[1] / "scripts" / "codex_trajectory_mcp.py"
    requests = [
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {
            "jsonrpc": "2.0",
            "id": "二",
            "method": "tools/call",
            "params": {"name": "list_codex_sessions", "arguments": {"query": "Inspect"}},
        },
        {"jsonrpc": "2.0", "id": 3, "method": "missing", "params": {}},
    ]
    process = subprocess.run(
        [sys.executable, str(script)],
        input=(
            "not-json\n[]\n"
            + json.dumps({"jsonrpc": "2.0", "id": 0})
            + "\n"
            + "\n".join(json.dumps(item, ensure_ascii=False) for item in requests)
            + "\n"
        ),
        capture_output=True,
        text=True,
        check=True,
    )
    responses = [json.loads(line) for line in process.stdout.splitlines()]
    assert len(responses) == 3
    assert responses[0]["result"]["serverInfo"]["name"] == "codex-trajectory"
    assert responses[1]["id"] == "二"
    assert responses[1]["result"]["structuredContent"]["count"] == 1
    assert responses[2]["error"]["code"] == -32603

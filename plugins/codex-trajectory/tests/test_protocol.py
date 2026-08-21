"""MCP protocol and input-validation tests."""

from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from codex_trajectory import __version__, projection, protocol
from codex_trajectory.json_support import MAX_JSON_NESTING_DEPTH
from codex_trajectory.projection import UI_URI, call_tool, tool_definitions
from codex_trajectory.protocol import JsonRpcError, handle


def test_tool_definitions_scope_reads_and_private_cdp_setting() -> None:
    tools = tool_definitions()
    assert [tool["name"] for tool in tools] == [
        "list_codex_sessions",
        "get_codex_trajectory",
        "show_codex_trajectory",
        "get_codex_trajectory_update",
        "get_codex_toolbar_injection_status",
        "set_codex_toolbar_injection",
        "request_codex_task_stop",
    ]
    assert all(tool["annotations"]["readOnlyHint"] for tool in tools[:5])
    assert all(
        tool["annotations"]
        == {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
        for tool in tools[5:]
    )
    assert tools[1]["inputSchema"]["properties"]["detailLevel"]["default"] == "summary"
    assert tools[1]["inputSchema"]["properties"]["beforeRecord"]["minimum"] == 1
    assert tools[2]["_meta"]["ui"]["resourceUri"] == UI_URI
    assert tools[3]["_meta"]["ui"]["visibility"] == ["app"]
    assert tools[3]["_meta"]["openai/visibility"] == "private"
    assert tools[3]["inputSchema"]["properties"]["revision"]["pattern"] == "^[0-9a-f]{64}$"
    assert tools[4]["_meta"]["ui"]["visibility"] == ["app"]
    assert tools[5]["_meta"]["openai/visibility"] == "private"
    assert tools[5]["inputSchema"]["required"] == ["enabled"]
    assert tools[6]["_meta"]["ui"]["visibility"] == ["app"]
    assert tools[6]["_meta"]["openai/visibility"] == "private"
    assert tools[6]["inputSchema"]["required"] == [
        "sessionId",
        "source",
        "threshold",
        "language",
    ]


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
        ("get_codex_trajectory", {"beforeRecord": True}, "integer"),
        ("get_codex_trajectory", {"beforeRecord": 0}, "between 1 and"),
        ("get_codex_trajectory", {"beforeRecord": 2**53}, "between 1 and"),
        ("get_codex_trajectory", {"sessionId": 4}, "string"),
        ("get_codex_trajectory", {"includeArchived": 1}, "boolean"),
        ("get_codex_trajectory", {"detailLevel": "verbose"}, "detailLevel"),
        ("get_codex_trajectory_update", {"sessionId": 4}, "string"),
        ("get_codex_trajectory_update", {"revision": 1}, "string"),
        ("get_codex_trajectory_update", {"revision": "bad"}, "SHA-256"),
        ("get_codex_trajectory_update", {"revision": "A" * 64}, "SHA-256"),
        ("get_codex_trajectory_update", {"includeArchived": 1}, "boolean"),
        ("get_codex_trajectory_update", {"extra": 1}, "Unknown argument"),
        ("get_codex_toolbar_injection_status", {"extra": 1}, "Unknown argument"),
        ("set_codex_toolbar_injection", {}, "enabled"),
        ("set_codex_toolbar_injection", {"enabled": 1}, "boolean"),
        ("set_codex_toolbar_injection", {"enabled": True, "port": True}, "integer"),
        ("set_codex_toolbar_injection", {"enabled": True, "port": 1023}, "between"),
        ("set_codex_toolbar_injection", {"enabled": False, "extra": 1}, "Unknown argument"),
        (
            "request_codex_task_stop",
            {"source": "manual", "threshold": 10, "language": "en"},
            "sessionId",
        ),
        (
            "request_codex_task_stop",
            {"sessionId": "../task", "source": "manual", "threshold": 10, "language": "en"},
            "sessionId",
        ),
        (
            "request_codex_task_stop",
            {
                "sessionId": "session-alpha",
                "turnId": "bad/turn",
                "source": "manual",
                "threshold": 10,
                "language": "en",
            },
            "turnId",
        ),
        (
            "request_codex_task_stop",
            {"sessionId": "session-alpha", "source": "later", "threshold": 10, "language": "en"},
            "source",
        ),
        (
            "request_codex_task_stop",
            {"sessionId": "session-alpha", "source": "auto", "threshold": True, "language": "en"},
            "integer",
        ),
        (
            "request_codex_task_stop",
            {"sessionId": "session-alpha", "source": "auto", "threshold": 101, "language": "en"},
            "between",
        ),
        (
            "request_codex_task_stop",
            {"sessionId": "session-alpha", "source": "auto", "threshold": 10, "language": "fr"},
            "language",
        ),
        (
            "request_codex_task_stop",
            {
                "sessionId": "session-alpha",
                "source": "auto",
                "threshold": 10,
                "language": "en",
                "prompt": "stop",
            },
            "Unknown argument",
        ),
        ("unknown", {}, "Unknown tool"),
    ],
)
def test_tool_validation(name: str, arguments: dict[str, object], message: str) -> None:
    result = call_tool(name, arguments)
    assert result["isError"] is True
    assert message in result["content"][0]["text"]


def test_tool_filesystem_errors_do_not_expose_local_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "sessions" / "secret-rollout.jsonl"

    def fail_to_list(*args: object, **kwargs: object) -> list[dict[str, object]]:
        raise OSError(13, "Permission denied", secret)

    monkeypatch.setattr(projection, "list_session_overviews", fail_to_list)

    result = call_tool("list_codex_sessions", {})
    message = result["content"][0]["text"]

    assert result["isError"] is True
    assert message == "Could not read local Codex task data."
    assert str(secret) not in message


def test_protocol_methods_and_resource(codex_home: Path) -> None:
    initialized = handle("initialize", {"protocolVersion": "2025-06-18"})
    assert initialized["serverInfo"]["version"] == __version__
    negotiated = handle("initialize", {"protocolVersion": "2099-01-01"})
    assert negotiated["protocolVersion"] == "2025-06-18"
    assert handle("ping", {}) == {}
    assert len(handle("tools/list", {})["tools"]) == 7
    assert handle("resources/list", {})["resources"][0]["uri"] == UI_URI
    resource = handle("resources/read", {"uri": UI_URI})["contents"][0]
    assert resource["mimeType"] == "text/html;profile=mcp-app"
    assert "Safe summary" in resource["text"]
    assert "__WHALE_MINING_SPRITE_DATA_URI__" not in resource["text"]
    assert "data:image/png;base64," in resource["text"]
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
    earlier = handle(
        "tools/call",
        {
            "name": "get_codex_trajectory",
            "arguments": {
                "sessionId": "session-alpha",
                "maxRecords": 50,
                "beforeRecord": 5,
            },
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
    assert trajectory["structuredContent"]["pagination"]["lastRecord"] == 9
    assert [record["index"] for record in earlier["structuredContent"]["records"]] == [1, 2, 3, 4]
    assert earlier["structuredContent"]["pagination"]["laterRecords"] == 5
    assert shown["structuredContent"]["detailLevel"] == "full"
    assert shown["_meta"]["ui"]["resourceUri"] == UI_URI
    live = handle(
        "tools/call",
        {
            "name": "get_codex_trajectory_update",
            "arguments": {"sessionId": "session-alpha"},
        },
    )
    update = live["structuredContent"]
    assert update["unchanged"] is False
    assert len(update["revision"]) == 64
    assert update["trajectory"]["detailLevel"] == "summary"
    assert "recentSessions" not in update["trajectory"]
    unchanged = handle(
        "tools/call",
        {
            "name": "get_codex_trajectory_update",
            "arguments": {
                "sessionId": "session-alpha",
                "revision": update["revision"],
            },
        },
    )["structuredContent"]
    assert unchanged == {
        "schemaVersion": 1,
        "unchanged": True,
        "revision": update["revision"],
    }
    with pytest.raises(ValueError, match="Unknown resource"):
        handle("resources/read", {"uri": "ui://unknown"})
    with pytest.raises(ValueError, match="Method not found"):
        handle("unknown/method", {})
    with pytest.raises(JsonRpcError, match="parameters") as invalid_params:
        handle("tools/list", [])
    assert invalid_params.value.code == -32602
    with pytest.raises(JsonRpcError, match="arguments") as invalid_arguments:
        handle("tools/call", {"name": "list_codex_sessions", "arguments": []})
    assert invalid_arguments.value.code == -32602
    for invalid_name in ("", "x" * 257):
        with pytest.raises(JsonRpcError, match="non-empty"):
            handle("tools/call", {"name": invalid_name, "arguments": {}})
    with pytest.raises(JsonRpcError, match="Unknown tool"):
        handle("tools/call", {"name": "missing", "arguments": {}})


def test_private_cdp_toolbar_tools_report_and_update_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = {
        "schemaVersion": 1,
        "enabled": False,
        "port": 9222,
        "cdpAvailable": False,
        "daemonRunning": False,
        "connected": False,
        "injected": False,
        "viewerServing": False,
        "lastError": None,
    }
    monkeypatch.setattr(projection, "cdp_toolbar_status", lambda: current)
    configured: list[tuple[bool, int]] = []

    def configure(enabled: bool, port: int) -> dict[str, object]:
        configured.append((enabled, port))
        return {**current, "enabled": enabled, "port": port}

    monkeypatch.setattr(projection, "configure_cdp_toolbar", configure)
    status = call_tool("get_codex_toolbar_injection_status", {})
    assert status["structuredContent"] == current

    changed = call_tool(
        "set_codex_toolbar_injection",
        {"enabled": True, "port": 9333},
    )
    assert configured == [(True, 9333)]
    assert changed["structuredContent"]["enabled"] is True
    assert changed["structuredContent"]["port"] == 9333
    assert changed["content"][0]["text"].startswith("Enabled")

    def fail(_enabled: bool, _port: int) -> dict[str, object]:
        raise OSError("private path")

    monkeypatch.setattr(projection, "configure_cdp_toolbar", fail)
    failed = call_tool("set_codex_toolbar_injection", {"enabled": False})
    assert failed["isError"] is True
    assert "private path" not in failed["content"][0]["text"]


def test_private_direct_stop_tool_returns_only_bounded_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, object]] = []

    def stop(arguments: dict[str, object]) -> dict[str, object]:
        requests.append(arguments)
        return {"sent": True}

    monkeypatch.setattr(projection, "request_direct_task_stop", stop)
    result = call_tool(
        "request_codex_task_stop",
        {
            "sessionId": "session-alpha",
            "turnId": "turn-2",
            "source": "auto",
            "threshold": 9,
            "language": "zh",
        },
    )
    assert result["structuredContent"] == {"sent": True}
    assert requests == [
        {
            "sessionId": "session-alpha",
            "turnId": "turn-2",
            "source": "auto",
            "threshold": 9,
            "language": "zh",
        }
    ]

    monkeypatch.setattr(projection, "request_direct_task_stop", lambda _args: {"sent": False})
    failed = call_tool(
        "request_codex_task_stop",
        {
            "sessionId": "session-alpha",
            "source": "manual",
            "threshold": 10,
            "language": "en",
        },
    )
    assert failed["isError"] is True


def test_live_update_reprojects_only_after_the_rollout_changes(codex_home: Path) -> None:
    first = call_tool(
        "get_codex_trajectory_update",
        {"sessionId": "session-alpha"},
    )["structuredContent"]
    rollout = codex_home / "sessions" / "2026" / "rollout-alpha.jsonl"
    with rollout.open("a", encoding="utf-8") as handle_stream:
        handle_stream.write(
            json.dumps(
                {
                    "timestamp": "2026-08-14T00:00:12.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "id": "message-live",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Live update"}],
                    },
                }
            )
            + "\n"
        )

    second = call_tool(
        "get_codex_trajectory_update",
        {"sessionId": "session-alpha", "revision": first["revision"]},
    )["structuredContent"]

    assert second["unchanged"] is False
    assert second["revision"] != first["revision"]
    assert second["trajectory"]["stats"]["records"] == 10
    assert second["trajectory"]["records"][-1]["summary"] == "Live update"


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
    assert len(responses) == 6
    assert responses[0]["error"]["code"] == -32700
    assert responses[1]["error"]["code"] == -32600
    assert responses[2]["error"]["code"] == -32600
    assert responses[3]["result"]["serverInfo"]["name"] == "codex-trajectory"
    assert responses[4]["id"] == "二"
    assert responses[4]["result"]["structuredContent"]["count"] == 1
    assert responses[5]["error"]["code"] == -32601


def test_stdio_rejects_oversized_nonstandard_and_invalid_notification_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        b"x" * 140
        + b"\n"
        + b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"value":NaN}}\n'
        + b'{"jsonrpc":"2.0","id":2,"id":3,"method":"ping"}\n'
        + b'{"jsonrpc":"2.0"}\n'
        + b'{"jsonrpc":"2.0","id":null,"method":"ping"}\n'
        + b'{"jsonrpc":"2.0","method":"ping"}\n'
    )
    output = io.BytesIO()
    monkeypatch.setattr(protocol, "MAX_RPC_LINE_BYTES", 128)
    monkeypatch.setattr(protocol.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(source)))
    monkeypatch.setattr(protocol.sys, "stdout", SimpleNamespace(buffer=output))

    protocol.main()

    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [response["error"]["code"] for response in responses] == [
        -32700,
        -32700,
        -32700,
        -32600,
        -32600,
    ]
    assert responses[-1]["id"] is None


def test_stdio_rejects_excessively_nested_json_without_stopping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    nested = b"[" * (MAX_JSON_NESTING_DEPTH + 1) + b"0" + b"]" * (MAX_JSON_NESTING_DEPTH + 1)
    source = (
        b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"value":'
        + nested
        + b"}}\n"
        + b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
    )
    output = io.BytesIO()
    monkeypatch.setattr(protocol.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(source)))
    monkeypatch.setattr(protocol.sys, "stdout", SimpleNamespace(buffer=output))

    protocol.main()

    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses[0]["id"] is None
    assert responses[0]["error"]["code"] == -32700
    assert responses[1] == {"jsonrpc": "2.0", "id": 2, "result": {}}


def test_stdio_accepts_fractional_json_rpc_ids_and_rejects_nonfinite_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        b'{"jsonrpc":"2.0","id":1.5,"method":"ping"}\n'
        b'{"jsonrpc":"2.0","id":1e400,"method":"ping"}\n'
        + b'{"jsonrpc":"2.0","id":'
        + b"9" * 257
        + b',"method":"ping"}\n'
    )
    output = io.BytesIO()
    monkeypatch.setattr(protocol.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(source)))
    monkeypatch.setattr(protocol.sys, "stdout", SimpleNamespace(buffer=output))

    protocol.main()

    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert responses[0] == {"jsonrpc": "2.0", "id": 1.5, "result": {}}
    assert responses[1]["id"] is None
    assert responses[1]["error"]["code"] == -32700
    assert responses[2]["id"] is None
    assert responses[2]["error"]["code"] == -32700


def test_stdio_preserves_escaped_lone_surrogate_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = b'{"jsonrpc":"2.0","id":"\\ud800","method":"ping"}\n'
    output = io.BytesIO()
    monkeypatch.setattr(protocol.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(source)))
    monkeypatch.setattr(protocol.sys, "stdout", SimpleNamespace(buffer=output))

    protocol.main()

    response = json.loads(output.getvalue())
    assert response == {"jsonrpc": "2.0", "id": chr(0xD800), "result": {}}


def test_stdio_sanitizes_value_and_internal_dispatch_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        b'{"jsonrpc":"2.0","id":1,"method":"value-error"}\n'
        b'{"jsonrpc":"2.0","id":2,"method":"internal-error"}\n'
        b'{"jsonrpc":"2.0","id":true,"method":"ping"}\n'
        b'{"jsonrpc":"2.0","id":{},"method":"ping"}\n'
    )
    output = io.BytesIO()

    def fail(method: str, params: object) -> dict[str, object]:
        if method == "value-error":
            raise ValueError("safe validation failure")
        raise RuntimeError("private implementation detail")

    monkeypatch.setattr(protocol, "handle", fail)
    monkeypatch.setattr(protocol.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(source)))
    monkeypatch.setattr(protocol.sys, "stdout", SimpleNamespace(buffer=output))

    protocol.main()

    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [response["error"]["code"] for response in responses] == [
        -32602,
        -32603,
        -32600,
        -32600,
    ]
    assert "private implementation detail" not in output.getvalue().decode()


def test_send_treats_a_broken_stdout_pipe_as_clean_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class BrokenPipe:
        def write(self, value: bytes) -> int:
            raise BrokenPipeError

        def flush(self) -> None:
            raise AssertionError("flush must not run after a broken write")

    monkeypatch.setattr(protocol.sys, "stdout", SimpleNamespace(buffer=BrokenPipe()))

    with pytest.raises(SystemExit) as shutdown:
        protocol.send({"jsonrpc": "2.0", "id": 1, "result": {}})

    assert shutdown.value.code == 0

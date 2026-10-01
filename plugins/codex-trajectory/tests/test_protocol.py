"""MCP protocol and input-validation tests."""

from __future__ import annotations

import io
import json
import queue
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from codex_trajectory import __version__, projection, protocol
from codex_trajectory.json_support import MAX_JSON_NESTING_DEPTH
from codex_trajectory.projection import UI_URI, call_tool, tool_definitions
from codex_trajectory.protocol import LEGACY_UI_URI, JsonRpcError, handle


def initialize_params(protocol_version: str = "2025-06-18") -> dict[str, object]:
    return {
        "protocolVersion": protocol_version,
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "1.0.0"},
    }


def initialization_lines() -> bytes:
    return (
        json.dumps(
            {"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": initialize_params()}
        ).encode()
        + b'\n{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
    )


def test_tool_definitions_scope_reads_and_native_panel() -> None:
    tools = {tool["name"]: tool for tool in tool_definitions()}
    assert set(tools) == {
        "list_codex_sessions",
        "get_codex_trajectory",
        "show_codex_trajectory",
        "open_codex_trajectory",
        "get_codex_trajectory_update",
        "get_codex_trajectory_preferences",
        "set_codex_trajectory_preferences",
    }
    assert tools["list_codex_sessions"]["annotations"]["readOnlyHint"] is False
    assert all(
        tool["annotations"]["readOnlyHint"]
        for name, tool in tools.items()
        if name not in {"list_codex_sessions", "set_codex_trajectory_preferences"}
    )
    assert all(tool["annotations"]["destructiveHint"] is False for tool in tools.values())
    assert (
        tools["get_codex_trajectory"]["inputSchema"]["properties"]["detailLevel"]["default"]
        == "summary"
    )
    assert (
        tools["get_codex_trajectory"]["inputSchema"]["properties"]["beforeRecord"]["minimum"] == 1
    )
    assert tools["show_codex_trajectory"]["_meta"]["ui"]["resourceUri"] == UI_URI
    assert tools["open_codex_trajectory"]["_meta"]["openai/ui"]["entrypoints"] == [
        {"type": "thread"}
    ]
    update = tools["get_codex_trajectory_update"]
    assert update["_meta"]["ui"]["visibility"] == ["app"]
    assert update["_meta"]["openai/visibility"] == "private"
    assert update["inputSchema"]["properties"]["revision"]["pattern"] == "^[0-9a-f]{64}$"


@pytest.mark.parametrize(
    ("name", "arguments", "message"),
    [
        ("list_codex_sessions", {"limit": 0}, "between 1 and 100"),
        ("list_codex_sessions", {"limit": True}, "integer"),
        ("list_codex_sessions", {"query": 1}, "string"),
        ("list_codex_sessions", {"query": "x" * 501}, "500 characters"),
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
    initialized = handle("initialize", initialize_params())
    assert initialized["serverInfo"]["version"] == __version__
    negotiated = handle("initialize", initialize_params("2099-01-01"))
    assert negotiated["protocolVersion"] == "2025-06-18"
    extended = initialize_params()
    extended["capabilities"] = {"futureCapability": {"enabled": True}}
    assert handle("initialize", extended)["protocolVersion"] == "2025-06-18"
    assert handle("ping", {}) == {}
    assert len(handle("tools/list", {})["tools"]) == 7
    listed_resources = handle("resources/list", {})["resources"]
    assert listed_resources[0]["uri"] == UI_URI
    assert LEGACY_UI_URI not in {item["uri"] for item in listed_resources}
    resource = handle("resources/read", {"uri": UI_URI})["contents"][0]
    legacy_resource = handle("resources/read", {"uri": LEGACY_UI_URI})["contents"][0]
    assert resource["uri"] == UI_URI
    assert legacy_resource["uri"] == LEGACY_UI_URI
    assert legacy_resource["text"] == resource["text"]
    assert resource["mimeType"] == "text/html;profile=mcp-app"
    assert "Safe summary" in resource["text"]
    assert "__WHALE_MINING_SPRITE_DATA_URI__" not in resource["text"]
    assert "data:image/png;base64," in resource["text"]
    assert handle("resources/templates/list", {}) == {"resourceTemplates": []}
    assert handle("prompts/list", {}) == {"prompts": []}
    with pytest.raises(JsonRpcError, match="Method not found") as logging_error:
        handle("logging/setLevel", {})
    assert logging_error.value.code == -32601
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
        "schemaVersion": 2,
        "unchanged": True,
        "revision": update["revision"],
        "quota": update["quota"],
    }
    with pytest.raises(JsonRpcError, match="Resource not found") as unknown_resource:
        handle("resources/read", {"uri": "ui://unknown"})
    assert unknown_resource.value.code == -32002
    with pytest.raises(JsonRpcError, match="Resource URI") as malformed_resource:
        handle("resources/read", {})
    assert malformed_resource.value.code == -32602
    with pytest.raises(JsonRpcError, match="Method not found") as unknown_method:
        handle("unknown/method", {})
    assert unknown_method.value.code == -32601
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


@pytest.mark.parametrize(
    "params",
    [
        {},
        {"protocolVersion": 1, "capabilities": {}, "clientInfo": {}},
        {"protocolVersion": "2025-06-18", "clientInfo": {}},
        {"protocolVersion": "2025-06-18", "capabilities": [], "clientInfo": {}},
        {"protocolVersion": "2025-06-18", "capabilities": {}},
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": []},
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"version": "1"},
        },
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "client"},
        },
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "client", "version": "1", "title": 1},
        },
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {"sampling": []},
            "clientInfo": {"name": "client", "version": "1"},
        },
        {
            "protocolVersion": "2025-06-18",
            "capabilities": {"roots": {"listChanged": 1}},
            "clientInfo": {"name": "client", "version": "1"},
        },
    ],
)
def test_initialize_rejects_malformed_client_metadata(params: dict[str, object]) -> None:
    with pytest.raises(JsonRpcError) as invalid:
        handle("initialize", params)

    assert invalid.value.code == -32602


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
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": initialize_params(),
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
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
        encoding="utf-8",
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
    source = initialization_lines() + (
        b'{"jsonrpc":"2.0","id":1,"method":"value-error"}\n'
        b'{"jsonrpc":"2.0","id":2,"method":"internal-error"}\n'
        b'{"jsonrpc":"2.0","id":true,"method":"ping"}\n'
        b'{"jsonrpc":"2.0","id":{},"method":"ping"}\n'
    )
    output = io.BytesIO()
    original_handle = protocol.handle

    def fail(method: str, params: object) -> dict[str, object]:
        if method == "initialize":
            return original_handle(method, params)
        if method == "value-error":
            raise ValueError("safe validation failure")
        raise RuntimeError("private implementation detail")

    monkeypatch.setattr(protocol, "handle", fail)
    monkeypatch.setattr(protocol.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(source)))
    monkeypatch.setattr(protocol.sys, "stdout", SimpleNamespace(buffer=output))

    protocol.main()

    responses = [json.loads(line) for line in output.getvalue().splitlines()]
    errors = [response for response in responses if "error" in response]
    assert sorted(response["error"]["code"] for response in errors) == [
        -32603,
        -32603,
        -32600,
        -32600,
    ]
    assert next(r for r in errors if r["id"] == 1)["error"]["message"] == "Internal server error."
    assert "safe validation failure" not in output.getvalue().decode()
    assert "private implementation detail" not in output.getvalue().decode()


def test_stdio_bounds_oversized_responses_and_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized_id = "i" * 400
    source = initialization_lines() + (
        b'{"jsonrpc":"2.0","id":1,"method":"large"}\n'
        + b'{"jsonrpc":"2.0","id":4,"method":"unicode-large"}\n'
        + b'{"jsonrpc":"2.0","id":5,"method":"nonfinite"}\n'
        + json.dumps({"jsonrpc": "2.0", "id": oversized_id, "method": "ping"}).encode()
        + b"\n"
        + b'{"jsonrpc":"2.0","id":2,"method":"ping"}\n'
    )
    output = io.BytesIO()
    original_handle = protocol.handle

    def large_result(method: str, params: object) -> dict[str, object]:
        if method == "large":
            return {"value": "private/path/" + "x" * 2_000}
        if method == "unicode-large":
            return {"value": "界" * 100}
        if method == "nonfinite":
            return {"value": float("nan")}
        return original_handle(method, params)

    monkeypatch.setattr(protocol, "MAX_RPC_RESPONSE_BYTES", 256)
    monkeypatch.setattr(protocol, "handle", large_result)
    monkeypatch.setattr(protocol.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(source)))
    monkeypatch.setattr(protocol.sys, "stdout", SimpleNamespace(buffer=output))

    protocol.main()

    wire_lines = output.getvalue().splitlines(keepends=True)
    by_id = {
        value["id"]: value for line in wire_lines if (value := json.loads(line))["id"] != "init"
    }
    responses = [by_id[key] for key in (1, 4, 5, None, 2)]
    assert all(len(line) <= 256 for line in wire_lines)
    assert responses[0] == {
        "jsonrpc": "2.0",
        "id": 1,
        "error": {
            "code": -32603,
            "message": "Response exceeds the server size limit.",
        },
    }
    assert responses[1] == {
        "jsonrpc": "2.0",
        "id": 4,
        "error": {
            "code": -32603,
            "message": "Response exceeds the server size limit.",
        },
    }
    assert responses[2] == {
        "jsonrpc": "2.0",
        "id": 5,
        "error": {"code": -32603, "message": "Internal server error."},
    }
    assert responses[3] == {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32603, "message": "Internal server error."},
    }
    assert responses[4] == {"jsonrpc": "2.0", "id": 2, "result": {}}
    assert "private/path" not in output.getvalue().decode()
    assert oversized_id not in output.getvalue().decode()
    assert "NaN" not in output.getvalue().decode()


def test_stdio_observes_midflight_cancellation_and_suppresses_the_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_started = threading.Event()
    release_request = threading.Event()
    active_cancellation_observed = threading.Event()
    queued_cancellation_observed = threading.Event()
    ping_sent = threading.Event()
    original_send = protocol.send

    def observed_send(message: dict[str, Any]) -> None:
        original_send(message)
        if message.get("id") == 3:
            ping_sent.set()

    monkeypatch.setattr(protocol, "send", observed_send)

    class ControlledInput:
        def __init__(self) -> None:
            self.lines: queue.Queue[bytes] = queue.Queue()
            self._cancellation_to_signal: threading.Event | None = None

        def readline(self, _limit: int) -> bytes:
            if self._cancellation_to_signal is not None:
                self._cancellation_to_signal.set()
                self._cancellation_to_signal = None
            line = self.lines.get(timeout=5)
            if b'"requestId":1' in line:
                self._cancellation_to_signal = active_cancellation_observed
            elif b'"requestId":2' in line:
                self._cancellation_to_signal = queued_cancellation_observed
            return line

    controlled_input = ControlledInput()
    output = io.BytesIO()
    original_handle = protocol.handle

    def blocking_handle(method: str, params: object) -> dict[str, object]:
        if method == "slow":
            request_started.set()
            if not release_request.wait(timeout=5):
                raise RuntimeError("test request was not released")
            return {"completed": True}
        if method == "queued":
            return {}
        return original_handle(method, params)

    monkeypatch.setattr(protocol, "handle", blocking_handle)
    monkeypatch.setattr(protocol.sys, "stdin", SimpleNamespace(buffer=controlled_input))
    monkeypatch.setattr(protocol.sys, "stdout", SimpleNamespace(buffer=output))
    failures: list[BaseException] = []

    def run_server() -> None:
        try:
            protocol.main()
        except BaseException as error:
            failures.append(error)

    server = threading.Thread(target=run_server)
    server.start()
    for line in initialization_lines().splitlines(keepends=True):
        controlled_input.lines.put(line)
    controlled_input.lines.put(b'{"jsonrpc":"2.0","id":1,"method":"slow"}\n')
    assert request_started.wait(timeout=2)
    controlled_input.lines.put(b'{"jsonrpc":"2.0","id":2,"method":"queued"}\n')
    controlled_input.lines.put(b'{"jsonrpc":"2.0","id":4,"method":"queued"}\n')
    controlled_input.lines.put(
        b'{"jsonrpc":"2.0","method":"notifications/cancelled",'
        b'"params":{"requestId":1,"reason":"no longer needed"}}\n'
    )
    assert active_cancellation_observed.wait(timeout=2)
    controlled_input.lines.put(
        b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":2}}\n'
    )
    assert queued_cancellation_observed.wait(timeout=2)
    controlled_input.lines.put(
        b'{"jsonrpc":"2.0","method":"notifications/cancelled","params":{"requestId":4}}\n'
    )
    controlled_input.lines.put(b'{"jsonrpc":"2.0","id":3,"method":"ping"}\n')
    assert ping_sent.wait(timeout=2), "Ping was blocked behind the slow business call"
    controlled_input.lines.put(b"")
    release_request.set()
    server.join(timeout=5)

    assert not server.is_alive()
    assert failures == []
    assert [
        value
        for line in output.getvalue().splitlines()
        if (value := json.loads(line))["id"] != "init"
    ] == [{"jsonrpc": "2.0", "id": 3, "result": {}}]


def test_stdio_ignores_cancellation_for_a_future_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = (
        b'{"jsonrpc":"2.0","method":"notifications/cancelled",'
        b'"params":{"requestId":7}}\n'
        b'{"jsonrpc":"2.0","id":7,"method":"ping"}\n'
    )
    output = io.BytesIO()
    monkeypatch.setattr(protocol.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(source)))
    monkeypatch.setattr(protocol.sys, "stdout", SimpleNamespace(buffer=output))

    protocol.main()

    assert json.loads(output.getvalue()) == {"jsonrpc": "2.0", "id": 7, "result": {}}


def test_full_business_queue_does_not_block_ping_or_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lines: queue.Queue[bytes] = queue.Queue()
    started, release, ping = threading.Event(), threading.Event(), threading.Event()
    output = io.BytesIO()
    original_handle, original_send = protocol.handle, protocol.send
    executed: list[str] = []

    def handle(method: str, params: Any) -> dict[str, Any]:
        if method == "slow":
            started.set()
            assert release.wait(timeout=5)
            return {}
        if method == "queued":
            executed.append(method)
            return {}
        return original_handle(method, params)

    def send(message: dict[str, Any]) -> None:
        original_send(message)
        if message.get("id") == 99:
            ping.set()

    monkeypatch.setattr(protocol, "MAX_PENDING_REQUESTS", 1)
    monkeypatch.setattr(protocol, "handle", handle)
    monkeypatch.setattr(protocol, "send", send)
    monkeypatch.setattr(
        protocol.sys,
        "stdin",
        SimpleNamespace(buffer=SimpleNamespace(readline=lambda _limit: lines.get(timeout=5))),
    )
    monkeypatch.setattr(protocol.sys, "stdout", SimpleNamespace(buffer=output))
    worker = threading.Thread(target=protocol.main, daemon=True)
    worker.start()
    try:
        for line in initialization_lines().splitlines(keepends=True):
            lines.put(line)
        lines.put(b'{"jsonrpc":"2.0","id":1,"method":"slow"}\n')
        assert started.wait(timeout=2)
        for request in (
            {"id": 2, "method": "queued"},
            {"id": 3, "method": "queued"},
            {"id": 1, "method": "queued"},
            {"method": "notifications/cancelled", "params": {"requestId": 1}},
            {"method": "notifications/cancelled", "params": {"requestId": 2}},
            {"id": 99, "method": "ping"},
        ):
            lines.put((json.dumps({"jsonrpc": "2.0", **request}) + "\n").encode())
        assert ping.wait(timeout=2)
    finally:
        lines.put(b"")
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    results = {
        item["id"]: item for line in output.getvalue().splitlines() if (item := json.loads(line))
    }
    assert results[3]["error"]["code"] == -32000
    assert results[1]["error"]["code"] == -32600
    assert results[99]["result"] == {}
    assert 2 not in results
    assert executed == []


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

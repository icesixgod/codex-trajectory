"""User-controlled large-log budgets and task/request isolation."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from codex_trajectory import projection, sessions
from codex_trajectory.tools import call_tool, tool_definitions
from conftest import rollout_events, write_rollout

TASK_ID = "10101010-1010-4010-8010-101010101010"


def open_task(task_id: str = TASK_ID) -> dict[str, Any]:
    return call_tool("open_codex_trajectory", {}, metadata={"thread_id": task_id})


def test_native_large_plain_log_reports_size_and_suggests_three_gb(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    path = write_rollout(
        tmp_path / "sessions" / f"rollout-large-{TASK_ID}.jsonl", rollout_events(TASK_ID)
    )
    with path.open("r+b") as handle:
        handle.truncate(2_835_303_913)

    result = open_task()
    assert result["structuredContent"] == {
        "viewerState": "read-limit-exceeded",
        "sessionId": TASK_ID,
        "readLimit": {
            "requiredBytes": 2_835_303_913,
            "currentBytes": 512 * 1024 * 1024,
            "suggestedBytes": 3_000_000_000,
            "maximumBytes": sessions.MAX_OPT_IN_READ_BYTES,
            "atLeast": False,
        },
    }
    assert result["_meta"]["ui"]["resourceUri"].startswith("ui://")
    serialized = json.dumps(result)
    for hidden in (str(path), "private system instructions", "secret-tool-input"):
        assert hidden not in serialized


@pytest.mark.parametrize("tool", ["get_codex_trajectory", "show_codex_trajectory"])
def test_opt_in_reads_task_without_changing_default_or_bypassing_cache(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    path = codex_home / "sessions" / "2026" / "rollout-alpha.jsonl"
    needed = path.stat().st_size
    monkeypatch.setattr(sessions, "MAX_JSONL_TOTAL_BYTES", needed - 1)
    args = {"sessionId": "session-alpha", "maxRecords": 50}

    refused = call_tool(tool, args)
    assert refused["isError"] is True
    assert refused["structuredContent"]["readLimit"]["requiredBytes"] == needed
    assert refused["structuredContent"]["sessionId"] == "session-alpha"
    enabled = call_tool(tool, dict(args, maxReadBytes=needed))
    assert "isError" not in enabled
    assert enabled["structuredContent"]["session"]["id"] == "session-alpha"
    assert enabled["structuredContent"]["detailLevel"] == "summary"
    assert "secret-tool-input" not in json.dumps(enabled)
    assert sessions.jsonl_byte_limit() == needed - 1

    # A cached projection made with permission must not bypass the default budget.
    assert call_tool(tool, args)["structuredContent"]["viewerState"] == "read-limit-exceeded"
    assert open_task("session-alpha")["structuredContent"]["viewerState"] == "read-limit-exceeded"

    # Refresh, history pages and live updates use the same explicit task budget.
    refreshed = call_tool(tool, dict(args, maxReadBytes=needed))
    assert refreshed["structuredContent"]["stats"] == enabled["structuredContent"]["stats"]
    earlier = call_tool(tool, dict(args, maxReadBytes=needed, beforeRecord=5))
    assert earlier["structuredContent"]["pagination"]["lastRecord"] == 4
    live = call_tool(
        "get_codex_trajectory_update", {"sessionId": "session-alpha", "maxReadBytes": needed}
    )
    assert live["structuredContent"]["trajectory"]["session"]["id"] == "session-alpha"


def test_compressed_log_retains_limit_error_and_supports_explicit_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    try:
        from compression import zstd
    except ImportError:
        import zstandard as zstd

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    raw = "".join(json.dumps(event) + "\n" for event in rollout_events(TASK_ID)).encode()
    compressed = tmp_path / "sessions" / f"rollout-compressed-{TASK_ID}.jsonl.zst"
    compressed.parent.mkdir()
    if hasattr(zstd, "compress"):
        compressed.write_bytes(zstd.compress(raw))
    else:
        compressed.write_bytes(zstd.ZstdCompressor().compress(raw))
    monkeypatch.setattr(sessions, "MAX_JSONL_TOTAL_BYTES", len(raw) - 1)

    native = open_task()
    # The decoder cannot verify metadata within this budget. Filename hints
    # support explicit public reads, but cannot bind the native caller.
    assert native["structuredContent"] == {
        "viewerState": "select-session",
        "reason": "task-unavailable",
    }
    refused = call_tool("get_codex_trajectory", {"sessionId": TASK_ID})
    assert refused["structuredContent"]["viewerState"] == "read-limit-exceeded"
    assert refused["structuredContent"]["sessionId"] == TASK_ID
    enabled = call_tool("get_codex_trajectory", {"sessionId": TASK_ID, "maxReadBytes": len(raw)})
    assert enabled["structuredContent"]["session"]["id"] == TASK_ID


@pytest.mark.parametrize("metadata_id", [TASK_ID, "20202020-2020-4020-8020-202020202020"])
def test_native_compressed_limit_requires_verified_metadata_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, metadata_id: str
) -> None:
    from test_sessions import _zstd_compress

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    raw = "".join(json.dumps(event) + "\n" for event in rollout_events(metadata_id)).encode()
    path = tmp_path / "sessions" / f"rollout-compressed-conflict-{TASK_ID}.jsonl.zst"
    path.parent.mkdir()
    path.write_bytes(_zstd_compress(raw))
    monkeypatch.setattr(sessions, "MAX_JSONL_TOTAL_BYTES", len(raw) - 1)
    assert open_task()["structuredContent"] == {
        "viewerState": "select-session",
        "reason": "task-unavailable",
    }
    # Explicit public filename alias resolution remains supported; it cannot
    # confer a verified native binding before the metadata has been read.
    public = call_tool("get_codex_trajectory", {"sessionId": TASK_ID, "maxReadBytes": len(raw)})
    assert public["structuredContent"]["session"]["id"] == metadata_id


@pytest.mark.parametrize("identifier", [TASK_ID, TASK_ID[:8]])
def test_native_oversized_alias_cannot_offer_retry_for_another_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, identifier: str
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    other_id = "20202020-2020-4020-8020-202020202020"
    path = write_rollout(
        tmp_path / "sessions" / f"rollout-2026-10-01T00-00-00-{other_id}_{TASK_ID}.jsonl",
        rollout_events(other_id),
    )
    monkeypatch.setattr(sessions, "MAX_JSONL_TOTAL_BYTES", path.stat().st_size - 1)
    assert open_task(identifier)["structuredContent"] == {
        "viewerState": "select-session",
        "reason": "task-unavailable",
    }
    assert open_task(other_id)["structuredContent"]["viewerState"] == "read-limit-exceeded"


@pytest.mark.parametrize("value", [True, 0, -1, 3.0, "3000000000", 64_000_000_001])
@pytest.mark.parametrize("tool", ["get_codex_trajectory", "get_codex_trajectory_update"])
def test_invalid_read_budgets_are_rejected(codex_home: Path, value: Any, tool: str) -> None:
    result = call_tool(tool, {"sessionId": "session-alpha", "maxReadBytes": value})
    assert result["isError"] is True
    assert "maxReadBytes" in result["content"][0]["text"]


@pytest.mark.parametrize("task", [None, "latest"])
@pytest.mark.parametrize("tool", ["get_codex_trajectory", "get_codex_trajectory_update"])
def test_larger_reads_require_selected_task(codex_home: Path, task: Any, tool: str) -> None:
    result = call_tool(tool, {"sessionId": task, "maxReadBytes": 3_000_000_000})
    assert result["isError"] is True
    assert "explicit sessionId" in result["content"][0]["text"]


def test_raising_byte_limit_preserves_line_and_path_checks(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "MAX_JSONL_LINES", 2)
    args = {"sessionId": "session-alpha", "maxReadBytes": 3_000_000_000}
    result = call_tool("get_codex_trajectory", args)
    assert result["isError"] is True
    assert "line limit" in result["content"][0]["text"]
    result = call_tool("get_codex_trajectory", dict(args, sessionId="../secret"))
    assert result["isError"] is True
    assert "filesystem path" in result["content"][0]["text"]


def test_read_limit_context_isolated_and_restored_after_errors() -> None:
    barrier = Barrier(2)

    def limit_in_thread(limit: int) -> int:
        with sessions.session_read_limit(limit):
            barrier.wait(timeout=5)
            return sessions.jsonl_byte_limit()

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(limit_in_thread, [1_000_000_000, 3_000_000_000])) == [
            1_000_000_000,
            3_000_000_000,
        ]
    with pytest.raises(RuntimeError), sessions.session_read_limit(3_000_000_000):
        raise RuntimeError("test failure")
    assert sessions.jsonl_byte_limit() == sessions.MAX_JSONL_TOTAL_BYTES


@pytest.mark.parametrize("unrelated_first", [True, False])
def test_task_budget_does_not_expand_unrelated_metadata_reads(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch, unrelated_first: bool
) -> None:
    unrelated = write_rollout(
        codex_home / "sessions" / "rollout-other.jsonl", rollout_events("other-task")
    )
    selected = codex_home / "sessions" / "2026" / "rollout-alpha.jsonl"
    # Discovery uses mtime, which can tie on Windows. Exercise both scan orders
    # explicitly instead of assuming the file just created will appear first.
    paths = [unrelated, selected] if unrelated_first else [selected, unrelated]
    monkeypatch.setattr(projection, "session_files", lambda include_archived: paths)
    original = projection.first_session_metadata
    limits: list[int] = []

    def metadata(path: Path) -> dict[str, Any]:
        if path == unrelated:
            limits.append(sessions.jsonl_byte_limit())
        return original(path)

    monkeypatch.setattr(projection, "first_session_metadata", metadata)
    result = call_tool(
        "get_codex_trajectory", {"sessionId": "session-alpha", "maxReadBytes": 3_000_000_000}
    )
    assert result["structuredContent"]["session"]["id"] == "session-alpha"
    assert limits == ([sessions.MAX_JSONL_TOTAL_BYTES] if unrelated_first else [])


def test_read_limit_schema_exposed_only_on_selected_task_reads() -> None:
    definitions = {tool["name"]: tool for tool in tool_definitions()}
    for tool in ("get_codex_trajectory", "show_codex_trajectory", "get_codex_trajectory_update"):
        field = definitions[tool]["inputSchema"]["properties"]["maxReadBytes"]
        assert field["default"] == 512 * 1024 * 1024
        assert field["maximum"] == sessions.MAX_OPT_IN_READ_BYTES
    assert "maxReadBytes" not in definitions["open_codex_trajectory"]["inputSchema"]["properties"]
    assert "maxReadBytes" not in definitions["list_codex_sessions"]["inputSchema"]["properties"]

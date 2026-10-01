"""Cross-layer regressions discovered during the repository audit."""

from __future__ import annotations

import importlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from codex_trajectory import filesystem, projection, protocol, session_index, sessions
from conftest import rollout_events, write_rollout
from test_protocol import initialization_lines
from test_schema_v2 import VALIDATOR


def compress(value: bytes) -> bytes:
    try:
        return bytes(importlib.import_module("compression.zstd").compress(value))
    except ModuleNotFoundError:
        return bytes(importlib.import_module("zstandard").ZstdCompressor().compress(value))


def test_compressed_cold_search_preserves_title_and_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    events = rollout_events("compressed-search")
    events[2]["payload"]["message"] = "Cold compressed title"
    plain = write_rollout(tmp_path / "sessions" / "compressed.jsonl", events)
    plain.with_suffix(".jsonl.zst").write_bytes(compress(plain.read_bytes()))
    plain.unlink()
    assert (
        projection.list_session_overviews(query="Cold compressed title")[0]["id"]
        == "compressed-search"
    )
    session_index.search_index_path().unlink()
    assert projection.list_session_overviews(query="gpt-test")[0]["id"] == "compressed-search"


def test_compressed_lineage_search_uses_logical_offsets_and_shared_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    source_id = "11111111-1111-4111-8111-111111111111"
    target_id = "22222222-2222-4222-8222-222222222222"
    source = write_rollout(
        tmp_path / "sessions" / f"{source_id}.jsonl",
        [
            {
                "ordinal": 0,
                "type": "session_meta",
                "payload": {"id": source_id, "history_mode": "paginated"},
            },
            {"ordinal": 1, "type": "turn_context", "payload": {"model": "inherited-model"}},
        ],
    )
    logical_bytes = source.read_bytes()
    source.with_suffix(".jsonl.zst").write_bytes(compress(logical_bytes))
    source.unlink()
    target = write_rollout(
        tmp_path / "sessions" / f"{target_id}.jsonl",
        [
            {
                "ordinal": 2,
                "type": "session_meta",
                "payload": {
                    "id": target_id,
                    "history_mode": "paginated",
                    "history_base": {
                        "thread_id": source_id,
                        "end_ordinal_exclusive": 2,
                        "end_byte_offset": len(logical_bytes),
                    },
                },
            },
            {"ordinal": 3, "type": "turn_context", "payload": {"model": "target-model"}},
        ],
    )
    assert [e[1]["ordinal"] for e in sessions.iter_session_search_jsonl(target)] == [1, 3]
    monkeypatch.setattr(sessions, "MAX_JSONL_LINES", 3)
    with pytest.raises(ValueError, match="line limit"):
        list(sessions.iter_session_search_jsonl(target))
    monkeypatch.setattr(sessions, "MAX_JSONL_LINES", 100)
    monkeypatch.setattr(
        sessions, "MAX_JSONL_TOTAL_BYTES", max(len(logical_bytes), target.stat().st_size) + 1
    )
    with pytest.raises(ValueError, match="byte limit"):
        list(sessions.iter_session_search_jsonl(target))


def test_search_rechecks_swapped_source_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    path = write_rollout(tmp_path / "sessions" / "safe.jsonl")
    outside = write_rollout(tmp_path / "outside.jsonl", rollout_events("outside"))
    original = sessions.rollout_lineage

    def swapped(candidate: Path) -> list[sessions.RolloutSegment]:
        result = original(candidate)
        candidate.unlink()
        try:
            candidate.symlink_to(outside)
        except OSError:
            pytest.skip("symlink privilege unavailable")
        return result

    monkeypatch.setattr(sessions, "rollout_lineage", swapped)
    with pytest.raises(OSError):
        list(sessions.iter_session_search_jsonl(path))


def test_index_surrogates_do_not_break_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    path = tmp_path / "sessions" / "surrogate.jsonl"
    path.parent.mkdir()
    events = rollout_events("surrogate-session")
    events[2]["payload"]["message"] = "title " + chr(0xD800)
    path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
    assert projection.list_session_overviews()[0]["id"] == "surrogate-session"
    assert (
        session_index.SessionSearchIndex().lookup(path, sessions.session_signature(path))
        is not None
    )


def test_latest_defaults_active_and_exact_metadata_beats_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    wanted = write_rollout(tmp_path / "sessions" / "wanted.jsonl", rollout_events("target"))
    impostor = write_rollout(tmp_path / "sessions" / "target.jsonl", rollout_events("not-target"))
    archived = write_rollout(
        tmp_path / "archived_sessions" / "archive.jsonl", rollout_events("archived")
    )
    for number, path in enumerate((wanted, impostor, archived), 1):
        os.utime(path, (number, number))
    assert projection.resolve_session("target", False) == wanted
    assert (
        projection.trajectory_result({}, False)["structuredContent"]["session"]["id"]
        == "not-target"
    )
    for selector in ("", " ", "\t"):
        with pytest.raises(ValueError, match="empty"):
            projection.resolve_session(selector, False)
    with pytest.raises(ValueError, match="explicit sessionId"):
        projection.trajectory_result({"detailLevel": "full"}, False)


def test_rollout_discovery_is_reused_only_within_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def discover(include_archived: bool) -> list[Path]:
        nonlocal calls
        calls += 1
        return []

    monkeypatch.setattr(sessions, "session_files", discover)
    with sessions.session_scan():
        sessions._rollout_index()
        sessions._rollout_index()
    with sessions.session_scan():
        sessions._rollout_index()
    assert calls == 2


def test_index_merges_concurrent_snapshots_and_persists_lru(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    paths = [
        write_rollout(tmp_path / "sessions" / f"{i}.jsonl", rollout_events(str(i)))
        for i in range(3)
    ]
    first, second = session_index.SessionSearchIndex(), session_index.SessionSearchIndex()
    for owner, path in zip((first, second), paths[:2], strict=True):
        owner.store(
            path,
            sessions.session_signature(path),
            {"id": path.stem, "title": "task", "cwd": None, "model": None},
        )
        owner.flush()
    reloaded = session_index.SessionSearchIndex()
    assert all(reloaded.lookup(path, sessions.session_signature(path)) for path in paths[:2])
    reloaded.lookup(paths[0], sessions.session_signature(paths[0]))
    reloaded.flush()
    monkeypatch.setattr(session_index, "MAX_INDEX_ENTRIES", 2)
    latest = session_index.SessionSearchIndex()
    latest.store(
        paths[2],
        sessions.session_signature(paths[2]),
        {"id": "2", "title": "task", "cwd": None, "model": None},
    )
    latest.flush()
    persisted = session_index.SessionSearchIndex()
    assert persisted.lookup(paths[0], sessions.session_signature(paths[0]))
    assert persisted.lookup(paths[1], sessions.session_signature(paths[1])) is None


@pytest.mark.parametrize("last", [None, {"input_tokens": 1, "output_tokens": 1}])
def test_cumulative_tokens_survive_missing_or_inconsistent_last_samples(
    tmp_path: Path, last: object
) -> None:
    events = rollout_events("usage")[:3]
    for i, usage in enumerate(
        ({"input_tokens": 10, "output_tokens": 2}, {"input_tokens": 30, "output_tokens": 5})
    ):
        events.append(
            {
                "timestamp": 10 + i,
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"total_token_usage": usage, "last_token_usage": last},
                },
            }
        )
    path = write_rollout(tmp_path / "usage.jsonl", events)
    assert projection.parse_session(path)["stats"]["tokens"] == {
        "input_tokens": 30,
        "output_tokens": 5,
    }
    assert projection.session_overview(path)["tokens"] == {"input_tokens": 30, "output_tokens": 5}


def test_missing_call_id_never_correlates_with_numeric_real_id(tmp_path: Path) -> None:
    events = [
        {
            "type": "response_item",
            "payload": {"type": "function_call", "name": "anonymous", "arguments": "{}"},
        },
        {
            "type": "response_item",
            "payload": {"type": "function_call", "name": "real", "call_id": "1", "arguments": "{}"},
        },
        {
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "1", "output": "done"},
        },
    ]
    result = projection.parse_session(write_rollout(tmp_path / "calls.jsonl", events))
    assert result["stats"]["toolCalls"] == 2
    anonymous = next(record for record in result["records"] if record["event"] == "anonymous")
    assert anonymous["callId"] is None
    assert anonymous["status"] == "running"


def test_detail_page_and_projection_cache_have_byte_budgets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = [
        {
            "timestamp": i,
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": f"item-{i}",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "x" * 12000}],
            },
        }
        for i in range(500)
    ]
    path = write_rollout(tmp_path / "large.jsonl", events)
    result = projection.parse_session(path, detail_level="full")
    assert (
        len(json.dumps(result, ensure_ascii=True).encode()) < protocol.MAX_RPC_RESPONSE_BYTES - 1024
    )
    assert result["pagination"]["hasEarlier"]
    assert result["pagination"]["nextBeforeRecord"] == result["records"][0]["index"]
    VALIDATOR.validate(result)
    monkeypatch.setattr(projection, "MAX_TRAJECTORY_CACHE_BYTES", 1)
    projection.cached_trajectory(path)
    assert not projection._TRAJECTORY_CACHE


def test_rpc_notifications_never_invoke_tools_and_enforces_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called: list[str] = []
    original = protocol.handle

    def handle(method: str, params: Any) -> dict[str, Any]:
        if method == "tools/call":
            called.append(method)
            return {}
        return original(method, params)

    source = (
        b'{"jsonrpc":"2.0","id":0,"method":"tools/list"}\n'
        + initialization_lines()
        + b'{"jsonrpc":"2.0","method":"tools/call",'
        b'"params":{"name":"open_codex_trajectory","arguments":{}}}\n'
        + b'{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{}}\n'
    )
    output = io.BytesIO()
    monkeypatch.setattr(protocol, "handle", handle)
    monkeypatch.setattr(protocol.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(source)))
    monkeypatch.setattr(protocol.sys, "stdout", SimpleNamespace(buffer=output))
    protocol.main()
    assert called == ["tools/call"]
    responses = {
        item["id"]: item for line in output.getvalue().splitlines() if (item := json.loads(line))
    }
    assert responses[0]["error"]["code"] == -32002
    assert responses[2]["result"] == {}


def test_terminal_tool_time_survives_later_output_and_page_marker(tmp_path: Path) -> None:
    events = [
        {
            "timestamp": 1,
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "call_id": "call",
                "name": "work",
                "arguments": "{}",
            },
        }
    ]
    events.extend(
        {
            "timestamp": 2,
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": []},
        }
        for _ in range(55)
    )
    events.extend(
        [
            {
                "timestamp": 5,
                "type": "event_msg",
                "payload": {
                    "type": "mcp_tool_call_end",
                    "call_id": "call",
                    "invocation": {"tool": "work"},
                    "result": {"Ok": "authoritative"},
                },
            },
            {
                "timestamp": 10,
                "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": "call", "output": "late"},
            },
        ]
    )
    result = projection.parse_session(
        write_rollout(tmp_path / "time.jsonl", events), max_records=50, detail_level="full"
    )
    marker = next(record for record in result["records"] if record["kind"] == "tool")
    assert marker["durationMs"] == 4000
    assert marker["completedAt"] == "1970-01-01T00:00:05Z"
    assert "authoritative" in marker["output"]


def test_windows_reparse_state_paths_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        Path, "lstat", lambda _path: SimpleNamespace(st_mode=0o100600, st_file_attributes=0x400)
    )
    assert filesystem.linked_state_path(Path("junction"))

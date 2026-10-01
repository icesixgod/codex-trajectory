"""Pages reuse summaries; full details seek selected validated source lines only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from codex_trajectory import projection, sessions
from conftest import rollout_events, write_rollout


def comparable(value: dict[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key != "generatedAt"}


@pytest.fixture(autouse=True)
def clear_page_indexes() -> Any:
    with projection._PAGE_INDEX_LOCK:
        for index in projection._PAGE_INDEXES.values():
            index.close()
        projection._PAGE_INDEXES.clear()
    projection._TRAJECTORY_CACHE.clear()
    yield
    with projection._PAGE_INDEX_LOCK:
        for index in projection._PAGE_INDEXES.values():
            index.close()
        projection._PAGE_INDEXES.clear()
    projection._TRAJECTORY_CACHE.clear()


@pytest.mark.parametrize("detail", ["summary", "full"])
@pytest.mark.parametrize("before", [None, 5, 1, 1000])
def test_index_matches_canonical_projection(
    codex_home: Path, detail: str, before: int | None
) -> None:
    path = codex_home / "sessions/2026/rollout-alpha.jsonl"
    expected = projection.parse_session(path, 50, detail, before)  # type: ignore[arg-type]
    actual = projection.cached_trajectory(path, 50, detail, before)  # type: ignore[arg-type]
    assert comparable(actual) == comparable(expected)


def test_tool_envelopes_bind_pages_to_same_source_revision(codex_home: Path) -> None:
    summary = projection.trajectory_result({"sessionId": "session-alpha"}, True)
    full = projection.trajectory_result(
        {"sessionId": "session-alpha", "detailLevel": "full"}, False
    )
    revision = summary["_meta"]["codex-trajectory/revision"]
    assert len(revision) == 64
    assert full["_meta"]["codex-trajectory/revision"] == revision
    path = codex_home / "sessions/2026/rollout-alpha.jsonl"
    with path.open("a") as handle:
        handle.write("{}\n")
    newer = projection.trajectory_result({"sessionId": "session-alpha"}, False)
    assert newer["_meta"]["codex-trajectory/revision"] != revision


def test_active_summary_survives_append_without_claiming_stable_revision(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = projection.cached_trajectory

    def append_during_read(path: Path, *args: Any, **kwargs: Any) -> Any:
        result = original(path, *args, **kwargs)
        with path.open("a") as handle:
            handle.write("{}\n")
        return result

    monkeypatch.setattr(projection, "cached_trajectory", append_during_read)
    result = projection.trajectory_result({"sessionId": "session-alpha"}, True)
    assert result["structuredContent"]["session"]["id"] == "session-alpha"
    assert "codex-trajectory/revision" not in result["_meta"]


def test_history_and_details_do_not_rescan_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    events = rollout_events("large")[:4]
    for number in range(250):
        events.extend(
            [
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "exec",
                        "call_id": f"call-{number}",
                        "arguments": json.dumps({"cmd": f"echo {number}"}),
                    },
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": f"call-{number}",
                        "output": {"output": "hidden-output-" + "x" * 100_000},
                    },
                },
            ]
        )
    path = write_rollout(tmp_path / "sessions/rollout-large.jsonl", events)
    tail = projection.cached_trajectory(path, 50)
    cursor = tail["pagination"]["nextBeforeRecord"]
    expected_summary = projection.parse_session(path, 50, "summary", cursor)
    expected_full = projection.parse_session(path, 50, "full", cursor)

    def no_scan(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a warmed page must not scan the complete rollout")

    monkeypatch.setattr(projection, "iter_session_jsonl", no_scan)
    summary = projection.cached_trajectory(path, 50, "summary", cursor)
    assert comparable(summary) == comparable(expected_summary)
    full = projection.cached_trajectory(path, 50, "full", cursor)
    assert comparable(full) == comparable(expected_full)
    assert "hidden-output" not in json.dumps(summary)
    index = next(iter(projection._PAGE_INDEXES.values()))
    raw_index = index.path.read_bytes()
    assert b"hidden-output" not in raw_index
    assert b"private system instructions" not in raw_index
    assert b"echo 0" not in raw_index
    assert index.size < path.stat().st_size / 10


def test_index_invalidation_and_permissions(codex_home: Path) -> None:
    path = codex_home / "sessions/2026/rollout-alpha.jsonl"
    initial = projection.cached_trajectory(path, 50)
    with path.open("a") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "id": "new",
                        "role": "assistant",
                        "content": "new message",
                    },
                }
            )
            + "\n"
        )
    changed = projection.cached_trajectory(path, 50)
    assert changed["stats"]["records"] == initial["stats"]["records"] + 1
    assert changed["records"][-1]["summary"] == "new message"
    index = next(reversed(projection._PAGE_INDEXES.values()))
    assert index.path.parent.stat().st_mode & 0o077 == 0
    directory = index.path.parent
    projection._close_page_indexes()
    assert not directory.exists()


def test_index_size_cap_falls_back_without_leaking_tempfiles(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_trajectory import page_index

    monkeypatch.setattr(page_index, "MAX_INDEX_BYTES", 1)
    path = codex_home / "sessions/2026/rollout-alpha.jsonl"
    expected = projection.parse_session(path, 50)
    actual = projection.cached_trajectory(path, 50)
    assert comparable(actual) == comparable(expected)
    assert not projection._PAGE_INDEXES


def test_index_size_cap_stops_all_subsequent_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_trajectory import page_index

    monkeypatch.setattr(
        sessions, "current_jsonl_position", lambda: (tmp_path / "rollout.jsonl", 0, 100)
    )
    index = page_index.PageIndex()
    try:
        index.enter_event(256, 256)
        index.track({"index": 1, "turn": 1, "summary": "before cap"})
        index.save_turn({"index": 1})
        index.track_turn_error({"index": 1}, "turn_aborted")
        monkeypatch.setattr(page_index, "MAX_INDEX_BYTES", index.size - 1)
        index.flush()
        assert not index.enabled
        writes = index.db.total_changes
        size = index.size

        for event in range(257, 769):
            index.enter_event(event, event)
            index.track_identifiers(f"record-{event}", f"call-{event}")
            index.track({"index": event, "turn": event}, inherit=1)
            index.save_turn({"index": event, "summary": "x" * 1000})
            index.track_turn_error({"index": event}, "turn_aborted")
            index.flush()

        assert index.db.total_changes == writes
        assert index.size == size
        assert not index.pending
    finally:
        index.close()


def test_index_requires_unchanged_safe_source_for_full_details(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = codex_home / "sessions/2026/rollout-alpha.jsonl"
    projection.cached_trajectory(path, 50)
    index = next(iter(projection._PAGE_INDEXES.values()))
    selected = index.page(50, None, projection.MAX_TRAJECTORY_PAGE_BYTES)["records"]
    original = path.read_bytes()
    path.unlink()
    other = codex_home / "outside.jsonl"
    other.write_bytes(original)
    path.symlink_to(other)
    with pytest.raises(OSError):
        list(index.detail_entries(selected))


def test_cross_budget_index_cannot_bypass_limit(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = codex_home / "sessions/2026/rollout-alpha.jsonl"
    needed = path.stat().st_size
    monkeypatch.setattr(sessions, "MAX_JSONL_TOTAL_BYTES", needed - 1)
    with sessions.session_read_limit(needed):
        projection.cached_trajectory(path, 50)
    with pytest.raises(sessions.ReadLimitExceeded):
        projection.cached_trajectory(path, 50)


@pytest.mark.parametrize("compressed", [False, True])
def test_paginated_lineage_and_subagent_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compressed: bool
) -> None:
    from test_sessions import _paginated_line, _zstd_compress

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    parent_id = "11111111-1111-4111-8111-111111111111"
    child_id = "22222222-2222-4222-8222-222222222222"
    directory = tmp_path / "sessions"
    directory.mkdir()
    parent = directory / f"rollout-parent-{parent_id}.jsonl"
    lines = [
        _paginated_line(
            0,
            "session_meta",
            {"id": parent_id, "history_mode": "paginated", "subagent_history_start_ordinal": 40},
        )
    ]
    for n in range(1, 101):
        lines.append(
            _paginated_line(
                n,
                "event_msg",
                {
                    "type": "item_completed",
                    "turn_id": "parent",
                    "item": {
                        "type": "command_execution",
                        "id": f"cmd-{n}",
                        "command": "pwd",
                        "status": "completed",
                        "aggregated_output": f"private output {n}",
                        "exit_code": 0,
                    },
                },
            )
        )
    raw = "".join(lines).encode()
    if compressed:
        parent = parent.with_suffix(".jsonl.zst")
        parent.write_bytes(_zstd_compress(raw))
    else:
        parent.write_bytes(raw)
    child = directory / f"rollout-child-{child_id}.jsonl"
    child.write_text(
        _paginated_line(
            101,
            "session_meta",
            {
                "id": child_id,
                "history_mode": "paginated",
                "history_base": {
                    "thread_id": parent_id,
                    "end_ordinal_exclusive": 101,
                    "end_byte_offset": len(raw),
                },
            },
        )
        + _paginated_line(
            102,
            "event_msg",
            {
                "type": "item_completed",
                "turn_id": "child",
                "item": {"type": "agent_message", "id": "answer", "content": "child answer"},
            },
        )
    )
    projection.cached_trajectory(child, 50)
    expected = projection.parse_session(child, 50, "full", 50)
    original_iterator = projection.iter_session_jsonl

    def no_scan(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("indexed lineage details must not rescan all JSONL records")

    monkeypatch.setattr(projection, "iter_session_jsonl", no_scan)
    actual = projection.cached_trajectory(child, 50, "full", 50)
    monkeypatch.setattr(projection, "iter_session_jsonl", original_iterator)
    assert comparable(actual) == comparable(expected)
    # Any inherited file change invalidates the index and reruns ordinal validation.
    if not compressed:
        with parent.open("r+b") as handle:
            handle.write(b"{bad}")
        with pytest.raises(ValueError):
            projection.cached_trajectory(child, 50)


def test_full_page_keeps_serialized_byte_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    events = rollout_events("big-details")[:4]
    for n in range(80):
        events.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "id": f"answer-{n}",
                    "content": "q" * 50_000,
                },
            }
        )
    path = write_rollout(tmp_path / "sessions/rollout-details.jsonl", events)
    projection.cached_trajectory(path, 50)
    monkeypatch.setattr(projection, "MAX_TRAJECTORY_PAGE_BYTES", 100_000)
    full = projection.cached_trajectory(path, 60, "full")
    assert sum(len(json.dumps(r, ensure_ascii=True)) for r in full["records"]) <= 100_000
    assert full["pagination"]["hasEarlier"]
    assert full["pagination"]["lastRecord"] == 81
    assert full["stats"]["visibleRecords"] == len(full["records"])
    assert any(w["code"] == "page_byte_limit" for w in full["warnings"])


def test_late_terminal_marker_hydrates_original_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    events = rollout_events("late")[:6]
    for n in range(100):
        events.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "id": f"answer-{n}",
                    "content": "next",
                },
            }
        )
    events.extend(rollout_events("late")[6:7])
    path = write_rollout(tmp_path / "sessions/rollout-late.jsonl", events)
    expected = projection.parse_session(path, 50, "full")
    projection.cached_trajectory(path, 500)

    def no_scan(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a generated late-result marker must reuse the index")

    monkeypatch.setattr(projection, "iter_session_jsonl", no_scan)
    actual = projection.cached_trajectory(path, 50, "full")
    assert comparable(actual) == comparable(expected)
    assert actual["records"][-1]["input"] == expected["records"][-1]["input"]


def test_native_string_scan_preserves_strict_validation() -> None:
    from codex_trajectory.json_support import strict_json_loads

    text = '\\"[{}]' * 20_000
    assert strict_json_loads(json.dumps({"text": text})) == {"text": text}
    for invalid in (
        '{"text":"' + '\\"' * 20_000,
        '{"text":"bad\\escape"}',
        '{"text":"literal\nnewline"}',
        '{"text":"a","text":"b"}',
        '{"number":NaN}',
    ):
        with pytest.raises(ValueError):
            strict_json_loads(invalid)


@pytest.mark.parametrize("next_type", ["turn_started", "turn_complete", "item_completed"])
def test_indexed_full_turn_errors_match_superseded_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, next_type: str
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    events = rollout_events("superseded")[:5]
    payload: dict[str, Any] = {"type": next_type, "turn_id": "different"}
    if next_type == "item_completed":
        payload["item"] = {"type": "agent_message", "id": "next", "content": "next"}
    events.append({"type": "event_msg", "payload": payload})
    path = write_rollout(tmp_path / "sessions/rollout-superseded.jsonl", events)
    expected = projection.parse_session(path, 50, "full")
    actual = projection.cached_trajectory(path, 50, "full")
    assert comparable(actual) == comparable(expected)


def test_unavailable_optional_index_falls_back_to_streaming(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sqlite3

    def unavailable() -> Any:
        raise sqlite3.OperationalError("temporary storage unavailable")

    path = codex_home / "sessions/2026/rollout-alpha.jsonl"
    expected = projection.parse_session(path, 50)
    monkeypatch.setattr(projection, "PageIndex", unavailable)
    assert comparable(projection.cached_trajectory(path, 50)) == comparable(expected)


@pytest.mark.parametrize("before", [None, 51, 40])
@pytest.mark.parametrize("kind", ["call", "message"])
def test_reused_identifiers_preserve_canonical_full_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, before: int | None, kind: str
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    events = rollout_events("reused-identifiers")[:2]
    for number in range(1, 52):
        label = {1: "first", 39: "second", 51: "third"}.get(number)
        if label and kind == "call":
            events.extend(
                [
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call",
                            "call_id": "reused",
                            "name": "exec",
                            "arguments": json.dumps({"cmd": label}),
                        },
                    },
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "function_call_output",
                            "call_id": "reused",
                            "output": label,
                        },
                    },
                ]
            )
        else:
            events.append(
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "id": "reused" if label else f"answer-{number}",
                        "content": label or "filler",
                    },
                }
            )
    path = write_rollout(tmp_path / "sessions/rollout-reused.jsonl", events)
    expected = projection.parse_session(path, 50, "full", before)
    projection.cached_trajectory(path, 50)
    actual = projection.cached_trajectory(path, 50, "full", before)
    assert comparable(actual) == comparable(expected)


@pytest.mark.parametrize("initial_size", [50, 500])
@pytest.mark.parametrize("before", [None, 602, 501])
def test_enlarged_summary_preserves_unique_canonical_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, initial_size: int, before: int | None
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    events: list[dict[str, Any]] = [{"type": "session_meta", "payload": {"id": "repeated-summary"}}]
    for number in range(1, 602):
        events.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "id": "repeated" if number in (1, 601) else f"message-{number}",
                    "content": f"message {number}",
                },
            }
        )
    path = write_rollout(tmp_path / "sessions/rollout-repeated-summary.jsonl", events)
    projection.cached_trajectory(path, initial_size)
    actual = projection.cached_trajectory(path, 1000, "summary", before)
    expected = projection.parse_session(path, 1000, "summary", before)
    assert len({record["id"] for record in actual["records"]}) == len(actual["records"])
    assert comparable(actual) == comparable(expected)


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("terminal", ["turn_complete", "turn_aborted"])
def test_turn_error_hydration_opens_each_source_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compressed: bool, terminal: str
) -> None:
    from test_sessions import _zstd_compress

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    events = rollout_events("many-errors")[:2]
    for number in range(12):
        events.extend(
            [
                {
                    "timestamp": number * 3,
                    "type": "event_msg",
                    "payload": {"type": "turn_started", "turn_id": f"turn-{number}"},
                },
                {
                    "timestamp": number * 3 + 1,
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "id": f"answer-{number}",
                        "content": "answer",
                    },
                },
                {
                    "timestamp": number * 3 + 2,
                    "type": "event_msg",
                    "payload": {
                        "type": terminal,
                        "turn_id": f"turn-{number}",
                        "error": {"message": f"private error {number}"},
                        "reason": f"private reason {number}",
                    },
                },
            ]
        )
    path = write_rollout(tmp_path / "sessions/rollout-errors.jsonl", events)
    if compressed:
        raw = path.read_bytes()
        path.unlink()
        path = path.with_suffix(".jsonl.zst")
        path.write_bytes(_zstd_compress(raw))
    expected = projection.parse_session(path, 50, "full")
    summary = projection.cached_trajectory(path, 50)
    index = next(iter(projection._PAGE_INDEXES.values()))
    handles: list[Any] = []
    original = sessions._open_rollout_binary

    def counted(source: Path) -> Any:
        handle = original(source)
        handles.append(handle)
        return handle

    monkeypatch.setattr(sessions, "_open_rollout_binary", counted)
    index.hydrate_turn_errors(summary["turns"])
    assert len(handles) == 1
    assert all(handle.closed for handle in handles)
    assert summary["turns"] == expected["turns"]
    assert comparable(projection.cached_trajectory(path, 50, "full")) == comparable(expected)

    from codex_trajectory import page_index

    def invalid_entry(value: str) -> Any:
        raise ValueError("invalid indexed error entry")

    monkeypatch.setattr(page_index, "strict_json_loads", invalid_entry)
    with pytest.raises(ValueError, match="invalid indexed error entry"):
        index.hydrate_turn_errors(summary["turns"])
    assert all(handle.closed for handle in handles)

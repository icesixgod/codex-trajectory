"""Current-task timing must cover full histories without inventing token timings."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from codex_trajectory import project_timing, projection, sessions
from codex_trajectory.project_timing import summarize_timing
from codex_trajectory.tools import call_tool, tool_definitions
from conftest import rollout_events, write_rollout

BASE = 1_786_665_600_000


@pytest.fixture(autouse=True)
def timing_cache() -> Iterator[None]:
    project_timing._TIMING_CACHE.clear()
    yield
    project_timing._TIMING_CACHE.clear()


def timed(
    index: int, start: int, end: int, *, turn: int | None = None, kind: str = "tool"
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "index": index,
        "startedAt": projection.iso_timestamp(BASE + start),
        "completedAt": projection.iso_timestamp(BASE + end),
        "durationMs": end - start,
    }
    if turn is None:
        value["modelCalls"] = 1
    else:
        value.update(turn=turn, kind=kind, callId=f"call-{index}")
    return value


def test_parallel_tools_count_once_and_gaps_between_turns_are_excluded() -> None:
    result = summarize_timing(
        [timed(1, 0, 10_000), timed(2, 100_000, 104_000)],
        [timed(1, 2_000, 6_000, turn=1), timed(2, 4_000, 7_000, turn=1)],
    )
    assert result == {
        "totalMs": 14_000,
        "localMs": 5_000,
        "modelMs": 9_000,
        "modelRecordedMs": 0,
        "modelEstimatedMs": 9_000,
        "unattributedMs": 0,
        "turns": 2,
        "timedTurns": 2,
        "untimedTools": 0,
        "missingTimingTools": 0,
        "invalidTimingTools": 0,
        "zeroDurationTools": 0,
        "submillisecondTools": 0,
    }


def test_unknown_tools_only_block_their_own_interval_and_keep_recorded_model_time() -> None:
    unknown = timed(1, 1_000, 3_000, turn=1)
    unknown["durationMs"] = None
    result = summarize_timing(
        [timed(1, 0, 10_000)],
        [unknown, timed(2, 5_000, 7_000, turn=1, kind="reasoning")],
    )
    assert result["modelRecordedMs"] == 2_000
    assert result["modelEstimatedMs"] == 6_000
    assert result["localMs"] == 0
    assert result["unattributedMs"] == 2_000
    assert result["untimedTools"] == 1
    assert result["missingTimingTools"] == 1


def test_other_recorded_activity_is_not_estimated_as_model_work() -> None:
    result = summarize_timing(
        [timed(1, 0, 10_000)],
        [timed(1, 2_000, 5_000, turn=1, kind="subagent")],
    )
    assert result["modelMs"] == 7_000
    assert result["unattributedMs"] == 3_000


def test_duplicate_late_result_does_not_double_time_or_mark_call_unknown() -> None:
    original = timed(1, 2_000, 4_000, turn=1)
    marker = dict(original, index=100)
    unknown = dict(original, index=101, durationMs=None)
    result = summarize_timing([timed(1, 0, 10_000)], [original, marker, unknown])
    assert result["localMs"] == 2_000
    assert result["modelMs"] == 8_000
    assert result["untimedTools"] == 0


def test_reused_call_id_in_another_turn_cannot_confirm_unknown_timing() -> None:
    known = timed(1, 2_000, 4_000, turn=1)
    unknown = dict(timed(2, 101_000, 103_000, turn=2), callId="call-1", durationMs=None)
    result = summarize_timing([timed(1, 0, 10_000), timed(2, 100_000, 110_000)], [known, unknown])
    assert result["localMs"] == 2_000
    assert result["modelMs"] == 16_000
    assert result["unattributedMs"] == 2_000
    assert result["untimedTools"] == 1


@pytest.mark.parametrize("status", ["zero", "submillisecond"])
def test_precision_limited_tools_do_not_block_other_model_estimates(status: str) -> None:
    tool = dict(timed(1, 2_000, 3_000, turn=1), durationMs=None, _timingStatus=status)
    marker = dict(tool, index=100)
    result = summarize_timing([timed(1, 0, 10_000)], [tool, marker])
    assert result["localMs"] == result["unattributedMs"] == result["untimedTools"] == 0
    assert result["modelEstimatedMs"] == 10_000
    assert result["zeroDurationTools"] == (status == "zero")
    assert result["submillisecondTools"] == (status == "submillisecond")


def test_completion_only_parallel_tool_uses_model_step_anchor_not_previous_result() -> None:
    result = summarize_timing(
        [timed(1, 0, 10_000)],
        [
            timed(1, 0, 1_000, turn=1, kind="reasoning"),
            dict(timed(2, 2_000, 4_000, turn=1), step=1),
            dict(timed(3, 6_000, 6_000, turn=1), step=1, durationMs=None, _hasStartBoundary=False),
        ],
    )
    assert result["localMs"] == 2_000
    assert result["modelRecordedMs"] == 1_000
    assert result["modelEstimatedMs"] == 4_000
    assert result["unattributedMs"] == 3_000
    assert result["untimedTools"] == 1


def test_running_tool_reserves_its_remaining_interval() -> None:
    tool = dict(
        timed(1, 2_000, 2_000, turn=1),
        completedAt=None,
        durationMs=None,
        status="running",
        _hasStartBoundary=True,
    )
    result = summarize_timing([timed(1, 0, 10_000)], [tool])
    assert result["unattributedMs"] == 8_000
    assert result["modelEstimatedMs"] == 2_000
    assert result["untimedTools"] == 1


def test_tool_without_any_boundaries_stays_unattributed() -> None:
    tool = dict(timed(1, 0, 1, turn=1), startedAt=None, completedAt=None, durationMs=None)
    result = summarize_timing([timed(1, 0, 10_000)], [tool])
    assert result["unattributedMs"] == 10_000
    assert result["modelMs"] == 0


def test_invalid_timing_is_distinguished_and_overlap_counts_once() -> None:
    tools = [
        dict(timed(1, 1_000, 5_000, turn=1), durationMs=None, _timingStatus="invalid"),
        dict(timed(2, 3_000, 7_000, turn=1), durationMs=None),
        timed(3, 4_000, 6_000, turn=1),
        timed(4, 5_000, 8_000, turn=1, kind="reasoning"),
    ]
    result = summarize_timing([timed(1, 0, 10_000)], tools)
    assert result["invalidTimingTools"] == result["missingTimingTools"] == 1
    assert result["untimedTools"] == 2
    assert result["localMs"] == 2_000
    assert result["modelRecordedMs"] == 2_000
    assert result["modelEstimatedMs"] == result["unattributedMs"] == 3_000
    assert result["localMs"] + result["modelMs"] + result["unattributedMs"] == result["totalMs"]


@pytest.mark.parametrize("duration", [None, 0, False, -1, 0.5])
def test_unknown_turn_duration_is_not_reconstructed_from_point_timestamps(duration: Any) -> None:
    turn = dict(timed(1, 0, 10_000), durationMs=duration)
    result = summarize_timing([turn], [timed(1, 1_000, 2_000, turn=1)])
    assert result["totalMs"] == result["localMs"] == result["modelMs"] == 0
    assert result["turns"] == 1
    assert result["timedTurns"] == 0


def test_running_turn_uses_latest_persisted_elapsed_time_and_clips_tools() -> None:
    turn = dict(timed(1, 0, 10_000), completedAt=None)
    result = summarize_timing([turn], [timed(1, 9_000, 12_000, turn=1)])
    assert result["localMs"] == 1_000
    assert result["modelMs"] == 9_000


def test_no_model_evidence_leaves_remaining_time_unattributed() -> None:
    turn = dict(timed(1, 0, 10_000), modelCalls=0)
    result = summarize_timing([turn], [timed(1, 1_000, 2_000, turn=1)])
    assert result["localMs"] == 1_000
    assert result["modelMs"] == 0
    assert result["unattributedMs"] == 9_000


def test_overlapping_turns_and_model_intervals_form_an_exact_partition() -> None:
    result = summarize_timing(
        [timed(1, 0, 10_000), timed(2, 5_000, 15_000)],
        [
            timed(1, 1_000, 9_000, turn=1),
            timed(2, 7_000, 12_000, turn=2, kind="assistant"),
        ],
    )
    assert result["totalMs"] == 15_000
    assert result["localMs"] == 8_000
    assert result["modelRecordedMs"] == 3_000
    assert result["modelEstimatedMs"] == 4_000
    assert result["unattributedMs"] == 0


def get_project(session_id: str = "session-alpha", **arguments: Any) -> dict[str, Any]:
    result = call_tool("get_codex_project_timing", {"sessionId": session_id, **arguments})
    assert not result.get("isError"), result
    return result["structuredContent"]


def test_running_server_keeps_its_timing_implementation_after_files_are_updated(
    codex_home: Path, tmp_path: Path
) -> None:
    runtime = tmp_path / "runtime"
    package = runtime / "codex_trajectory"
    shutil.copytree(
        Path(__file__).parents[1] / "scripts" / "codex_trajectory",
        package,
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from codex_trajectory.tools import call_tool
Path(sys.argv[2]).write_text(
    "raise RuntimeError('Plugin files changed after server startup.')\\n", encoding="utf-8"
)
result = call_tool("get_codex_project_timing", {"sessionId": "session-alpha"})
assert not result.get("isError"), result
assert result["structuredContent"]["selectedSessionId"] == "session-alpha"
assert result["structuredContent"]["timing"]["totalMs"] == 7990
""",
            str(runtime),
            str(package / "project_timing.py"),
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize(
    "capability", ["resolve_timing_session", "session_timing_summary", "timing_records"]
)
def test_mixed_runtime_requests_reload_without_reading_task_data(
    monkeypatch: pytest.MonkeyPatch, capability: str
) -> None:
    component = projection.PageIndex if capability == "timing_records" else projection
    monkeypatch.delattr(component, capability)

    def unexpected_read(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("An incompatible runtime must not read task data.")

    monkeypatch.setattr(projection, "session_files", unexpected_read)
    result = call_tool("get_codex_project_timing", {"sessionId": "session-alpha"})
    assert result["isError"] is True
    assert result["structuredContent"] == {"viewerState": "plugin-reload-required"}
    assert "Fully quit and reopen Codex" in result["content"][0]["text"]


def test_only_the_selected_task_contributes_to_timing(codex_home: Path) -> None:
    write_rollout(codex_home / "sessions" / "rollout-other.jsonl", rollout_events("other-task"))
    payload = get_project()
    assert payload["scope"] == "session"
    assert payload["sessionCount"] == 1
    assert [row["id"] for row in payload["sessions"]] == ["session-alpha"]
    assert payload["timing"]["totalMs"] == 7_990
    assert payload["timing"]["localMs"] == 2_250
    assert payload["timing"]["modelMs"] == 3_750
    assert payload["timing"]["unattributedMs"] == 1_990
    assert payload["aggregation"] == "task-active-time"
    assert payload["skippedSessions"] == payload["unreadableMetadata"] == 0
    serialized = json.dumps(payload)
    for hidden in (
        str(codex_home),
        str(Path.home()),
        "secret-tool-input",
        "secret-tool-output",
        "private system instructions",
        "opaque-secret-reasoning",
        "user:secret",
        "other-task",
        "session-archive",
    ):
        assert hidden not in serialized
    assert get_project("session-archive")["sessionCount"] == 1
    assert get_project("session-archive")["selectedSessionId"] == "session-archive"


def test_cache_reuses_only_selected_timing_and_invalidates_on_append(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []
    original = projection.session_timing_summary

    def counted(path: Path) -> dict[str, Any]:
        calls.append(path)
        return original(path)

    monkeypatch.setattr(projection, "session_timing_summary", counted)
    first = get_project()
    assert len(calls) == 1
    assert get_project()["timing"] == first["timing"]
    assert len(calls) == 1
    path = codex_home / "sessions" / "2026" / "rollout-alpha.jsonl"
    events = rollout_events()
    events[-2]["timestamp"] = "2026-08-14T00:00:12.000Z"
    write_rollout(path, events)
    assert get_project()["timing"]["totalMs"] == first["timing"]["totalMs"] + 2_000
    assert calls == [path, path]


def test_open_task_reuses_index_without_discovery_or_other_metadata_reads(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = codex_home / "sessions" / "2026" / "rollout-alpha.jsonl"
    projection.cached_trajectory(path)
    original = projection.first_session_metadata

    def selected_metadata(candidate: Path) -> dict[str, Any]:
        assert candidate == path, "Timing must not inspect another task's metadata"
        return original(candidate)

    def forbid(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("An open task must reuse its source and index without discovery or parsing")

    monkeypatch.setattr(projection, "session_files", forbid)
    monkeypatch.setattr(projection, "parse_session", forbid)
    monkeypatch.setattr(projection, "first_session_metadata", selected_metadata)
    monkeypatch.setattr(project_timing, "first_session_metadata", selected_metadata)
    assert get_project()["timing"]["totalMs"] == 7_990


def test_cold_uuid_lookup_reads_only_the_named_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    identity = "10101010-1010-4010-8010-101010101010"
    path = write_rollout(
        tmp_path / "sessions" / f"rollout-current-{identity}.jsonl", rollout_events(identity)
    )
    write_rollout(tmp_path / "sessions" / "rollout-other.jsonl", rollout_events("other-task"))
    original = projection.first_session_metadata

    def selected_metadata(candidate: Path) -> dict[str, Any]:
        assert candidate == path
        return original(candidate)

    def forbid(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("An exact UUID must not use the legacy metadata scan")

    monkeypatch.setattr(projection, "resolve_session", forbid)
    monkeypatch.setattr(projection, "first_session_metadata", selected_metadata)
    assert get_project(identity)["timing"]["totalMs"] == 7_990


def test_no_project_directory_is_required(codex_home: Path) -> None:
    events = rollout_events()
    del events[0]["payload"]["cwd"]
    write_rollout(codex_home / "sessions" / "2026" / "rollout-alpha.jsonl", events)
    payload = get_project()
    assert payload["project"]["cwd"] is None
    assert payload["timing"]["totalMs"] == 7_990


def test_open_task_cache_cannot_cross_codex_homes(
    codex_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = codex_home / "sessions" / "2026" / "rollout-alpha.jsonl"
    projection.cached_trajectory(old)
    changed_home = tmp_path / "other-home"
    events = rollout_events()
    events[-2]["timestamp"] = "2026-08-14T00:00:12.000Z"
    current = write_rollout(changed_home / "sessions" / "rollout-alpha.jsonl", events)
    monkeypatch.setenv("CODEX_HOME", str(changed_home))
    original = projection.first_session_metadata

    def only_current(candidate: Path) -> dict[str, Any]:
        assert candidate == current, "Cached source from another Codex home must not be opened"
        return original(candidate)

    monkeypatch.setattr(projection, "first_session_metadata", only_current)
    assert get_project()["timing"]["totalMs"] == 9_990


def test_task_read_budget_and_cache_remain_request_local(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = codex_home / "sessions" / "2026" / "rollout-alpha.jsonl"
    size = path.stat().st_size
    monkeypatch.setattr(sessions, "MAX_JSONL_TOTAL_BYTES", size - 1)
    enabled = get_project(maxReadBytes=size + 100)
    assert enabled["sessionCount"] == 1
    assert enabled["skippedSessions"] == enabled["unreadableMetadata"] == 0
    assert sessions.jsonl_byte_limit() == size - 1
    refused = call_tool("get_codex_project_timing", {"sessionId": "session-alpha"})
    assert refused["isError"] is True


def test_descriptor_describes_current_task_only() -> None:
    tool = next(item for item in tool_definitions() if item["name"] == "get_codex_project_timing")
    assert tool["inputSchema"]["required"] == ["sessionId"]
    assert "selected task" in tool["description"]
    assert tool["annotations"]["readOnlyHint"] is True


def test_temporary_index_limit_returns_no_partial_timing(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from codex_trajectory import page_index

    monkeypatch.setattr(page_index, "MAX_INDEX_BYTES", 1)
    result = call_tool("get_codex_project_timing", {"sessionId": "session-alpha"})
    assert result["isError"] is True
    assert "temporary summary index limit" in result["content"][0]["text"]


def test_timing_reads_every_turn_beyond_page_and_turn_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    events = [rollout_events()[0]]
    for index in range(1_005):
        offset = index * 20_000
        events.extend(
            [
                {
                    "timestamp": projection.iso_timestamp(BASE + offset),
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": f"t-{index}"},
                },
                {
                    "timestamp": projection.iso_timestamp(BASE + offset + 2_000),
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "call_id": f"c-{index}",
                        "name": "exec",
                        "arguments": "{}",
                    },
                },
                {
                    "timestamp": projection.iso_timestamp(BASE + offset + 3_000),
                    "type": "response_item",
                    "payload": {"type": "function_call_output", "call_id": f"c-{index}"},
                },
                {
                    "timestamp": projection.iso_timestamp(BASE + offset + 10_000),
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": f"t-{index}"},
                },
            ]
        )
    write_rollout(tmp_path / "sessions" / "rollout-many.jsonl", events)
    timing = get_project()["timing"]
    assert timing["turns"] == timing["timedTurns"] == 1_005
    assert timing["totalMs"] == 10_050_000
    assert timing["localMs"] == 1_005_000


@pytest.mark.parametrize(
    "arguments, message",
    [
        ({}, "explicit sessionId"),
        ({"sessionId": "latest"}, "explicit sessionId"),
        ({"sessionId": 1}, "explicit sessionId"),
        ({"sessionId": ""}, "explicit sessionId"),
        ({"sessionId": "/tmp/file"}, "identifier"),
        ({"sessionId": "session-alpha", "includeArchived": 1}, "boolean"),
        ({"sessionId": "session-alpha", "extra": 1}, "Unknown argument"),
        ({"sessionId": "session-alpha", "maxReadBytes": True}, "integer"),
    ],
)
def test_project_arguments_are_validated(
    codex_home: Path, arguments: dict[str, Any], message: str
) -> None:
    result = call_tool("get_codex_project_timing", arguments)
    assert result["isError"] is True
    assert message in result["content"][0]["text"]


@pytest.mark.parametrize("compressed", [False, True])
def test_paginated_lineage_counts_task_once_and_preserves_recorded_item_timing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, compressed: bool
) -> None:
    from test_sessions import _paginated_line, _zstd_compress

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    thread_id = "11111111-1111-4111-8111-111111111111"
    rollout_id = "22222222-2222-4222-8222-222222222222"
    directory = tmp_path / "sessions"
    directory.mkdir()
    root = directory / f"rollout-parent-{thread_id}.jsonl"
    raw = (
        _paginated_line(
            0,
            "session_meta",
            {"id": thread_id, "history_mode": "paginated", "cwd": "/work/project"},
        )
        + _paginated_line(1, "event_msg", {"type": "turn_started", "turn_id": "work"})
        + _paginated_line(
            2,
            "event_msg",
            {
                "type": "item_completed",
                "turn_id": "work",
                "started_at_ms": BASE + 172_801_000,
                "completed_at_ms": BASE + 172_802_000,
                "item": {"type": "reasoning", "id": "reason-1", "summary_text": []},
            },
        )
    ).encode()
    if compressed:
        root = root.with_suffix(".jsonl.zst")
        root.write_bytes(_zstd_compress(raw))
    else:
        root.write_bytes(raw)
    child = directory / f"rollout-child-{thread_id}_{rollout_id}.jsonl"
    child.write_text(
        _paginated_line(
            3,
            "session_meta",
            {
                "id": thread_id,
                "history_mode": "paginated",
                "cwd": "/work/project",
                "history_base": {
                    "thread_id": thread_id,
                    "end_ordinal_exclusive": 3,
                    "end_byte_offset": len(raw),
                },
            },
        )
        + _paginated_line(
            4,
            "event_msg",
            {"type": "turn_complete", "turn_id": "work", "duration_ms": 5_000},
        ),
        encoding="utf-8",
    )
    payload = get_project(thread_id)
    assert payload["sessionCount"] == 1
    assert payload["timing"]["turns"] == payload["timing"]["timedTurns"] == 1
    assert payload["timing"]["totalMs"] == 5_000
    assert payload["timing"]["modelRecordedMs"] == 1_000

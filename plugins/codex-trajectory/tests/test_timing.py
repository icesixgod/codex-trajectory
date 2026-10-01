"""Regression coverage for timing precision and missing call boundaries."""

from __future__ import annotations

from pathlib import Path

import pytest
from codex_trajectory.projection import duration_milliseconds, elapsed_milliseconds, parse_session
from conftest import write_rollout


@pytest.mark.parametrize("nanos", [1, 9_375, 499_999, 500_000, 999_999])
def test_submillisecond_durations_are_not_inflated_to_one_ms(nanos: int) -> None:
    # Zero is an internal marker; final tool records expose unconfirmed timing as null.
    assert duration_milliseconds({"secs": 0, "nanos": nanos}) == 0
    assert duration_milliseconds(nanos / 1_000_000_000) == 0
    assert duration_milliseconds({"secs": 0, "nanos": 1_000_000}) == 1


@pytest.mark.parametrize(
    ("started", "completed", "expected"),
    [
        (None, 1001, None),
        (1000, None, None),
        (1000, 1000, None),
        (1001, 1000, None),
        (1000, 1001, 1),
    ],
)
def test_elapsed_duration_requires_distinct_ordered_boundaries(
    started: int | None, completed: int | None, expected: int | None
) -> None:
    assert elapsed_milliseconds(started, completed) == expected


@pytest.mark.parametrize(
    "raw_duration",
    [{"secs": 0, "nanos": 9_375}, {"secs": 0, "nanos": 0}, 0, None, "invalid"],
)
def test_unconfirmed_explicit_duration_is_not_replaced_by_later_timestamps(
    tmp_path: Path, raw_duration: object
) -> None:
    events = [
        {
            "timestamp": 1,
            "type": "response_item",
            "payload": {"type": "function_call", "name": "exec", "call_id": "call"},
        },
        {
            "timestamp": 1.001,
            "type": "event_msg",
            "payload": {
                "type": "mcp_tool_call_end",
                "call_id": "call",
                "invocation": {"tool": "exec"},
                "duration": raw_duration,
                "result": {"Ok": {"content": []}},
            },
        },
        {
            "timestamp": 2,
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "call", "output": "done"},
        },
    ]
    result = parse_session(write_rollout(tmp_path / "unconfirmed.jsonl", events))
    assert len(result["records"]) == 1
    assert result["records"][0]["durationMs"] is None
    assert result["records"][0]["status"] == "complete"


@pytest.mark.parametrize(
    "raw_duration",
    [{"secs": 0, "nanos": 9_375}, {"secs": 0, "nanos": 0}, 0, None, "invalid"],
)
def test_paginated_command_with_unconfirmed_duration_keeps_it_unknown(
    tmp_path: Path, raw_duration: object
) -> None:
    thread_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    events = [
        {
            "timestamp": 1,
            "ordinal": 0,
            "type": "session_meta",
            "payload": {"id": thread_id, "history_mode": "paginated"},
        },
        {
            "timestamp": 2,
            "ordinal": 1,
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "turn",
                "started_at_ms": 1000,
                "completed_at_ms": 1001,
                "item": {
                    "type": "CommandExecution",
                    "id": "command",
                    "command": ["ruff", "check"],
                    "status": "completed",
                    "duration": raw_duration,
                },
            },
        },
    ]
    result = parse_session(write_rollout(tmp_path / f"rollout-{thread_id}.jsonl", events))
    assert result["records"][0]["durationMs"] is None
    assert result["records"][0]["event"] == "Command"


def test_paginated_command_without_duration_keeps_confirmed_boundaries(tmp_path: Path) -> None:
    thread_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    events = [
        {
            "timestamp": 1,
            "ordinal": 0,
            "type": "session_meta",
            "payload": {"id": thread_id, "history_mode": "paginated"},
        },
        {
            "timestamp": 2,
            "ordinal": 1,
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "turn",
                "started_at_ms": 1000,
                "completed_at_ms": 1250,
                "item": {
                    "type": "CommandExecution",
                    "id": "command",
                    "command": ["ruff", "check"],
                    "status": "completed",
                },
            },
        },
    ]
    result = parse_session(write_rollout(tmp_path / f"rollout-{thread_id}.jsonl", events))
    assert result["records"][0]["durationMs"] == 250


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "web_search_call", "id": "point", "status": "completed"},
        {"type": "image_generation_call", "id": "point", "status": "completed"},
        {"type": "local_shell_call", "id": "point", "status": "completed"},
        {"type": "tool_search_call", "id": "point", "status": "completed"},
        {"type": "tool_search_output", "call_id": "point", "tools": []},
        {"type": "function_call_output", "call_id": "point", "output": "done"},
        {"type": "custom_tool_call_output", "call_id": "point", "output": "done"},
    ],
)
def test_terminal_only_raw_tools_have_unknown_duration(tmp_path: Path, payload: dict) -> None:
    result = parse_session(
        write_rollout(
            tmp_path / "point-tool.jsonl",
            [{"timestamp": 10, "type": "response_item", "payload": payload}],
        )
    )

    record = result["records"][0]
    assert record["status"] == "complete"
    assert record["startedAt"] == record["completedAt"]
    assert record["durationMs"] is None


def test_repeated_orphan_outputs_do_not_become_a_measured_interval(tmp_path: Path) -> None:
    result = parse_session(
        write_rollout(
            tmp_path / "repeated-result.jsonl",
            [
                {
                    "timestamp": timestamp,
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": "missing-call",
                        "output": "done",
                    },
                }
                for timestamp in (10, 12)
            ],
        )
    )

    assert len(result["records"]) == 1
    assert result["records"][0]["durationMs"] is None


@pytest.mark.parametrize("terminal_type", ["web_search_call", "tool_search_call"])
def test_terminal_raw_tool_keeps_a_real_prior_start(tmp_path: Path, terminal_type: str) -> None:
    result = parse_session(
        write_rollout(
            tmp_path / "matched-call.jsonl",
            [
                {
                    "timestamp": 10,
                    "type": "response_item",
                    "payload": {"type": terminal_type, "id": "call", "status": "in_progress"}
                    if terminal_type == "tool_search_call"
                    else {"type": "function_call", "name": "search", "call_id": "call"},
                },
                {
                    "timestamp": 12,
                    "type": "response_item",
                    "payload": {"type": terminal_type, "id": "call", "status": "completed"},
                },
            ],
        )
    )

    assert len(result["records"]) == 1
    assert result["records"][0]["durationMs"] == 2000


@pytest.mark.parametrize(
    ("completion", "expected"),
    [
        ({}, None),
        ({"duration_ms": 0}, None),
        ({"duration_ms": 0.5}, None),
        ({"duration_ms": None, "started_at": 8}, None),
        ({"duration_ms": "invalid", "started_at": 8}, None),
        ({"duration_ms": -1, "started_at": 8}, None),
        ({"duration_ms": False, "started_at": 8}, None),
        ({"duration_ms": 1250}, 1250),
        ({"started_at": 8}, 2000),
    ],
)
def test_completion_only_turn_does_not_invent_a_zero_duration(
    tmp_path: Path, completion: dict, expected: int | None
) -> None:
    result = parse_session(
        write_rollout(
            tmp_path / "point-turn.jsonl",
            [
                {
                    "timestamp": 10,
                    "type": "event_msg",
                    "payload": {"type": "turn_complete", "turn_id": "turn", **completion},
                }
            ],
        )
    )

    assert result["turns"][0]["status"] == "complete"
    assert result["turns"][0]["durationMs"] == expected

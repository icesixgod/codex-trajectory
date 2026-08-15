"""Behavior and privacy tests for rollout projection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from codex_trajectory.privacy import (
    bounded,
    content_text,
    display_path,
    json_text,
    reasoning_summary,
    safe_git,
    shorten,
)
from codex_trajectory.projection import (
    iso_timestamp,
    list_session_overviews,
    output_is_error,
    parse_session,
    parse_timestamp,
    resolve_session,
    session_overview,
)
from conftest import rollout_events, write_rollout


def test_summary_projects_turns_tools_usage_and_privacy(tmp_path: Path) -> None:
    path = write_rollout(tmp_path / "rollout.jsonl")
    result = parse_session(path)
    serialized = json.dumps(result)

    assert result["schemaVersion"] == 1
    assert result["detailLevel"] == "summary"
    assert result["stats"] == {
        "turns": 2,
        "records": 9,
        "visibleRecords": 9,
        "omittedRecords": 0,
        "toolCalls": 2,
        "failedTools": 1,
        "compactions": 1,
        "tokens": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
        "contextWindow": 100000,
    }
    assert result["turns"][0]["steps"] == 2
    assert result["turns"][0]["timeToFirstTokenMs"] == 900
    assert result["turns"][1]["status"] == "aborted"
    assert result["session"]["cwd"].startswith("~")
    assert result["session"]["git"] == {"branch": "main", "commit_hash": "abc123"}
    assert all(record["input"] is None for record in result["records"])
    assert all(record["output"] is None for record in result["records"])
    assert all(record["metadata"] == {} for record in result["records"])
    assert "private system instructions" not in serialized
    assert "opaque-secret-reasoning" not in serialized
    assert "secret-tool-input" not in serialized
    assert "secret-tool-output" not in serialized
    assert "repository_url" not in serialized
    assert '"source"' not in serialized


def test_full_details_are_bounded_but_never_include_private_reasoning(tmp_path: Path) -> None:
    events = rollout_events()
    events[5]["payload"]["arguments"] = json.dumps({"cmd": "x" * 13_000})
    path = write_rollout(tmp_path / "rollout.jsonl", events)
    result = parse_session(path, detail_level="full")
    serialized = json.dumps(result)
    tool = next(record for record in result["records"] if record["callId"] == "call-1")

    assert result["detailLevel"] == "full"
    assert tool["input"] and "truncated" in tool["input"]
    assert tool["output"] and "secret-tool-output" in tool["output"]
    assert tool["metadata"] == {"protocolType": "function_call"}
    assert "private system instructions" not in serialized
    assert "opaque-secret-reasoning" not in serialized
    assert "repository_url" not in serialized


def test_record_tail_preserves_original_indexes(tmp_path: Path) -> None:
    events = rollout_events()
    for index in range(70):
        events.insert(
            -1,
            {
                "timestamp": f"2026-08-14T00:01:{index % 60:02d}.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": f"bulk-{index}",
                    "role": "assistant",
                    "content": [{"text": f"record {index}"}],
                },
            },
        )
    result = parse_session(write_rollout(tmp_path / "large.jsonl", events), max_records=50)

    assert result["stats"]["records"] > 50
    assert len(result["records"]) == 50
    assert result["records"][0]["index"] > 1
    assert [record["index"] for record in result["records"]] == sorted(
        record["index"] for record in result["records"]
    )
    assert result["stats"]["failedTools"] == 1


@pytest.mark.parametrize(
    "completion",
    [
        {"success": False},
        {"result": {"Err": "user cancelled MCP tool call"}},
    ],
)
def test_completion_failure_is_not_overwritten_by_plain_output(
    tmp_path: Path, completion: dict[str, object]
) -> None:
    events: list[dict[str, object]] = [
        {
            "timestamp": "2026-08-14T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "task_started"},
        },
        {
            "timestamp": "2026-08-14T00:00:01Z",
            "type": "response_item",
            "payload": {"type": "function_call", "call_id": "failed", "name": "demo"},
        },
        {
            "timestamp": "2026-08-14T00:00:02Z",
            "type": "event_msg",
            "payload": {"type": "mcp_tool_call_end", "call_id": "failed", **completion},
        },
        {
            "timestamp": "2026-08-14T00:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "failed",
                "output": '[{"type":"text","text":"user cancelled MCP tool call"}]',
            },
        },
    ]
    result = parse_session(write_rollout(tmp_path / "failure.jsonl", events))
    tool = result["records"][0]

    assert tool["status"] == "error"
    assert tool["summary"] == "demo · error"
    assert result["stats"]["failedTools"] == 1


def test_jsonl_diagnostics_distinguish_bad_line_from_incomplete_tail(tmp_path: Path) -> None:
    path = write_rollout(tmp_path / "damaged.jsonl")
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{bad complete line}\n")
        handle.write('{"timestamp":"unfinished"')
    result = parse_session(path)

    assert len(result["warnings"]) == 1
    assert result["warnings"][0]["code"] == "malformed_jsonl"


def test_resolve_exact_prefix_archived_and_ambiguous(codex_home: Path) -> None:
    assert resolve_session("session-alpha", False).name == "rollout-alpha.jsonl"
    assert resolve_session("session-a", False).name == "rollout-alpha.jsonl"
    assert resolve_session("session-archive", True).name == "rollout-archive.jsonl"
    write_rollout(
        codex_home / "sessions" / "rollout-another.jsonl", rollout_events("session-alpine")
    )
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_session("session-a", False)
    with pytest.raises(ValueError, match="not found"):
        resolve_session("missing", True)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2026-08-14T00:00:00Z", 1786665600000), (1_786_665_600, 1786665600000), (None, None)],
)
def test_timestamp_parsing(value: object, expected: int | None) -> None:
    assert parse_timestamp(value) == expected


def test_helper_edge_cases() -> None:
    assert shorten(" a   b ") == "a b"
    assert shorten("x" * 230).endswith("…")
    assert "truncated" in bounded("x" * 13_000)
    assert json_text('{"a":1}').startswith("{")
    assert json_text({1, 2}).startswith("{")
    assert json_text("plain") == "plain"
    assert content_text("text") == "text"
    assert content_text(None) == ""
    assert content_text(["a", {"text": "b"}, {"type": "input_image"}]) == "a\n\nb\n\n[image]"
    assert content_text([{}, 1]) == ""
    assert reasoning_summary("summary") == "summary"
    assert reasoning_summary(None) == ""
    assert reasoning_summary(["a", {"text": "b"}, {}]) == "a\n\nb"
    assert display_path(Path.home().as_posix()) == "~"
    assert display_path(None) is None
    assert display_path("/system/example") == "<absolute>/example"
    assert safe_git(None) is None
    assert safe_git({"repository_url": "secret"}) is None
    assert output_is_error('{"success":false}')
    assert not output_is_error("plain output")
    assert not output_is_error("[]")
    assert iso_timestamp(None) is None
    assert parse_timestamp("not-a-time") is None
    assert parse_timestamp(1_786_665_600_000) == 1_786_665_600_000


def test_invalid_detail_level(tmp_path: Path) -> None:
    path = write_rollout(tmp_path / "rollout.jsonl")
    with pytest.raises(ValueError, match="detailLevel"):
        parse_session(path, detail_level="verbose")  # type: ignore[arg-type]


def test_overview_fallbacks_filters_limits_and_empty_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    with pytest.raises(ValueError, match="No local"):
        resolve_session(None, False)

    events: list[dict[str, object]] = [
        {"timestamp": "bad", "type": "ignored", "payload": []},
        {
            "timestamp": "2026-08-14T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": "fallback-id"},
        },
        {
            "timestamp": "2026-08-14T00:00:01Z",
            "type": "turn_context",
            "payload": {
                "model": "fallback-model",
                "effort": "low",
                "collaboration_mode_kind": "plan",
            },
        },
        {
            "timestamp": "2026-08-14T00:00:02Z",
            "type": "event_msg",
            "payload": {"type": "token_count", "info": "invalid"},
        },
    ]
    first = write_rollout(home / "sessions" / "one.jsonl", events)  # type: ignore[arg-type]
    write_rollout(home / "sessions" / "two.jsonl", rollout_events("second-id"))

    overview = session_overview(first)
    assert overview["id"] == "fallback-id"
    assert overview["collaborationMode"] == "plan"
    assert resolve_session("latest", False).exists()
    assert resolve_session("one", False) == first
    assert list_session_overviews(limit=1, include_archived=False)
    assert list_session_overviews(query="does-not-match", include_archived=False) == []


def test_projection_defensive_and_completion_events(tmp_path: Path) -> None:
    events: list[dict[str, object]] = [
        {"timestamp": "2026-08-14T00:00:00Z", "type": "session_meta", "payload": {}},
        {"timestamp": "2026-08-14T00:00:00Z", "type": "turn_context", "payload": {}},
        {"timestamp": "2026-08-14T00:00:00Z", "type": "ignored", "payload": []},
        {
            "timestamp": "2026-08-14T00:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": 5},
        },
        {
            "timestamp": "2026-08-14T00:00:02Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "model_context_window": "unknown"},
        },
        {
            "timestamp": "2026-08-14T00:00:03Z",
            "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": []},
        },
        {
            "timestamp": "2026-08-14T00:00:04Z",
            "type": "response_item",
            "payload": {"type": "reasoning", "summary": None},
        },
        {
            "timestamp": "2026-08-14T00:00:05Z",
            "type": "response_item",
            "payload": {"type": "function_call", "call_id": "pending", "name": "tool"},
        },
        {
            "timestamp": "2026-08-14T00:00:06Z",
            "type": "event_msg",
            "payload": {
                "type": "mcp_tool_call_end",
                "call_id": "pending",
                "success": False,
                "duration": 1.5,
                "result": {"message": "completion detail"},
            },
        },
        {
            "timestamp": "2026-08-14T00:00:07Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "orphan",
                "output": "orphan output",
            },
        },
        {
            "timestamp": "2026-08-14T00:00:08Z",
            "type": "event_msg",
            "payload": {"type": "context_compacted"},
        },
        {
            "timestamp": "2026-08-14T00:00:09Z",
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {}},
        },
    ]
    result = parse_session(
        write_rollout(tmp_path / "defensive.jsonl", events),
        500,
        detail_level="full",  # type: ignore[arg-type]
    )
    pending = next(record for record in result["records"] if record["callId"] == "pending")
    orphan = next(record for record in result["records"] if record["callId"] == "orphan")

    assert result["session"]["id"] == "defensive"
    assert result["session"]["title"] == "Untitled Codex task"
    assert result["turns"][0]["status"] == "running"
    assert pending["status"] == "error"
    assert pending["durationMs"] == 1500
    assert pending["output"] and "completion detail" in pending["output"]
    assert orphan["event"] == "Tool result"
    assert result["stats"]["compactions"] == 1

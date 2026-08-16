"""Behavior and privacy tests for rollout projection."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from codex_trajectory.json_support import MAX_JSON_NESTING_DEPTH, strict_json_loads
from codex_trajectory.privacy import (
    bounded,
    content_text,
    display_path,
    json_text,
    reasoning_summary,
    safe_git,
    shorten,
    source_kind,
)
from codex_trajectory.projection import (
    duration_milliseconds,
    epoch_milliseconds,
    iso_timestamp,
    list_session_overviews,
    merge_token_usage,
    numeric_token_usage,
    output_is_error,
    parse_session,
    parse_timestamp,
    resolve_session,
    session_overview,
    trajectory_result,
)
from conftest import rollout_events, write_rollout
from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_PATH = Path(__file__).parents[3] / "schemas" / "trajectory-v1.schema.json"
TRAJECTORY_VALIDATOR = Draft202012Validator(
    json.loads(SCHEMA_PATH.read_text(encoding="utf-8")),
    format_checker=FormatChecker(),
)


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
    assert result["turns"][0]["model"] == "gpt-test"
    assert result["turns"][0]["timeToFirstTokenMs"] == 900
    assert result["turns"][0]["modelCalls"] == 1
    assert result["turns"][0]["usage"] == {
        "input_tokens": 100,
        "output_tokens": 10,
        "total_tokens": 110,
    }
    assert result["turns"][1]["status"] == "aborted"
    assert result["turns"][1]["error"] == "Turn was aborted."
    assert result["turns"][1]["model"] == "gpt-test"
    assert result["turns"][1]["modelCalls"] == 0
    assert result["turns"][1]["usage"] is None
    assistant = next(record for record in result["records"] if record["id"] == "message-1")
    assert assistant["usage"] == result["turns"][0]["usage"]
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
    TRAJECTORY_VALIDATOR.validate(result)


def test_turns_capture_model_changes(tmp_path: Path) -> None:
    events = rollout_events()
    second_user = next(
        index
        for index, event in enumerate(events)
        if event.get("payload", {}).get("message") == "Try failure"
    )
    events.insert(
        second_user,
        {
            "timestamp": "2026-08-14T00:00:06.500Z",
            "type": "turn_context",
            "payload": {"model": "gpt-test-next", "effort": "high"},
        },
    )

    result = parse_session(write_rollout(tmp_path / "model-change.jsonl", events))

    assert [turn["model"] for turn in result["turns"]] == ["gpt-test", "gpt-test-next"]
    assert result["session"]["model"] == "gpt-test-next"


def test_tool_only_model_response_receives_usage(tmp_path: Path) -> None:
    events: list[dict[str, object]] = [
        {
            "timestamp": "2026-08-14T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "task_started"},
        },
        {
            "timestamp": "2026-08-14T00:00:01Z",
            "type": "response_item",
            "payload": {"type": "function_call", "call_id": "only-tool", "name": "exec"},
        },
        {
            "timestamp": "2026-08-14T00:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"input_tokens": 20, "output_tokens": 4},
                    "last_token_usage": {"input_tokens": 20, "output_tokens": 4},
                },
            },
        },
        {
            "timestamp": "2026-08-14T00:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "only-tool",
                "output": "done",
            },
        },
    ]

    result = parse_session(write_rollout(tmp_path / "tool-only.jsonl", events))
    tool = result["records"][0]

    assert tool["usage"] == {"input_tokens": 20, "output_tokens": 4}


def test_duplicate_and_initial_zero_token_snapshots_are_not_counted(tmp_path: Path) -> None:
    events: list[dict[str, object]] = [
        {
            "timestamp": "2026-08-14T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "task_started"},
        },
        {
            "timestamp": "2026-08-14T00:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "stale-target",
                "role": "assistant",
                "content": [{"text": "starting"}],
            },
        },
        {
            "timestamp": "2026-08-14T00:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"input_tokens": 0, "output_tokens": 0},
                    "last_token_usage": {"input_tokens": 99, "output_tokens": 9},
                },
            },
        },
        {
            "timestamp": "2026-08-14T00:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "accepted-target",
                "role": "assistant",
                "content": [{"text": "done"}],
            },
        },
        {
            "timestamp": "2026-08-14T00:00:04Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"input_tokens": 20, "output_tokens": 4},
                    "last_token_usage": {"input_tokens": 20, "output_tokens": 4},
                },
            },
        },
        {
            "timestamp": "2026-08-14T00:00:05Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"input_tokens": 20, "output_tokens": 4},
                    "last_token_usage": {"input_tokens": 20, "output_tokens": 4},
                },
            },
        },
    ]

    result = parse_session(write_rollout(tmp_path / "duplicate-tokens.jsonl", events))
    records = {record["id"]: record for record in result["records"]}

    assert result["stats"]["tokens"] == {"input_tokens": 20, "output_tokens": 4}
    assert result["turns"][0]["modelCalls"] == 1
    assert result["turns"][0]["usage"] == {"input_tokens": 20, "output_tokens": 4}
    assert records["stale-target"]["usage"] is None
    assert records["accepted-target"]["usage"] == {"input_tokens": 20, "output_tokens": 4}


def test_token_usage_without_cumulative_snapshot_keeps_legacy_behavior(tmp_path: Path) -> None:
    events: list[dict[str, object]] = [
        {
            "timestamp": "2026-08-14T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "task_started"},
        },
        {
            "timestamp": "2026-08-14T00:00:01Z",
            "type": "response_item",
            "payload": {"type": "message", "role": "assistant", "content": []},
        },
        {
            "timestamp": "2026-08-14T00:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"last_token_usage": {"input_tokens": 7, "output_tokens": 2}},
            },
        },
    ]

    result = parse_session(write_rollout(tmp_path / "legacy-tokens.jsonl", events))

    assert result["turns"][0]["modelCalls"] == 1
    assert result["turns"][0]["usage"] == {"input_tokens": 7, "output_tokens": 2}


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
    assert result["pagination"] == {
        "firstRecord": result["records"][0]["index"],
        "lastRecord": result["stats"]["records"],
        "earlierRecords": result["records"][0]["index"] - 1,
        "laterRecords": 0,
        "hasEarlier": True,
        "hasLater": False,
        "nextBeforeRecord": result["records"][0]["index"],
    }


def test_record_pages_are_adjacent_stable_and_keep_aggregate_stats(tmp_path: Path) -> None:
    events = rollout_events()
    for index in range(140):
        events.insert(
            -1,
            {
                "timestamp": f"2026-08-14T00:02:{index % 60:02d}.000Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": f"page-{index}",
                    "role": "assistant",
                    "content": [{"text": f"page record {index}"}],
                },
            },
        )
    path = write_rollout(tmp_path / "paged.jsonl", events)

    newest = parse_session(path, max_records=50)
    middle = parse_session(
        path,
        max_records=50,
        before_record=newest["pagination"]["nextBeforeRecord"],
    )
    oldest = parse_session(
        path,
        max_records=50,
        before_record=middle["pagination"]["nextBeforeRecord"],
    )

    newest_indexes = [record["index"] for record in newest["records"]]
    middle_indexes = [record["index"] for record in middle["records"]]
    oldest_indexes = [record["index"] for record in oldest["records"]]
    assert middle_indexes[-1] + 1 == newest_indexes[0]
    assert oldest_indexes[-1] + 1 == middle_indexes[0]
    assert len(set(newest_indexes + middle_indexes + oldest_indexes)) == len(
        newest_indexes + middle_indexes + oldest_indexes
    )
    for key in (
        "turns",
        "records",
        "toolCalls",
        "failedTools",
        "compactions",
        "tokens",
        "contextWindow",
    ):
        assert newest["stats"][key] == middle["stats"][key] == oldest["stats"][key]
    assert middle["pagination"]["hasEarlier"] is True
    assert middle["pagination"]["hasLater"] is True
    assert middle["pagination"]["laterRecords"] == len(newest_indexes)
    assert all(
        any(turn["index"] == record["turn"] for turn in page["turns"])
        for page in (newest, middle, oldest)
        for record in page["records"]
    )
    TRAJECTORY_VALIDATOR.validate(newest)
    TRAJECTORY_VALIDATOR.validate(middle)
    TRAJECTORY_VALIDATOR.validate(oldest)


def test_before_first_record_returns_an_empty_bounded_page(tmp_path: Path) -> None:
    result = parse_session(write_rollout(tmp_path / "empty-page.jsonl"), before_record=1)

    assert result["records"] == []
    assert result["stats"]["visibleRecords"] == 0
    assert result["stats"]["omittedRecords"] == result["stats"]["records"]
    assert result["pagination"] == {
        "firstRecord": None,
        "lastRecord": None,
        "earlierRecords": 0,
        "laterRecords": result["stats"]["records"],
        "hasEarlier": False,
        "hasLater": True,
        "nextBeforeRecord": None,
    }
    TRAJECTORY_VALIDATOR.validate(result)


def test_tool_correlation_keeps_indexes_stable_across_page_sizes(tmp_path: Path) -> None:
    events = rollout_events()
    output_position = next(
        index
        for index, event in enumerate(events)
        if event.get("payload", {}).get("type") == "function_call_output"
    )
    for index in range(60):
        events.insert(
            output_position + index,
            {
                "timestamp": f"2026-08-14T00:00:{index % 60:02d}.500Z",
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "id": f"between-call-and-output-{index}",
                    "role": "assistant",
                    "content": [{"text": str(index)}],
                },
            },
        )
    path = write_rollout(tmp_path / "tool-across-page.jsonl", events)

    bounded = parse_session(path, max_records=50)
    complete = parse_session(path, max_records=200)
    earlier = parse_session(
        path,
        max_records=50,
        before_record=bounded["pagination"]["nextBeforeRecord"],
    )

    assert bounded["stats"]["records"] == complete["stats"]["records"]
    assert bounded["stats"]["toolCalls"] == complete["stats"]["toolCalls"]
    assert [record["index"] for record in bounded["records"]] == [
        record["index"] for record in complete["records"][-50:]
    ]
    assert [record["id"] for record in bounded["records"]] == [
        record["id"] for record in complete["records"][-50:]
    ]
    expected_earlier = [
        record for record in complete["records"] if record["index"] < bounded["records"][0]["index"]
    ][-50:]
    assert [record["index"] for record in earlier["records"]] == [
        record["index"] for record in expected_earlier
    ]
    assert earlier["stats"]["records"] == complete["stats"]["records"]


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
    assert resolve_session(" latest ", False).name == "rollout-alpha.jsonl"
    assert resolve_session("session-a", False).name == "rollout-alpha.jsonl"
    assert resolve_session("session-archive", True).name == "rollout-archive.jsonl"
    write_rollout(
        codex_home / "sessions" / "rollout-another.jsonl", rollout_events("session-alpine")
    )
    with pytest.raises(ValueError, match="ambiguous"):
        resolve_session("session-a", False)
    with pytest.raises(ValueError, match="not found"):
        resolve_session("missing", True)
    with pytest.raises(ValueError, match="not a filesystem path"):
        resolve_session("../session-alpha", True)
    with pytest.raises(ValueError, match="not a filesystem path"):
        resolve_session(r"..\session-alpha", True)


def test_resolve_exact_identifier_stops_scanning(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def counted_overview(path: Path) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return session_overview(path)

    monkeypatch.setattr("codex_trajectory.projection.session_overview", counted_overview)

    assert resolve_session("session-alpha", False).name == "rollout-alpha.jsonl"
    assert calls == 1


def test_session_overview_cache_invalidates_after_append(tmp_path: Path) -> None:
    path = write_rollout(tmp_path / "cached.jsonl", rollout_events("cached-session"))
    first = session_overview(path)
    cached = session_overview(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "timestamp": "2026-08-14T00:00:12Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started"},
                }
            )
            + "\n"
        )
    refreshed = session_overview(path)

    assert cached == first
    assert refreshed["turns"] == first["turns"] + 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("2026-08-14T00:00:00Z", 1786665600000),
        ("2026-08-14T00:00:00", 1786665600000),
        (1_786_665_600, 1786665600000),
        (None, None),
    ],
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
    assert json_text('{"a":1,"a":2}') == '{"a":1,"a":2}'
    assert json_text('{"value":NaN}') == '{"value":NaN}'
    oversized_integer = '{"value":' + "9" * 257 + "}"
    assert json_text(oversized_integer) == oversized_integer
    assert json_text('{"value":1e400}') == '{"value":1e400}'
    deeply_nested = "[" * 2_000 + "0" + "]" * 2_000
    assert json_text(deeply_nested) == deeply_nested
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
    assert display_path(r"C:\Users\alice\private-project") == "<absolute>/private-project"
    assert display_path(r"\\server\share\secret-project") == "<absolute>/secret-project"
    assert source_kind({1: "invalid"}) is None
    assert source_kind({"subagent": {"private": "payload"}}) == "subagent"
    assert safe_git(None) is None
    assert safe_git({"repository_url": "secret"}) is None
    assert output_is_error('{"success":false}')
    assert not output_is_error("plain output")
    assert not output_is_error("[]")
    assert not output_is_error('{"isError":true,"isError":false}')
    assert not output_is_error('{"value":NaN}')
    assert not output_is_error(oversized_integer)
    assert not output_is_error('{"value":1e400}')
    assert not output_is_error("[" * 2_000 + "0" + "]" * 2_000)
    assert not output_is_error({"Ok": {"Ok": {"Ok": {"Ok": {"Ok": {"Ok": {"Ok": {"Ok": {}}}}}}}}})
    assert iso_timestamp(None) is None
    assert parse_timestamp("not-a-time") is None
    assert parse_timestamp(1_786_665_600_000) == 1_786_665_600_000
    assert merge_token_usage(None, {"input_tokens": 2, "ignored": {}, "flag": True}) == {
        "input_tokens": 2
    }
    assert merge_token_usage({"input_tokens": 2}, {"input_tokens": 3}) == {"input_tokens": 5}
    assert numeric_token_usage({"input_tokens": 2, "flag": True, "nested": {}}) == {
        "input_tokens": 2
    }
    assert numeric_token_usage({"cache_write_input_tokens": 7}) == {}
    assert numeric_token_usage(None) == {}
    assert numeric_token_usage({"input_tokens": 2**53}) == {}
    assert merge_token_usage({"input_tokens": 2**53 - 2}, {"input_tokens": 2}) == {
        "input_tokens": 2**53 - 2
    }


def test_strict_json_nesting_is_bounded_and_ignores_string_contents() -> None:
    string_value = "[{" * (MAX_JSON_NESTING_DEPTH + 1) + '\\"'
    assert strict_json_loads(json.dumps({"value": string_value})) == {"value": string_value}
    at_limit = "[" * MAX_JSON_NESTING_DEPTH + "0" + "]" * MAX_JSON_NESTING_DEPTH
    assert strict_json_loads(at_limit)
    over_limit = "[" * (MAX_JSON_NESTING_DEPTH + 1) + "0" + "]" * (MAX_JSON_NESTING_DEPTH + 1)
    with pytest.raises(ValueError, match="nesting"):
        strict_json_loads(over_limit)


def test_display_path_fails_closed_on_resolution_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "private-project"
    relative = Path("relative-project")
    original_resolve = Path.resolve

    def fail_for_secret(path: Path, strict: bool = False) -> Path:
        if path in {secret, relative}:
            raise OSError("resolution failed")
        return original_resolve(path, strict=strict)

    monkeypatch.setattr(Path, "resolve", fail_for_secret)

    assert display_path(str(secret)) == "<absolute>/private-project"
    assert display_path(str(relative)) == "relative-project"


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
    with pytest.raises(ValueError, match="at most 500"):
        list_session_overviews(query="x" * 501, include_archived=False)
    assert resolve_session(None, False).exists()
    with pytest.raises(ValueError, match="at most 240"):
        resolve_session("x" * 241, False)


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
    assert pending["event"] == "MCP tool"
    assert pending["durationMs"] == 1500
    assert pending["output"] and "completion detail" in pending["output"]
    assert orphan["event"] == "Tool result"
    assert result["stats"]["compactions"] == 1


def test_first_metadata_owns_identity_and_structured_metadata_stays_private(tmp_path: Path) -> None:
    events: list[dict[str, object]] = [
        {
            "timestamp": "2026-08-16T00:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": "child-thread",
                "session_id": "shared-session",
                "cwd": str(Path.home() / "child-project"),
                "originator": {"secret-originator": "do-not-return"},
                "source": {"subagent": {"secret-source": "do-not-return"}},
                "parent_thread_id": {"secret-parent": "do-not-return"},
                "agent_path": ["secret-agent-path"],
            },
        },
        {
            "timestamp": "2026-08-16T00:00:00Z",
            "type": "session_meta",
            "payload": {
                "id": "ancestor-thread",
                "session_id": "shared-session",
                "cwd": "/private/ancestor-project",
            },
        },
        {
            "timestamp": "2026-08-16T00:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "child task"},
        },
        {
            "timestamp": "2026-08-16T00:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 5,
                        "secret-counter-name": 99,
                        "nested-secret": {"value": 1},
                        "output_tokens": "not-a-number",
                    }
                },
            },
        },
    ]
    path = write_rollout(tmp_path / "identity.jsonl", events)

    result = parse_session(path)
    overview = session_overview(path)
    serialized = json.dumps(result)

    assert result["session"]["id"] == "child-thread"
    assert overview["id"] == "child-thread"
    assert result["session"]["cwd"] == "~/child-project"
    assert result["session"]["originator"] is None
    assert result["session"]["sourceKind"] == "subagent"
    assert result["session"]["parentThreadId"] is None
    assert result["session"]["agentPath"] is None
    assert result["stats"]["tokens"] == {"input_tokens": 5}
    assert "secret-" not in serialized
    assert "ancestor-project" not in serialized


def test_turn_complete_error_is_a_failed_turn_without_summary_leak(tmp_path: Path) -> None:
    secret = "credential=do-not-return"
    events: list[dict[str, object]] = [
        {
            "timestamp": "2026-08-16T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "failed-turn"},
        },
        {
            "timestamp": "2026-08-16T00:00:01Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "failed-turn",
                "error": {"message": secret, "codex_error_info": {"kind": "internal"}},
            },
        },
    ]
    path = write_rollout(tmp_path / "turn-error.jsonl", events)

    summary = parse_session(path)
    full = parse_session(path, detail_level="full")

    assert summary["turns"][0]["status"] == "error"
    assert summary["turns"][0]["error"] == "Turn completed with an error."
    assert secret not in json.dumps(summary)
    assert full["turns"][0]["error"] == secret


def test_malformed_attachment_fields_and_nonfinite_timestamps_are_rejected(tmp_path: Path) -> None:
    events: list[dict[str, object]] = [
        {
            "timestamp": "2026-08-16T00:00:00Z",
            "type": "event_msg",
            "payload": {
                "type": "user_message",
                "message": "attachments",
                "images": 7,
                "audio": {"not": "an array"},
            },
        }
    ]
    result = parse_session(
        write_rollout(tmp_path / "attachments.jsonl", events), detail_level="full"
    )

    assert result["records"][0]["metadata"] == {"images": 0, "audio": 0}
    assert parse_timestamp(True) is None
    assert parse_timestamp(float("nan")) is None
    assert parse_timestamp(float("inf")) is None
    assert epoch_milliseconds(True) is None
    assert duration_milliseconds({"secs": 1, "nanos": 250_000_000}) == 1250
    assert duration_milliseconds(10_000) == 10_000_000
    assert duration_milliseconds({"secs": -1, "nanos": 0}) is None
    assert iso_timestamp(10**30) is None


def test_legacy_completion_only_tools_are_projected_from_official_event_shapes(
    tmp_path: Path,
) -> None:
    events: list[dict[str, object]] = [
        {
            "timestamp": "2026-08-16T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "legacy-tools"},
        },
        {
            "timestamp": "2026-08-16T00:00:01Z",
            "type": "event_msg",
            "payload": {
                "type": "mcp_tool_call_end",
                "call_id": "mcp-1",
                "invocation": {
                    "server": "files",
                    "tool": "read",
                    "arguments": {"secret-input": "hidden"},
                },
                "duration": {"secs": 1, "nanos": 250_000_000},
                "result": {"Ok": {"content": [], "isError": True}},
            },
        },
        {
            "timestamp": "2026-08-16T00:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "web_search_end",
                "call_id": "web-1",
                "query": "secret query",
                "action": {"type": "search", "query": "secret query"},
                "results": [{"secret-result": "hidden"}],
            },
        },
        {
            "timestamp": "2026-08-16T00:00:03Z",
            "type": "event_msg",
            "payload": {
                "type": "patch_apply_end",
                "call_id": "patch-1",
                "success": True,
                "status": "completed",
                "changes": {"file.py": {"type": "update"}},
                "stdout": "Done",
                "stderr": "",
            },
        },
        {
            "timestamp": "2026-08-16T00:00:04Z",
            "type": "event_msg",
            "payload": {
                "type": "image_generation_end",
                "call_id": "image-1",
                "status": "failed",
                "result": "secret image result",
                "failure": {"message": "expected"},
            },
        },
        {
            "timestamp": "2026-08-16T00:00:05Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "legacy-tools"},
        },
    ]
    path = write_rollout(tmp_path / "legacy-tools.jsonl", events)

    summary = parse_session(path)
    full = parse_session(path, detail_level="full")
    records = {record["callId"]: record for record in full["records"]}

    assert summary["stats"]["toolCalls"] == 4
    assert summary["stats"]["failedTools"] == 2
    assert summary["turns"][0]["steps"] == 1
    assert records["mcp-1"]["event"] == "files.read"
    assert records["mcp-1"]["status"] == "error"
    assert records["mcp-1"]["durationMs"] == 1250
    assert records["web-1"]["event"] == "Web search · search"
    assert records["patch-1"]["status"] == "complete"
    assert records["image-1"]["status"] == "error"
    serialized_summary = json.dumps(summary)
    assert "secret query" not in serialized_summary
    assert "secret-input" not in serialized_summary
    assert "secret-result" not in serialized_summary


def _paginated_rollout(path: Path, items: list[dict[str, object]]) -> Path:
    thread_id = "12345678-1234-4234-8234-123456789abc"
    lines: list[dict[str, object]] = [
        {
            "timestamp": "2026-08-16T00:00:00Z",
            "ordinal": 0,
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "session_id": "shared-session",
                "history_mode": "paginated",
            },
        },
        {
            "timestamp": "2026-08-16T00:00:01Z",
            "ordinal": 1,
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "paginated-turn"},
        },
    ]
    for offset, item in enumerate(items, 2):
        lines.append(
            {
                "timestamp": f"2026-08-16T00:00:{offset:02d}Z",
                "ordinal": offset,
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "turn_id": "paginated-turn",
                    "started_at_ms": 1_786_665_600_000 + offset * 1000,
                    "completed_at_ms": 1_786_665_600_500 + offset * 1000,
                    "item": item,
                },
            }
        )
    lines.extend(
        [
            {
                "timestamp": "2026-08-16T00:01:00Z",
                "ordinal": len(lines),
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"text": "duplicate raw response"}],
                },
            },
            {
                "timestamp": "2026-08-16T00:01:01Z",
                "ordinal": len(lines) + 1,
                "type": "event_msg",
                "payload": {"type": "task_complete", "turn_id": "paginated-turn"},
            },
        ]
    )
    return write_rollout(path, lines)


def test_paginated_turn_item_protocol_matrix_has_no_raw_response_duplicates(tmp_path: Path) -> None:
    items: list[dict[str, object]] = [
        {
            "type": "UserMessage",
            "id": "user",
            "content": [
                {"type": "text", "text": "hello"},
                {"type": "local_image", "path": "/secret/image.png"},
                {"type": "audio", "audio_url": "secret-audio"},
            ],
        },
        {
            "type": "HookPrompt",
            "id": "hook",
            "fragments": [{"text": "secret hook", "hookRunId": "hook-1"}],
        },
        {
            "type": "AgentMessage",
            "id": "assistant",
            "content": [{"type": "Text", "text": "answer"}],
            "phase": "commentary",
        },
        {"type": "Plan", "id": "plan", "text": "plan text"},
        {
            "type": "Reasoning",
            "id": "reasoning",
            "summary_text": ["safe summary"],
            "raw_content": ["raw-secret-reasoning"],
        },
        {
            "type": "CommandExecution",
            "id": "command",
            "command": ["pwd"],
            "cwd": "/secret/project",
            "source": "agent",
            "status": "completed",
            "stdout": "ok",
            "duration": {"secs": 0, "nanos": 500_000_000},
        },
        {
            "type": "DynamicToolCall",
            "id": "dynamic",
            "namespace": "demo",
            "tool": "lookup",
            "arguments": {"q": "secret"},
            "status": "failed",
            "success": False,
            "error": "expected",
        },
        {
            "type": "CollabAgentToolCall",
            "id": "collab",
            "tool": "spawn_agent",
            "status": "completed",
            "receiver_thread_ids": [],
            "agents_states": {},
        },
        {
            "type": "SubAgentActivity",
            "id": "activity",
            "kind": "started",
            "agent_thread_id": "agent-thread",
            "agent_path": "/root/reviewer",
        },
        {
            "type": "WebSearch",
            "id": "web",
            "query": "secret search",
            "action": {"type": "search", "query": "secret search"},
            "results": [],
        },
        {"type": "ImageView", "id": "view", "path": "/secret/image.png"},
        {"type": "Extension", "kind": "clock.sleep", "id": "sleep", "durationMs": 1000},
        {
            "type": "Extension",
            "kind": "web.search",
            "id": "extension-web",
            "query": "secret extension search",
            "action": {"type": "search"},
            "results": [],
        },
        {
            "type": "Extension",
            "kind": "image_gen.generation",
            "id": "extension-image",
            "status": "completed",
            "revisedPrompt": "secret prompt",
            "result": "secret result",
        },
        {
            "type": "ImageGeneration",
            "id": "image",
            "status": "completed",
            "result": "secret image",
        },
        {
            "type": "EnteredReviewMode",
            "id": "review-in",
            "target": {"type": "uncommittedChanges"},
            "user_facing_hint": "Reviewing local changes",
        },
        {"type": "ExitedReviewMode", "id": "review-out", "review_output": None},
        {
            "type": "FileChange",
            "id": "patch",
            "changes": {"file.py": {"type": "update"}},
            "status": "failed",
            "stderr": "expected",
        },
        {
            "type": "McpToolCall",
            "id": "mcp",
            "server": "demo",
            "tool": "read",
            "arguments": {"secret": "input"},
            "status": "completed",
            "result": {"content": [], "isError": True},
            "duration": {"secs": 2, "nanos": 0},
        },
        {"type": "ContextCompaction", "id": "compact"},
    ]
    path = _paginated_rollout(
        tmp_path / "rollout-2026-08-16T00-00-00-12345678-1234-4234-8234-123456789abc.jsonl",
        items,
    )

    result = parse_session(path)
    full_result = parse_session(path, detail_level="full")

    assert result["session"]["id"] == "12345678-1234-4234-8234-123456789abc"
    assert result["stats"]["records"] == 20
    assert result["stats"]["toolCalls"] == 11
    assert result["stats"]["failedTools"] == 3
    assert result["stats"]["compactions"] == 1
    assert len(result["records"]) == 20
    assert "duplicate raw response" not in json.dumps(result)
    assert "raw-secret-reasoning" not in json.dumps(result)
    assert (
        next(record for record in result["records"] if record["id"] == "command")["durationMs"]
        == 500
    )
    assert (
        next(record for record in result["records"] if record["id"] == "mcp")["durationMs"] == 2000
    )
    assert (
        next(record for record in result["records"] if record["id"] == "user")["durationMs"] == 500
    )
    assistant = next(record for record in full_result["records"] if record["id"] == "assistant")
    review = next(record for record in full_result["records"] if record["id"] == "review-in")
    assert assistant["event"] == "Assistant · commentary"
    assert assistant["metadata"] == {"phase": "commentary"}
    assert "Reviewing local changes" in review["output"]


def test_legacy_review_mode_events_are_projected_without_summary_leaks(tmp_path: Path) -> None:
    events = [
        {
            "timestamp": "2026-08-16T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "turn_started", "turn_id": "review-turn"},
        },
        {
            "timestamp": "2026-08-16T00:00:01Z",
            "type": "event_msg",
            "payload": {
                "type": "entered_review_mode",
                "turn_id": "review-turn",
                "item_id": "review-in",
                "target": {"type": "uncommitted_changes"},
                "user_facing_hint": "sensitive review hint",
            },
        },
        {
            "timestamp": "2026-08-16T00:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "exited_review_mode",
                "turn_id": "review-turn",
                "item_id": "review-out",
                "review_output": {"notes": "sensitive review output"},
            },
        },
        {
            "timestamp": "2026-08-16T00:00:03Z",
            "type": "event_msg",
            "payload": {"type": "turn_complete", "turn_id": "review-turn"},
        },
    ]
    path = write_rollout(tmp_path / "legacy-review.jsonl", events)

    summary = parse_session(path)
    full = parse_session(path, detail_level="full")
    overview = session_overview(path)

    assert [record["summary"] for record in summary["records"]] == [
        "Entered review mode",
        "Exited review mode",
    ]
    assert "sensitive review" not in json.dumps(summary)
    assert "sensitive review hint" in full["records"][0]["output"]
    assert "sensitive review output" in full["records"][1]["output"]
    assert overview["turns"] == summary["stats"]["turns"] == 1


def test_paginated_raw_agent_message_is_preserved_as_inter_agent_communication(
    tmp_path: Path,
) -> None:
    thread_id = "65656565-6565-4565-8565-656565656565"
    events: list[dict[str, object]] = [
        {
            "timestamp": "2026-08-16T00:00:00Z",
            "ordinal": 0,
            "type": "session_meta",
            "payload": {"id": thread_id, "history_mode": "paginated"},
        },
        {
            "timestamp": "2026-08-16T00:00:01Z",
            "ordinal": 1,
            "type": "event_msg",
            "payload": {"type": "turn_started", "turn_id": "communication-turn"},
        },
        {
            "timestamp": "2026-08-16T00:00:02Z",
            "ordinal": 2,
            "type": "response_item",
            "payload": {
                "type": "agent_message",
                "id": "agent-message",
                "author": "worker",
                "recipient": "root",
                "content": [{"type": "input_text", "text": "child finished"}],
                "internal_chat_message_metadata_passthrough": {"turn_id": "communication-turn"},
            },
        },
        {
            "timestamp": "2026-08-16T00:00:03Z",
            "ordinal": 3,
            "type": "event_msg",
            "payload": {"type": "turn_complete", "turn_id": "communication-turn"},
        },
    ]
    path = write_rollout(tmp_path / f"rollout-2026-08-16T00-00-00-{thread_id}.jsonl", events)

    result = parse_session(path, detail_level="full")
    overview = session_overview(path)

    assert [(record["kind"], record["summary"]) for record in result["records"]] == [
        ("subagent", "child finished")
    ]
    assert result["records"][0]["metadata"] == {
        "author": "worker",
        "recipient": "root",
    }
    assert result["turns"][0]["id"] == "communication-turn"
    assert overview["turns"] == result["stats"]["turns"] == 1


def test_paginated_compaction_checkpoint_and_completed_item_are_counted_once(
    tmp_path: Path,
) -> None:
    thread_id = "12345678-1234-4234-8234-123456789abc"
    events: list[dict[str, object]] = [
        {
            "timestamp": "2026-08-16T00:00:00Z",
            "ordinal": 0,
            "type": "session_meta",
            "payload": {"id": thread_id, "history_mode": "paginated"},
        },
        {
            "timestamp": "2026-08-16T00:00:01Z",
            "ordinal": 1,
            "type": "event_msg",
            "payload": {
                "type": "turn_started",
                "turn_id": "turn-one",
                "collaboration_mode_kind": "default",
            },
        },
        {
            "timestamp": "2026-08-16T00:00:02Z",
            "ordinal": 2,
            "type": "compacted",
            "payload": {"message": "private replacement checkpoint"},
        },
        {
            "timestamp": "2026-08-16T00:00:03Z",
            "ordinal": 3,
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "turn-one",
                "started_at_ms": 1_786_665_602_000,
                "completed_at_ms": 1_786_665_603_000,
                "item": {"type": "ContextCompaction", "id": "compact-one"},
            },
        },
        {
            "timestamp": "2026-08-16T00:00:04Z",
            "ordinal": 4,
            "type": "event_msg",
            "payload": {"type": "turn_complete", "turn_id": "turn-one"},
        },
    ]
    path = write_rollout(tmp_path / f"rollout-2026-08-16T00-00-00-{thread_id}.jsonl", events)

    result = parse_session(path, detail_level="full")
    summary = parse_session(path)
    overview = session_overview(path)

    assert result["stats"]["compactions"] == 1
    assert [record["id"] for record in result["records"]] == ["compact-one"]
    assert "private replacement checkpoint" not in json.dumps(summary)
    assert overview["turns"] == 1
    assert overview["collaborationMode"] == "default"


def test_overview_deduplicates_legacy_start_and_terminal_tool_records(tmp_path: Path) -> None:
    events: list[dict[str, object]] = [
        {
            "timestamp": "2026-08-16T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "turn_started", "turn_id": "turn-one"},
        },
        {
            "timestamp": "2026-08-16T00:00:01Z",
            "type": "response_item",
            "payload": {"type": "function_call", "call_id": "call-one", "name": "demo"},
        },
        {
            "timestamp": "2026-08-16T00:00:02Z",
            "type": "event_msg",
            "payload": {"type": "mcp_tool_call_end", "call_id": "call-one", "result": {}},
        },
        {
            "timestamp": "2026-08-16T00:00:03Z",
            "type": "event_msg",
            "payload": {"type": "turn_complete", "turn_id": "turn-one"},
        },
    ]
    path = write_rollout(tmp_path / "overview-dedup.jsonl", events)

    result = parse_session(path)
    overview = session_overview(path)

    assert overview["turns"] == result["stats"]["turns"] == 1
    assert overview["toolCalls"] == result["stats"]["toolCalls"] == 1


def test_completion_without_start_preserves_turn_identity_and_mismatch_splits(
    tmp_path: Path,
) -> None:
    completion_only = write_rollout(
        tmp_path / "completion-only.jsonl",
        [
            {
                "timestamp": "2026-08-16T00:00:00Z",
                "type": "event_msg",
                "payload": {"type": "turn_complete", "turn_id": "completed-turn"},
            }
        ],
    )
    assert parse_session(completion_only)["turns"][0]["id"] == "completed-turn"

    mismatched = write_rollout(
        tmp_path / "mismatched-turns.jsonl",
        [
            {
                "timestamp": "2026-08-16T00:00:00Z",
                "type": "event_msg",
                "payload": {"type": "turn_started", "turn_id": "first"},
            },
            {
                "timestamp": "2026-08-16T00:00:01Z",
                "type": "event_msg",
                "payload": {"type": "turn_complete", "turn_id": "second"},
            },
        ],
    )
    result = parse_session(mismatched)

    assert [(turn["id"], turn["status"]) for turn in result["turns"]] == [
        ("first", "aborted"),
        ("second", "complete"),
    ]
    assert result["warnings"][0]["code"] == "mismatched_turn_completion"


def test_thread_rollback_keeps_historical_records_but_is_not_silent(tmp_path: Path) -> None:
    events = [
        {
            "timestamp": "2026-08-16T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "turn_started", "turn_id": "rolled-back"},
        },
        {
            "timestamp": "2026-08-16T00:00:01Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "historical request"},
        },
        {
            "timestamp": "2026-08-16T00:00:02Z",
            "type": "event_msg",
            "payload": {"type": "turn_complete", "turn_id": "rolled-back"},
        },
        {
            "timestamp": "2026-08-16T00:00:03Z",
            "type": "event_msg",
            "payload": {"type": "thread_rolled_back", "num_turns": 1},
        },
    ]

    result = parse_session(write_rollout(tmp_path / "rollback.jsonl", events))

    assert result["stats"]["turns"] == 1
    assert result["records"][0]["summary"] == "historical request"
    assert result["warnings"] == [
        {
            "code": "thread_rolled_back",
            "line": 4,
            "message": (
                "Thread history rolled back 1 user turn(s); preceding records remain visible "
                "as historical execution."
            ),
        }
    ]


def test_paginated_item_turn_mismatch_splits_and_preserves_empty_mcp_result(
    tmp_path: Path,
) -> None:
    thread_id = "34343434-3434-4434-8434-343434343434"
    events: list[dict[str, object]] = [
        {
            "timestamp": "2026-08-16T00:00:00Z",
            "ordinal": 0,
            "type": "session_meta",
            "payload": {"id": thread_id, "history_mode": "paginated"},
        },
        {
            "timestamp": "2026-08-16T00:00:01Z",
            "ordinal": 1,
            "type": "event_msg",
            "payload": {"type": "turn_started", "turn_id": "first"},
        },
        {
            "timestamp": "2026-08-16T00:00:02Z",
            "ordinal": 2,
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "second",
                "started_at_ms": 1_786_665_602_000,
                "completed_at_ms": 1_786_665_603_000,
                "item": {
                    "type": "McpToolCall",
                    "id": "empty-result",
                    "server": "demo",
                    "tool": "empty",
                    "status": "completed",
                    "result": {},
                    "error": "must not replace an empty success result",
                },
            },
        },
    ]
    path = write_rollout(
        tmp_path / f"rollout-2026-08-16T00-00-00-{thread_id}.jsonl",
        events,
    )

    result = parse_session(path, detail_level="full")
    overview = session_overview(path)

    assert [(turn["id"], turn["status"]) for turn in result["turns"]] == [
        ("first", "aborted"),
        ("second", "running"),
    ]
    assert result["records"][0]["output"] == "{}"
    assert result["warnings"][0]["code"] == "mismatched_item_turn"
    assert overview["turns"] == result["stats"]["turns"] == 2


def test_paginated_subagent_overview_and_detail_exclude_inherited_prefix(
    tmp_path: Path,
) -> None:
    thread_id = "78787878-7878-4878-8878-787878787878"
    events: list[dict[str, object]] = [
        {
            "timestamp": "2026-08-16T00:00:00Z",
            "ordinal": 0,
            "type": "session_meta",
            "payload": {
                "id": thread_id,
                "timestamp": "2026-08-16T00:00:00Z",
                "history_mode": "paginated",
                "subagent_history_start_ordinal": 4,
            },
        },
        {
            "timestamp": "2026-08-16T00:00:01Z",
            "ordinal": 1,
            "type": "event_msg",
            "payload": {"type": "turn_started", "turn_id": "parent"},
        },
        {
            "timestamp": "2026-08-16T00:00:02Z",
            "ordinal": 2,
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "parent",
                "item": {
                    "type": "UserMessage",
                    "id": "parent-user",
                    "content": [{"type": "text", "text": "private parent context"}],
                },
            },
        },
        {
            "timestamp": "2026-08-16T00:00:03Z",
            "ordinal": 3,
            "type": "event_msg",
            "payload": {"type": "turn_complete", "turn_id": "parent"},
        },
        {
            "timestamp": "2026-08-16T00:00:04Z",
            "ordinal": 4,
            "type": "event_msg",
            "payload": {"type": "turn_started", "turn_id": "child"},
        },
        {
            "timestamp": "2026-08-16T00:00:05Z",
            "ordinal": 5,
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "child",
                "item": {
                    "type": "UserMessage",
                    "id": "child-user",
                    "content": [{"type": "text", "text": "child task"}],
                },
            },
        },
    ]
    path = write_rollout(
        tmp_path / f"rollout-2026-08-16T00-00-00-{thread_id}.jsonl",
        events,
    )

    result = parse_session(path, detail_level="full")
    overview = session_overview(path)

    assert result["stats"]["turns"] == overview["turns"] == 1
    assert result["session"]["title"] == overview["title"] == "child task"
    assert "private parent context" not in json.dumps(result)


def test_turn_completion_uses_the_persisted_start_when_duration_is_absent(
    tmp_path: Path,
) -> None:
    path = write_rollout(
        tmp_path / "turn-duration.jsonl",
        [
            {
                "timestamp": "2026-08-16T00:00:00Z",
                "type": "event_msg",
                "payload": {"type": "turn_started", "turn_id": "turn"},
            },
            {
                "timestamp": "2026-08-16T00:00:02Z",
                "type": "event_msg",
                "payload": {"type": "turn_complete", "turn_id": "turn"},
            },
        ],
    )

    result = parse_session(path)

    assert result["turns"][0]["durationMs"] == 2000


def test_malformed_token_snapshot_does_not_erase_the_last_valid_total(tmp_path: Path) -> None:
    events: list[dict[str, object]] = [
        {
            "timestamp": "2026-08-16T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "turn_started", "turn_id": "turn"},
        },
        {
            "timestamp": "2026-08-16T00:00:01Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"input_tokens": 10},
                    "last_token_usage": {"input_tokens": 10},
                },
            },
        },
        {
            "timestamp": "2026-08-16T00:00:02Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"input_tokens": "invalid"},
                    "last_token_usage": {"input_tokens": 999},
                },
            },
        },
        {
            "timestamp": "2026-08-16T00:00:03Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {"output_tokens": 5},
                    "last_token_usage": {"output_tokens": 5},
                },
            },
        },
    ]
    path = write_rollout(tmp_path / "malformed-token.jsonl", events)

    result = parse_session(path)
    overview = session_overview(path)

    assert result["stats"]["tokens"] == {"input_tokens": 10, "output_tokens": 5}
    assert result["turns"][0]["modelCalls"] == 2
    assert overview["tokens"] == {"input_tokens": 10, "output_tokens": 5}


def test_legacy_response_item_protocol_matrix(tmp_path: Path) -> None:
    events: list[dict[str, object]] = [
        {
            "timestamp": "2026-08-16T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "task_started"},
        },
        {
            "timestamp": "2026-08-16T00:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "local_shell_call",
                "call_id": "shell",
                "status": "completed",
                "action": {"type": "exec", "command": ["pwd"]},
            },
        },
        {
            "timestamp": "2026-08-16T00:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "tool_search_call",
                "call_id": "search-tools",
                "status": "in_progress",
                "execution": "tool_search",
                "arguments": {"query": "docs"},
            },
        },
        {
            "timestamp": "2026-08-16T00:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "tool_search_output",
                "call_id": "search-tools",
                "status": "completed",
                "execution": "tool_search",
                "tools": [],
            },
        },
        {
            "timestamp": "2026-08-16T00:00:04Z",
            "type": "response_item",
            "payload": {
                "type": "web_search_call",
                "id": "hosted-web",
                "status": "completed",
                "action": {"type": "search", "query": "docs"},
            },
        },
        {
            "timestamp": "2026-08-16T00:00:05Z",
            "type": "response_item",
            "payload": {
                "type": "image_generation_call",
                "id": "hosted-image",
                "status": "completed",
                "result": "image data",
            },
        },
        {
            "timestamp": "2026-08-16T00:00:06Z",
            "type": "response_item",
            "payload": {"type": "compaction", "id": "compact-1", "encrypted_content": "secret"},
        },
        {
            "timestamp": "2026-08-16T00:00:07Z",
            "type": "response_item",
            "payload": {"type": "context_compaction", "id": "compact-2"},
        },
        {
            "timestamp": "2026-08-16T00:00:08Z",
            "type": "response_item",
            "payload": {"type": "future_response_item", "private": "ignored"},
        },
    ]
    result = parse_session(write_rollout(tmp_path / "legacy-response-matrix.jsonl", events))

    assert result["stats"]["toolCalls"] == 4
    assert result["stats"]["failedTools"] == 0
    assert result["stats"]["compactions"] == 2
    assert {record["callId"] for record in result["records"] if record["kind"] == "tool"} == {
        "shell",
        "search-tools",
        "hosted-web",
        "hosted-image",
    }
    assert result["warnings"][-1]["code"] == "unsupported_response_item"
    assert "encrypted_content" not in json.dumps(result)


def test_miscellaneous_persisted_events_cover_defensive_projection_paths(
    tmp_path: Path,
) -> None:
    events: list[dict[str, object]] = [
        {
            "timestamp": "2026-08-16T00:00:00Z",
            "type": "session_meta",
            "payload": {"id": "defensive-matrix"},
        },
        {
            "timestamp": "2026-08-16T00:00:01Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "first"},
        },
        {
            "timestamp": "2026-08-16T00:00:02Z",
            "type": "turn_context",
            "payload": {"model": "context-model", "effort": "medium"},
        },
        {
            "timestamp": "2026-08-16T00:00:03Z",
            "type": "event_msg",
            "payload": {
                "type": "task_started",
                "turn_id": "second",
                "model": "turn-model",
                "model_context_window": 64_000,
            },
        },
        {
            "timestamp": "2026-08-16T00:00:04Z",
            "type": "response_item",
            "payload": {
                "type": "tool_search_call",
                "call_id": "search-running",
                "status": "in_progress",
                "arguments": {"query": "docs"},
            },
        },
        {
            "timestamp": "2026-08-16T00:00:05Z",
            "type": "response_item",
            "payload": {
                "type": "tool_search_output",
                "call_id": "unmatched-search",
                "status": "failed",
                "tools": [],
            },
        },
        {
            "timestamp": "2026-08-16T00:00:06Z",
            "type": "event_msg",
            "payload": {"type": "item_completed", "item": []},
        },
        {
            "timestamp": "2026-08-16T00:00:07Z",
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "second",
                "item": {
                    "type": "Extension",
                    "id": "future-extension",
                    "kind": "example.future",
                    "status": "completed",
                },
            },
        },
        {
            "timestamp": "2026-08-16T00:00:08Z",
            "type": "event_msg",
            "payload": {
                "type": "item_completed",
                "turn_id": "second",
                "item": {"type": "FutureTurnItem", "id": "future-item"},
            },
        },
        {
            "timestamp": "2026-08-16T00:00:09Z",
            "type": "event_msg",
            "payload": {"type": "thread_rolled_back", "num_turns": True},
        },
        {
            "timestamp": "2026-08-16T00:00:10Z",
            "type": "event_msg",
            "payload": {"type": "sub_agent_activity", "kind": "completed"},
        },
        {
            "timestamp": "2026-08-16T00:00:11Z",
            "type": "event_msg",
            "payload": {"type": "context_compacted"},
        },
        {
            "timestamp": "2026-08-16T00:00:12Z",
            "type": "inter_agent_communication",
            "payload": {
                "id": "communication",
                "content": "handoff complete",
                "author": "reviewer",
                "recipient": "root",
                "other_recipients": ["observer", 7],
                "trigger_turn": True,
            },
        },
        {
            "timestamp": "2026-08-16T00:00:13Z",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "second"},
        },
        {
            "timestamp": "invalid",
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "no-timing"},
        },
    ]

    result = parse_session(
        write_rollout(tmp_path / "defensive-event-matrix.jsonl", events),
        detail_level="full",
    )

    assert [(turn["id"], turn["status"]) for turn in result["turns"]] == [
        ("first", "aborted"),
        ("second", "complete"),
        ("no-timing", "complete"),
    ]
    assert result["turns"][0]["model"] == "context-model"
    assert result["turns"][1]["model"] == "turn-model"
    assert result["turns"][2]["startedAt"] is None
    assert result["stats"]["contextWindow"] == 64_000
    assert result["stats"]["toolCalls"] == 3
    assert result["stats"]["failedTools"] == 1
    assert {record["event"] for record in result["records"]} >= {
        "Tool search",
        "Tool search result",
        "Extension",
        "Subagent · completed",
        "Compaction",
        "Agent communication",
    }
    communication = next(record for record in result["records"] if record["id"] == "communication")
    assert communication["metadata"]["otherRecipients"] == ["observer"]
    assert communication["metadata"]["triggerTurn"] is True
    assert {warning["code"] for warning in result["warnings"]} >= {
        "overlapping_turn_start",
        "unmatched_tool_result",
        "malformed_item_completed",
        "unsupported_turn_item",
        "malformed_thread_rollback",
    }


def test_projection_bounds_turns_and_retains_constant_space_failure_aggregate(
    tmp_path: Path,
) -> None:
    events: list[dict[str, object]] = []
    for index in range(1105):
        events.extend(
            [
                {
                    "timestamp": index * 2,
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": f"turn-{index}"},
                },
                {
                    "timestamp": index * 2 + 1,
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "call_id": f"call-{index}",
                        "name": "fail",
                    },
                },
                {
                    "timestamp": index * 2 + 1.25,
                    "type": "response_item",
                    "payload": {
                        "type": "function_call_output",
                        "call_id": f"call-{index}",
                        "output": {"isError": True},
                    },
                },
                {
                    "timestamp": index * 2 + 1.5,
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": f"turn-{index}"},
                },
            ]
        )
    result = parse_session(write_rollout(tmp_path / "bounded-memory.jsonl", events), max_records=50)

    assert result["stats"]["turns"] == 1105
    assert result["stats"]["visibleTurns"] == 1000
    assert result["stats"]["omittedTurns"] == 105
    assert result["stats"]["toolCalls"] == 1105
    assert result["stats"]["failedTools"] == 1105
    assert len(result["records"]) == 50
    assert all(not any(key.startswith("_") for key in record) for record in result["records"])


def test_projection_retains_the_turn_referenced_by_an_old_visible_record(
    tmp_path: Path,
) -> None:
    events: list[dict[str, object]] = [
        {
            "timestamp": 0,
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "record-owner"},
        },
        {
            "timestamp": 0.25,
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "retained request"},
        },
        {
            "timestamp": 0.5,
            "type": "event_msg",
            "payload": {"type": "task_complete", "turn_id": "record-owner"},
        },
    ]
    for index in range(1, 1101):
        events.extend(
            [
                {
                    "timestamp": index,
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": f"empty-{index}"},
                },
                {
                    "timestamp": index + 0.5,
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "turn_id": f"empty-{index}"},
                },
            ]
        )

    result = parse_session(write_rollout(tmp_path / "old-record-turn.jsonl", events))

    visible_turn_indices = {turn["index"] for turn in result["turns"]}
    assert result["stats"]["turns"] == 1101
    assert result["stats"]["visibleTurns"] == 1000
    assert result["stats"]["omittedTurns"] == 101
    assert result["records"][0]["turn"] == 1
    assert all(record["turn"] in visible_turn_indices for record in result["records"])


def test_evicted_tool_call_is_correlated_with_late_failure(tmp_path: Path) -> None:
    events: list[dict[str, object]] = [
        {
            "timestamp": 1,
            "type": "response_item",
            "payload": {"type": "function_call", "call_id": "late", "name": "demo"},
        }
    ]
    events.extend(
        {
            "timestamp": index + 2,
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"text": f"filler {index}"}],
            },
        }
        for index in range(60)
    )
    events.append(
        {
            "timestamp": 100,
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "late",
                "output": {"isError": True},
            },
        }
    )

    result = parse_session(write_rollout(tmp_path / "late-result.jsonl", events), max_records=50)

    assert result["stats"]["toolCalls"] == 1
    assert result["stats"]["failedTools"] == 1
    assert not any(warning["code"] == "unmatched_tool_result" for warning in result["warnings"])
    assert result["records"][-1]["status"] == "error"


def test_resolver_does_not_accept_filename_substrings_and_session_list_deduplicates(
    codex_home: Path,
) -> None:
    write_rollout(
        codex_home / "sessions" / "rollout-unrelated-target-fragment.jsonl",
        rollout_events("different-session"),
    )
    with pytest.raises(ValueError, match="not found"):
        resolve_session("target", False)

    older_events = rollout_events("duplicate-session")
    older_events[2]["payload"]["message"] = "searchable only in stale copy"
    newer_events = rollout_events("duplicate-session")
    newer_events[2]["payload"]["message"] = "canonical newest copy"
    older = write_rollout(
        codex_home / "sessions" / "rollout-duplicate-old.jsonl",
        older_events,
    )
    newer = write_rollout(
        codex_home / "sessions" / "rollout-duplicate-new.jsonl",
        newer_events,
    )
    os.utime(older, ns=(1, 1))
    os.utime(newer, ns=(2, 2))
    listed = list_session_overviews(limit=100, include_archived=False)

    assert [session["id"] for session in listed].count("duplicate-session") == 1
    assert not any(
        session["id"] == "duplicate-session"
        for session in list_session_overviews(
            limit=100, query="searchable only in stale copy", include_archived=False
        )
    )


def test_unrelated_damaged_paginated_rollout_does_not_break_listing_or_resolution(
    codex_home: Path,
) -> None:
    damaged_id = "65656565-6565-4565-8565-656565656565"
    damaged = codex_home / "sessions" / f"rollout-2026-08-16T00-00-00-{damaged_id}.jsonl"
    damaged.write_text(
        "{damaged metadata}\n"
        + json.dumps(
            {
                "timestamp": "2026-08-16T00:00:01Z",
                "ordinal": 1,
                "type": "event_msg",
                "payload": {"type": "turn_started", "turn_id": "copied"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    listed = list_session_overviews(limit=100, include_archived=False)

    assert "session-alpha" in {session["id"] for session in listed}
    assert resolve_session("session-alpha", False).name == "rollout-alpha.jsonl"


def test_resolver_prefix_is_not_ambiguous_when_thread_and_rollout_ids_match(
    codex_home: Path,
) -> None:
    thread_id = "12345678-1234-4234-8234-123456789abc"
    events = rollout_events(thread_id)
    events[0]["payload"]["id"] = thread_id
    path = write_rollout(
        codex_home / "sessions" / f"rollout-2026-08-16T00-00-00-{thread_id}.jsonl",
        events,
    )

    assert resolve_session("12345678", False) == path


def test_extreme_numbers_and_identifiers_are_bounded(tmp_path: Path) -> None:
    enormous = 10**10_000
    assert parse_timestamp(enormous) is None
    assert parse_timestamp(10**300) is None
    assert epoch_milliseconds(enormous) is None
    assert duration_milliseconds(enormous) is None
    assert duration_milliseconds({"secs": enormous, "nanos": 0}) is None
    assert duration_milliseconds({"secs": 1.5, "nanos": 0}) is None

    oversized_id = "x" * 10_000
    events = [
        {
            "timestamp": "2026-08-16T00:00:00Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": oversized_id},
        },
        {
            "timestamp": "2026-08-16T00:00:01Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "call_id": oversized_id,
                "id": oversized_id,
                "name": "demo",
            },
        },
    ]
    result = parse_session(write_rollout(tmp_path / "oversized-id.jsonl", events))

    assert len(result["turns"][0]["id"]) <= 240
    assert len(result["records"][0]["id"]) <= 240
    assert len(result["records"][0]["callId"]) <= 240


def test_opaque_call_ids_stay_distinct_and_visible_record_ids_are_unique(
    tmp_path: Path,
) -> None:
    events: list[dict[str, object]] = [
        {
            "timestamp": "2026-08-16T00:00:00Z",
            "type": "response_item",
            "payload": {"type": "function_call", "id": "duplicate", "call_id": "call one"},
        },
        {
            "timestamp": "2026-08-16T00:00:01Z",
            "type": "response_item",
            "payload": {"type": "function_call", "id": "duplicate", "call_id": "call  one"},
        },
        {
            "timestamp": "2026-08-16T00:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "same-message",
                "role": "assistant",
                "content": [],
            },
        },
        {
            "timestamp": "2026-08-16T00:00:03Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "same-message",
                "role": "assistant",
                "content": [],
            },
        },
    ]
    path = write_rollout(tmp_path / "opaque-identifiers.jsonl", events)

    result = parse_session(path)
    overview = session_overview(path)

    assert {record["callId"] for record in result["records"] if record["kind"] == "tool"} == {
        "call one",
        "call  one",
    }
    assert len({record["id"] for record in result["records"]}) == len(result["records"])
    assert overview["toolCalls"] == result["stats"]["toolCalls"] == 2


def test_public_schema_accepts_full_trajectory_with_recent_sessions(codex_home: Path) -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    result = trajectory_result(
        {"sessionId": "session-alpha", "detailLevel": "full"}, with_ui=False
    )["structuredContent"]

    TRAJECTORY_VALIDATOR.validate(result)

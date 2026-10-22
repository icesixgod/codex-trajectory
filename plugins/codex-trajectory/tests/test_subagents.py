"""Task totals include descendants without mixing their records or inherited usage."""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from codex_trajectory import projection, sessions, subagents
from codex_trajectory.tools import call_tool
from conftest import write_rollout
from jsonschema import Draft202012Validator, FormatChecker

SCHEMA = json.loads(
    (Path(__file__).parents[3] / "schemas/trajectory-v2.schema.json").read_text(encoding="utf-8")
)
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


def tokens(input_tokens: int = 200, output_tokens: int = 40) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": input_tokens * 2 // 5,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": output_tokens // 2,
        "total_tokens": input_tokens + output_tokens,
    }


def event(entry_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"timestamp": "2026-10-01T00:00:01Z", "type": entry_type, "payload": payload}


def activity(identifier: str, *, canonical: bool = False) -> dict[str, Any]:
    payload = {
        "type": "SubAgentActivity" if canonical else "sub_agent_activity",
        "agent_thread_id": identifier,
        "agent_path": "/root/worker",
        "kind": "started",
    }
    if canonical:
        return event("event_msg", {"type": "item_completed", "item": payload})
    return event("event_msg", payload)


def write_agent(
    home: Path,
    identifier: str,
    *,
    parent: str | None = None,
    model: str = "gpt-5",
    tier: str = "default",
    usage: dict[str, int] | None = None,
    archived: bool = False,
    nested_parent: bool = False,
    activities: list[dict[str, Any]] | None = None,
) -> Path:
    metadata: dict[str, Any] = {
        "id": identifier,
        "source": "cli",
        "base_instructions": "private child instructions",
    }
    if parent is not None:
        if nested_parent:
            metadata["source"] = {
                "subagent": {"thread_spawn": {"parent_thread_id": parent, "depth": 1}}
            }
        else:
            metadata["parent_thread_id"] = parent
    values = [
        event("session_meta", metadata),
        event("turn_context", {"model": model, "service_tier": tier}),
        event("event_msg", {"type": "task_started", "turn_id": identifier}),
        event("event_msg", {"type": "user_message", "message": f"Prompt for {identifier}"}),
        *(activities or []),
        event(
            "response_item",
            {
                "type": "message",
                "id": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": f"Output for {identifier}"}],
            },
        ),
        event(
            "event_msg",
            {"type": "token_count", "info": {"total_token_usage": usage or tokens()}},
        ),
        event("event_msg", {"type": "task_complete", "turn_id": identifier, "duration_ms": 1000}),
    ]
    root = "archived_sessions" if archived else "sessions"
    return write_rollout(home / root / f"rollout-{identifier}.jsonl", values)


def read_task(identifier: str = "root", **arguments: Any) -> dict[str, Any]:
    return call_tool("get_codex_trajectory", {"sessionId": identifier, **arguments})[
        "structuredContent"
    ]


@pytest.mark.parametrize(
    "tool", ["get_codex_trajectory", "show_codex_trajectory", "open_codex_trajectory"]
)
def test_task_totals_include_nested_archived_agents_with_their_own_prices(
    codex_home: Path, tool: str
) -> None:
    write_agent(codex_home, "root", activities=[activity("grandchild")])
    write_agent(
        codex_home,
        "child",
        parent="root",
        model="gpt-5-mini",
        tier="fast",
        usage=tokens(1000, 200),
        nested_parent=True,
    )
    write_agent(codex_home, "grandchild", parent="child", model="gpt-5-nano", archived=True)
    write_agent(codex_home, "unrelated", usage=tokens(900_000, 100_000))
    arguments = {} if tool == "open_codex_trajectory" else {"sessionId": "root"}
    result = call_tool(tool, arguments, metadata={"thread_id": "root"})["structuredContent"]

    assert result["stats"]["tokens"] == tokens(1400, 280)
    assert result["stats"]["cost"]["totalUsd"] == pytest.approx(0.0019824)
    assert result["stats"]["cost"]["pricedModelCalls"] == 3
    assert result["stats"]["cost"]["coverage"] == "complete"
    assert result["turns"][0]["usage"] == tokens()
    assert result["turns"][0]["cost"]["totalUsd"] == pytest.approx(0.00056)
    assert result["stats"]["turns"] == 1
    assert any(w["code"] == "codex_fast_cost_multiplier" for w in result["warnings"])
    assert not any(w["code"] == "subagent_usage_incomplete" for w in result["warnings"])
    serialized = json.dumps(result)
    assert "Output for child" not in serialized
    assert "Prompt for grandchild" not in serialized
    assert "private child instructions" not in serialized
    assert str(codex_home) not in serialized
    VALIDATOR.validate(result)


def test_selecting_a_child_includes_only_its_descendants(codex_home: Path) -> None:
    write_agent(codex_home, "root")
    write_agent(codex_home, "child", parent="root")
    write_agent(codex_home, "grandchild", parent="child")
    write_agent(codex_home, "sibling", parent="root", usage=tokens(1000, 200))

    result = read_task("child")
    assert result["stats"]["tokens"] == tokens(400, 80)
    assert result["stats"]["cost"]["pricedModelCalls"] == 2


@pytest.mark.parametrize("canonical", [False, True])
def test_activity_links_resolve_older_agents_without_parent_metadata(
    codex_home: Path, canonical: bool
) -> None:
    write_agent(codex_home, "root", activities=[activity("child", canonical=canonical)] * 2)
    write_agent(codex_home, "child", activities=[activity("root")])
    result = read_task()
    assert result["stats"]["tokens"] == tokens(400, 80)
    assert result["stats"]["cost"]["pricedModelCalls"] == 2


def test_duplicate_logical_agent_ids_and_parent_cycles_count_once(codex_home: Path) -> None:
    write_agent(codex_home, "root", parent="grandchild")
    child = write_agent(codex_home, "child", parent="root")
    duplicate = write_rollout(codex_home / "archived_sessions/rollout-copy.jsonl", [])
    duplicate.write_bytes(child.read_bytes())
    write_agent(codex_home, "grandchild", parent="child")

    result = read_task()
    assert result["stats"]["tokens"] == tokens(600, 120)
    assert result["stats"]["cost"]["pricedModelCalls"] == 3


def test_unknown_child_price_preserves_tokens_and_partial_pricing(codex_home: Path) -> None:
    write_agent(codex_home, "root")
    write_agent(codex_home, "child", parent="root", model="unpublished-test-model")
    result = read_task()
    assert result["stats"]["tokens"] == tokens(400, 80)
    assert result["stats"]["cost"]["totalUsd"] == pytest.approx(0.00056)
    assert result["stats"]["cost"]["coverage"] == "partial"
    assert result["stats"]["cost"]["pricedModelCalls"] == 1
    assert result["stats"]["cost"]["unpricedModelCalls"] == 1


@pytest.mark.parametrize("missing_in", ["root", "child"])
def test_missing_agent_total_is_derived_before_combining(codex_home: Path, missing_in: str) -> None:
    partial = {key: value for key, value in tokens().items() if key != "total_tokens"}
    write_agent(codex_home, "root", usage=partial if missing_in == "root" else tokens())
    write_agent(
        codex_home, "child", parent="root", usage=partial if missing_in == "child" else tokens()
    )
    assert read_task()["stats"]["tokens"] == tokens(400, 80)


def test_child_growth_updates_live_totals_without_rescanning_parent(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = write_agent(codex_home, "root")
    child = write_agent(codex_home, "child", parent="root")
    first = projection.trajectory_update_result({"sessionId": "root"})["structuredContent"]
    root_signature = sessions.session_signature(root)
    original = projection.parse_session
    scans: list[Path] = []

    def track_scan(path: Path, *args: Any, **kwargs: Any) -> dict[str, Any]:
        scans.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(projection, "parse_session", track_scan)
    with child.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                event(
                    "event_msg",
                    {"type": "token_count", "info": {"total_token_usage": tokens(400, 80)}},
                )
            )
            + "\n"
        )
    second = projection.trajectory_update_result(
        {"sessionId": "root", "revision": first["revision"]}
    )["structuredContent"]
    assert second["unchanged"] is False
    assert second["revision"] != first["revision"]
    assert second["trajectory"]["stats"]["tokens"] == tokens(600, 120)
    assert second["trajectory"]["stats"]["cost"]["pricedModelCalls"] == 3
    assert sessions.session_signature(root) == root_signature
    assert scans == [child]
    third = projection.trajectory_update_result(
        {"sessionId": "root", "revision": second["revision"]}
    )["structuredContent"]
    assert third["unchanged"] is True
    assert scans == [child]


def test_new_or_removed_child_changes_revision_with_unchanged_parent(codex_home: Path) -> None:
    write_agent(codex_home, "root", activities=[activity("child")])
    first = projection.trajectory_update_result({"sessionId": "root"})["structuredContent"]
    assert first["trajectory"]["warnings"][0]["code"] == "subagent_usage_incomplete"
    child = write_agent(codex_home, "child", parent="root")
    second = projection.trajectory_update_result(
        {"sessionId": "root", "revision": first["revision"]}
    )["structuredContent"]
    assert second["unchanged"] is False
    assert second["trajectory"]["stats"]["tokens"] == tokens(400, 80)
    assert second["trajectory"]["warnings"] == []
    child.unlink()
    third = projection.trajectory_update_result(
        {"sessionId": "root", "revision": second["revision"]}
    )["structuredContent"]
    assert third["unchanged"] is False
    assert third["trajectory"]["stats"]["tokens"] == tokens()
    assert third["trajectory"]["warnings"][0]["code"] == "subagent_usage_incomplete"


def test_metadata_only_new_child_is_found_without_parent_activity(codex_home: Path) -> None:
    write_agent(codex_home, "root")
    first = projection.trajectory_update_result({"sessionId": "root"})["structuredContent"]
    write_agent(codex_home, "child", parent="root")
    second = projection.trajectory_update_result(
        {"sessionId": "root", "revision": first["revision"]}
    )["structuredContent"]
    assert second["unchanged"] is False
    assert second["trajectory"]["stats"]["tokens"] == tokens(400, 80)


@pytest.mark.parametrize("detail", ["summary", "full"])
@pytest.mark.parametrize("before", [None, 2, 1])
def test_task_totals_are_independent_of_parent_page(
    codex_home: Path, detail: str, before: int | None
) -> None:
    write_agent(codex_home, "root")
    write_agent(codex_home, "child", parent="root")
    arguments: dict[str, Any] = {"detailLevel": detail, "maxRecords": 50}
    if before is not None:
        arguments["beforeRecord"] = before
    result = read_task(**arguments)
    assert result["stats"]["tokens"] == tokens(400, 80)
    assert result["stats"]["cost"]["totalUsd"] == pytest.approx(0.00112)
    assert all("Output for child" not in str(record) for record in result["records"])
    VALIDATOR.validate(result)


def test_corrupt_child_reports_incomplete_totals_and_still_visits_grandchildren(
    codex_home: Path,
) -> None:
    write_agent(codex_home, "root")
    child = write_agent(codex_home, "child", parent="root")
    values = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
    values[0]["payload"]["history_mode"] = "paginated"
    write_rollout(child, values)
    write_agent(codex_home, "grandchild", parent="child")
    result = read_task()
    assert result["stats"]["tokens"] == tokens(400, 80)
    assert result["warnings"][0]["code"] == "subagent_usage_incomplete"
    assert "1 subagent log(s) unavailable" in result["warnings"][0]["message"]


def test_child_read_warnings_cannot_silently_claim_complete_history(codex_home: Path) -> None:
    write_agent(codex_home, "root")
    child = write_agent(codex_home, "child", parent="root")
    with child.open("a", encoding="utf-8") as handle:
        handle.write("{malformed private-child-data}\n")
    result = read_task()
    assert result["stats"]["tokens"] == tokens(400, 80)
    assert result["warnings"][0]["code"] == "subagent_usage_incomplete"
    assert "1 subagent log(s) with read warnings" in result["warnings"][0]["message"]
    assert "private-child-data" not in json.dumps(result)


def test_subagent_read_budget_is_not_bypassed_by_cached_larger_reads(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = write_agent(codex_home, "root")
    child = write_agent(codex_home, "child", parent="root")
    with child.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"padding": "x" * 8000}) + "\n")
    monkeypatch.setattr(sessions, "MAX_JSONL_TOTAL_BYTES", root.stat().st_size + 100)
    permitted = read_task(maxReadBytes=child.stat().st_size + 100)
    assert permitted["stats"]["tokens"] == tokens(400, 80)
    with pytest.raises(sessions.ReadLimitExceeded) as raised:
        projection.trajectory_result({"sessionId": "root"}, False)
    assert raised.value.session_id == "root"
    with pytest.raises(sessions.ReadLimitExceeded) as live_error:
        projection.trajectory_update_result({"sessionId": "root"})
    assert live_error.value.session_id == "root"
    assert live_error.value.session_id_verified is True


def test_linked_subagent_log_is_never_read(codex_home: Path) -> None:
    write_agent(codex_home, "root", activities=[activity("child")])
    outside = write_agent(codex_home.parent / "outside-home", "child", parent="root")
    link = codex_home / "sessions/rollout-child.jsonl"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("Symlinks unavailable")
    result = read_task()
    assert result["stats"]["tokens"] == tokens()
    assert result["warnings"][0]["code"] == "subagent_usage_incomplete"


def test_subagent_token_total_overflow_is_explicit(codex_home: Path) -> None:
    write_agent(codex_home, "root")
    write_agent(
        codex_home,
        "child",
        parent="root",
        usage={"input_tokens": projection.MAX_SAFE_INTEGER, "output_tokens": 0},
    )
    with pytest.raises(ValueError, match="integer limit"):
        projection.trajectory_result({"sessionId": "root"}, False)


def test_paginated_subagent_resume_counts_own_usage_once(codex_home: Path) -> None:
    write_agent(codex_home, "root")
    identifier = "12121212-1212-4212-8212-121212121212"
    physical = "34343434-3434-4434-8434-343434343434"
    child = codex_home / "sessions" / f"rollout-2026-10-01T00-00-00-{identifier}.jsonl"
    values = [
        event(
            "session_meta",
            {
                "id": identifier,
                "history_mode": "paginated",
                "subagent_history_start_ordinal": 4,
                "parent_thread_id": "root",
            },
        ),
        event(
            "event_msg", {"type": "token_count", "info": {"total_token_usage": tokens(8000, 1600)}}
        ),
        event("event_msg", {"type": "turn_started", "turn_id": "inherited-parent"}),
        event("event_msg", {"type": "turn_complete", "turn_id": "inherited-parent"}),
        event("turn_context", {"model": "gpt-5"}),
        event("event_msg", {"type": "turn_started", "turn_id": "own"}),
        event("event_msg", {"type": "token_count", "info": {"total_token_usage": tokens()}}),
        event("event_msg", {"type": "turn_complete", "turn_id": "own"}),
    ]
    for ordinal, value in enumerate(values):
        value["ordinal"] = ordinal
    write_rollout(child, values)
    resumed = child.with_name(f"rollout-2026-10-01T00-00-00-{identifier}_{physical}.jsonl")
    resumed_values = [
        event(
            "session_meta",
            {
                "id": identifier,
                "history_mode": "paginated",
                "parent_thread_id": "root",
                "history_base": {
                    "thread_id": identifier,
                    "end_ordinal_exclusive": len(values),
                    "end_byte_offset": child.stat().st_size,
                },
            },
        ),
        event("event_msg", {"type": "turn_started", "turn_id": "resumed"}),
        event("event_msg", {"type": "token_count", "info": {"total_token_usage": tokens(400, 80)}}),
        event("event_msg", {"type": "turn_complete", "turn_id": "resumed"}),
    ]
    for ordinal, value in enumerate(resumed_values, len(values)):
        value["ordinal"] = ordinal
    write_rollout(resumed, resumed_values)
    now = child.stat().st_mtime_ns + 1_000_000_000
    os.utime(resumed, ns=(now, now))
    result = read_task()
    assert result["stats"]["tokens"] == tokens(600, 120)
    assert result["stats"]["cost"]["pricedModelCalls"] == 3
    assert result["warnings"] == []


def test_compressed_subagent_is_counted_once_with_plain_sibling(codex_home: Path) -> None:
    write_agent(codex_home, "root")
    child = write_agent(codex_home, "child", parent="root")
    try:
        zstd = importlib.import_module("compression.zstd")
    except ImportError:
        zstd = importlib.import_module("zstandard")
        compressed_bytes = zstd.ZstdCompressor().compress(child.read_bytes())
    else:
        compressed_bytes = zstd.compress(child.read_bytes())
    compressed = child.with_suffix(".jsonl.zst")
    compressed.write_bytes(compressed_bytes)
    assert read_task()["stats"]["tokens"] == tokens(400, 80)
    child.unlink()
    assert read_task()["stats"]["tokens"] == tokens(400, 80)


def test_raised_budget_can_read_an_exact_referenced_compressed_child(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    identifier = "56565656-5656-4656-8656-565656565656"
    root = write_agent(codex_home, "root", activities=[activity(identifier)])
    child = write_agent(codex_home, identifier, parent="root")
    with child.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"padding": "x" * 8000}) + "\n")
    try:
        zstd = importlib.import_module("compression.zstd")
    except ImportError:
        zstd = importlib.import_module("zstandard")
        compressed_bytes = zstd.ZstdCompressor().compress(child.read_bytes())
    else:
        compressed_bytes = zstd.compress(child.read_bytes())
    required = child.stat().st_size
    child.with_suffix(".jsonl.zst").write_bytes(compressed_bytes)
    child.unlink()
    monkeypatch.setattr(sessions, "MAX_JSONL_TOTAL_BYTES", root.stat().st_size + 100)
    with pytest.raises(sessions.ReadLimitExceeded):
        projection.trajectory_result({"sessionId": "root"}, False)
    assert read_task(maxReadBytes=required + 100)["stats"]["tokens"] == tokens(400, 80)


def test_task_without_own_usage_can_total_its_child(codex_home: Path) -> None:
    write_rollout(
        codex_home / "sessions/rollout-root.jsonl", [event("session_meta", {"id": "root"})]
    )
    write_agent(codex_home, "child", parent="root")
    result = read_task()
    assert result["stats"]["tokens"] == tokens()
    assert result["stats"]["cost"]["totalUsd"] == pytest.approx(0.00056)
    assert result["turns"] == []


def test_activity_does_not_override_a_different_explicit_parent(codex_home: Path) -> None:
    write_agent(codex_home, "root", activities=[activity("unrelated")])
    write_agent(codex_home, "unrelated", parent="other-root")
    assert read_task()["stats"]["tokens"] == tokens()


def test_arbitrary_tool_output_and_cwd_never_link_an_unrelated_agent(codex_home: Path) -> None:
    root = write_agent(codex_home, "root")
    child = write_agent(codex_home, "unrelated")
    for path in (root, child):
        values = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        values[0]["payload"]["cwd"] = "/same/project"
        if path == root:
            values.append(
                event(
                    "response_item",
                    {"type": "function_call_output", "output": activity("unrelated")["payload"]},
                )
            )
        write_rollout(path, values)
    assert read_task()["stats"]["tokens"] == tokens()


def test_conflicting_parent_metadata_is_reported_as_unavailable(codex_home: Path) -> None:
    write_agent(codex_home, "root", activities=[activity("child")])
    child = write_agent(codex_home, "child", parent="root", nested_parent=True)
    values = [json.loads(line) for line in child.read_text(encoding="utf-8").splitlines()]
    values[0]["payload"]["parent_thread_id"] = "other-root"
    write_rollout(child, values)
    result = read_task()
    assert result["stats"]["tokens"] == tokens()
    assert result["warnings"][0]["code"] == "subagent_usage_incomplete"


def test_usage_cache_has_a_byte_limit(codex_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subagents, "MAX_USAGE_CACHE_BYTES", 1)
    write_agent(codex_home, "root")
    write_agent(codex_home, "child", parent="root")
    assert read_task()["stats"]["tokens"] == tokens(400, 80)
    assert not subagents._USAGE
    assert not subagents._USAGE_SIZES


@pytest.mark.parametrize("activities_only", [False, True])
def test_agent_discovery_limit_is_explicit(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch, activities_only: bool
) -> None:
    monkeypatch.setattr(subagents, "MAX_TASK_AGENTS", 2)
    activities = [activity(name) for name in ("one", "two", "three")] if activities_only else []
    write_agent(codex_home, "root", activities=activities)
    if not activities_only:
        for identifier in ("one", "two", "three"):
            write_agent(codex_home, identifier, parent="root")
    with pytest.raises(ValueError, match="discovery limit"):
        projection.trajectory_result({"sessionId": "root"}, False)


def test_usage_and_metadata_caches_are_bounded(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(subagents, "MAX_CACHED_USAGE", 2)
    monkeypatch.setattr(subagents, "MAX_CACHED_LINKS", 2)
    write_agent(codex_home, "root")
    for identifier in ("first", "second", "third"):
        write_agent(codex_home, identifier, parent="root")
    assert read_task()["stats"]["tokens"] == tokens(800, 160)
    assert len(subagents._USAGE) <= 2
    assert len(subagents._LINKS) <= 2

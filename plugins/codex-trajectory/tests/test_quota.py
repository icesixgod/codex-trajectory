"""Account quota refresh must not depend on the selected task changing."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from codex_trajectory import projection, sessions
from conftest import write_rollout


def sample(timestamp: str, used: int, **extra: Any) -> dict[str, Any]:
    return {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "limit_id": "codex",
                "primary": {"used_percent": used, "window_minutes": 10080},
                "secondary": None,
                "credits": {"balance": "private-credit-balance"},
                **extra,
            },
        },
    }


def test_other_task_quota_refreshes_without_reparsing_selected_task(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    other = codex_home / "sessions" / "rollout-other.jsonl"
    write_rollout(other, [sample("2026-09-30T00:00:00Z", 90)])
    first = projection.trajectory_update_result({"sessionId": "session-alpha"})["structuredContent"]
    write_rollout(other, [sample("2026-10-01T00:00:00Z", 93)])

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unchanged task must not be parsed")

    monkeypatch.setattr(projection, "cached_trajectory", fail)
    updated = projection.trajectory_update_result(
        {"sessionId": "session-alpha", "revision": first["revision"]}
    )["structuredContent"]
    assert updated["unchanged"] is True
    assert "trajectory" not in updated
    assert updated["quota"] == {
        "rateLimits": {"primary": {"usedPercent": 93, "windowMinutes": 10080, "resetsAt": None}},
        "sampledAt": "2026-10-01T00:00:00Z",
    }
    assert "private-credit-balance" not in json.dumps(updated)
    # Historical/public projections retain the selected task's own snapshot.
    assert first["trajectory"]["stats"]["rateLimits"]["primary"]["usedPercent"] == 31.5


def test_quota_uses_sample_timestamp_and_ignores_other_limit_buckets(codex_home: Path) -> None:
    write_rollout(
        codex_home / "sessions" / "rollout-fresh.jsonl",
        [sample("2026-10-01T00:00:00Z", 93)],
    )
    stale = write_rollout(
        codex_home / "sessions" / "rollout-stale.jsonl",
        [
            sample("2026-09-30T00:00:00Z", 90),
            sample("2026-10-01T01:00:00Z", 0, limit_id="base_model_inference"),
        ],
    )
    os.utime(stale, (2_000_000_000, 2_000_000_000))
    quota = projection.latest_account_quota()
    assert quota["sampledAt"] == "2026-10-01T00:00:00Z"
    assert quota["rateLimits"]["primary"]["usedPercent"] == 93


def test_retired_windows_do_not_keep_old_weekly_quota(tmp_path: Path) -> None:
    older = sample(
        "2026-09-30T00:00:00Z",
        50,
        secondary={"used_percent": 90, "window_minutes": 10080},
    )
    latest = sample("2026-10-01T00:00:00Z", 93)
    inference = sample("2026-10-01T01:00:00Z", 0, limit_id="base_model_inference")
    result = projection.parse_session(
        write_rollout(tmp_path / "quota.jsonl", [older, latest, inference])
    )
    assert result["stats"]["rateLimits"] == {
        "primary": {"usedPercent": 93, "windowMinutes": 10080, "resetsAt": None}
    }


def test_quota_tail_is_bounded_cached_and_tolerates_partial_lines(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = codex_home / "sessions" / "rollout-tail.jsonl"
    valid = json.dumps(sample("2026-10-01T00:00:00Z", 93)).encode()
    path.write_bytes(b"x" * 300_000 + b'\ninvalid "rate_limits"\n' + valid + b"\n" + valid[:100])
    signature, entries = sessions.recent_rate_limit_entries(path)
    assert entries == [json.loads(valid)]

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unchanged tail must not be decoded")

    monkeypatch.setattr(sessions, "strict_json_loads", fail)
    assert sessions.recent_rate_limit_entries(path, signature) == (signature, None)


def test_quota_reader_rejects_linked_sources(codex_home: Path) -> None:
    target = write_rollout(
        codex_home / "sessions" / "rollout-real.jsonl", [sample("2026-10-01T00:00:00Z", 93)]
    )
    linked = codex_home / "sessions" / "rollout-link.jsonl"
    linked.symlink_to(target)
    with pytest.raises(OSError):
        sessions.recent_rate_limit_entries(linked)
    linked.unlink()
    os.link(target, linked)
    with pytest.raises(OSError):
        sessions.recent_rate_limit_entries(linked)


def test_missing_local_samples_hide_quota(codex_home: Path) -> None:
    for path in (codex_home / "sessions").rglob("*.jsonl"):
        path.write_text("{}\n")
    assert projection.latest_account_quota() == {"rateLimits": None, "sampledAt": None}


def append_events(path: Path, events: list[dict[str, Any]]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event) + "\n")


def large_tool_output() -> dict[str, Any]:
    return {
        "type": "response_item",
        "payload": {"type": "function_call_output", "output": "x" * 300_000},
    }


def test_cached_quota_survives_large_tool_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    path = write_rollout(
        tmp_path / "sessions/rollout-quota.jsonl", [sample("2026-09-30T00:00:00Z", 90)]
    )
    expected = projection.latest_account_quota()
    append_events(path, [large_tool_output()])
    assert sessions.recent_rate_limit_entries(path)[1] == []
    assert projection.latest_account_quota() == expected
    assert projection.latest_account_quota() == expected


@pytest.mark.parametrize("update_kind", ["partial", "older", "remove", "remove_all"])
def test_cached_quota_merges_updates_after_tail_rollover(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, update_kind: str
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    path = write_rollout(
        tmp_path / "sessions/rollout-quota.jsonl",
        [
            sample(
                "2026-09-30T00:00:00Z",
                50,
                secondary={"used_percent": 90, "window_minutes": 10080},
            )
        ],
    )
    expected = projection.latest_account_quota()
    update = sample("2026-10-01T00:00:00Z", 60)
    del update["payload"]["rate_limits"]["secondary"]
    if update_kind == "older":
        update["timestamp"] = "2026-09-29T00:00:00Z"
    else:
        expected["sampledAt"] = "2026-10-01T00:00:00Z"
        if update_kind == "partial":
            expected["rateLimits"]["primary"]["usedPercent"] = 60
        else:
            update["payload"]["rate_limits"]["primary"] = None
            del expected["rateLimits"]["primary"]
            if update_kind == "remove_all":
                update["payload"]["rate_limits"]["secondary"] = None
                expected["rateLimits"] = None
    append_events(path, [large_tool_output(), update])
    assert projection.latest_account_quota() == expected


@pytest.mark.parametrize("change", ["replace", "truncate", "rewrite"])
def test_cached_quota_is_invalidated_when_source_is_not_appended(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    path = write_rollout(
        tmp_path / "sessions/rollout-quota.jsonl", [sample("2026-09-30T00:00:00Z", 90)]
    )
    projection.latest_account_quota()
    if change == "replace":
        replacement = write_rollout(tmp_path / "replacement.jsonl", [large_tool_output()])
        replacement.replace(path)
    elif change == "truncate":
        path.write_text("{}\n", encoding="utf-8")
    else:
        size = path.stat().st_size
        path.write_text(" " * (size - 1) + "\n", encoding="utf-8")
    assert projection.latest_account_quota() == {"rateLimits": None, "sampledAt": None}

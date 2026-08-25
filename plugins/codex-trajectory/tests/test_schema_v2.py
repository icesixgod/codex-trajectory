"""Contract tests for the published trajectory version 2 schema."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

SCHEMA_PATH = Path(__file__).parents[3] / "schemas" / "trajectory-v2.schema.json"
SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
MAX_SAFE_INTEGER = 2**53 - 1


def cost_estimate(coverage: str) -> dict[str, Any]:
    """Return one internally consistent estimate for a coverage branch."""
    estimate: dict[str, Any] = {
        "currency": "USD",
        "estimated": True,
        "totalUsd": 0.001,
        "uncachedInputUsd": 0.0004,
        "cachedInputUsd": 0.0001,
        "outputUsd": 0.0005,
        "coverage": coverage,
        "pricedModelCalls": 1,
        "unpricedModelCalls": 0,
        "pricingUpdatedAt": "2026-08-24",
    }
    if coverage == "partial":
        estimate["unpricedModelCalls"] = 1
    elif coverage == "unavailable":
        estimate.update(
            {
                "totalUsd": None,
                "uncachedInputUsd": None,
                "cachedInputUsd": None,
                "outputUsd": None,
                "pricedModelCalls": 0,
                "unpricedModelCalls": 1,
            }
        )
    return estimate


def trajectory(detail_level: str = "summary") -> dict[str, Any]:
    """Return one minimal valid trajectory payload."""
    return {
        "schemaVersion": 2,
        "detailLevel": detail_level,
        "generatedAt": "2026-08-24T00:00:00Z",
        "session": {
            "id": "session-alpha",
            "title": "Test task",
            "cwd": None,
            "model": None,
            "effort": None,
            "originator": None,
            "sourceKind": None,
            "startedAt": None,
            "updatedAt": None,
            "archived": False,
            "parentThreadId": None,
            "agentPath": None,
            "git": None,
        },
        "pagination": {
            "firstRecord": 1,
            "lastRecord": 1,
            "earlierRecords": 0,
            "laterRecords": 0,
            "hasEarlier": False,
            "hasLater": False,
            "nextBeforeRecord": None,
        },
        "stats": {
            "turns": 1,
            "records": 1,
            "visibleRecords": 1,
            "omittedRecords": 0,
            "toolCalls": 0,
            "failedTools": 0,
            "compactions": 0,
            "tokens": None,
            "cost": None,
            "contextWindow": None,
            "rateLimits": None,
        },
        "turns": [
            {
                "index": 1,
                "id": None,
                "startedAt": None,
                "completedAt": None,
                "durationMs": None,
                "timeToFirstTokenMs": None,
                "status": "complete",
                "error": None,
                "records": 1,
                "steps": 0,
                "modelCalls": 0,
                "usage": None,
                "cost": None,
                "model": None,
            }
        ],
        "records": [
            {
                "index": 1,
                "id": "record-1",
                "turn": 1,
                "step": None,
                "kind": "user",
                "event": "User",
                "summary": "Test task",
                "startedAt": None,
                "completedAt": None,
                "durationMs": None,
                "status": "complete",
                "callId": None,
                "input": None,
                "output": None,
                "error": None,
                "usage": None,
                "cost": None,
                "metadata": {},
            }
        ],
        "warnings": [],
    }


def test_schema_is_valid_and_only_describes_trajectory_payloads() -> None:
    Draft202012Validator.check_schema(SCHEMA)
    VALIDATOR.validate(trajectory())

    live_update = {
        "schemaVersion": 2,
        "unchanged": True,
        "revision": "0" * 64,
    }
    assert not VALIDATOR.is_valid(live_update)
    assert "trajectory payload" in SCHEMA["description"]
    assert "Live-update" in SCHEMA["description"]


def test_pagination_is_required() -> None:
    payload = trajectory()
    del payload["pagination"]

    assert not VALIDATOR.is_valid(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("input", "SECRET INPUT"),
        ("output", "SECRET OUTPUT"),
        ("metadata", {"base_instructions": "SECRET"}),
    ],
)
def test_summary_records_reject_full_detail_fields(field: str, value: Any) -> None:
    payload = trajectory("summary")
    payload["records"][0][field] = value

    assert not VALIDATOR.is_valid(payload)


def test_full_records_allow_bounded_details() -> None:
    payload = trajectory("full")
    payload["records"][0].update(
        {
            "input": "tool input",
            "output": "tool output",
            "metadata": {"protocolType": "function_call"},
        }
    )

    VALIDATOR.validate(payload)


@pytest.mark.parametrize("coverage", ["complete", "partial", "unavailable"])
def test_cost_coverage_branches_accept_consistent_estimates(coverage: str) -> None:
    payload = trajectory()
    payload["stats"]["cost"] = cost_estimate(coverage)

    VALIDATOR.validate(payload)


@pytest.mark.parametrize(
    ("coverage", "field", "value"),
    [
        *(
            condition
            for field in ("totalUsd", "uncachedInputUsd", "cachedInputUsd", "outputUsd")
            for condition in (("complete", field, None), ("partial", field, None))
        ),
        *(
            ("unavailable", field, 0.0)
            for field in ("totalUsd", "uncachedInputUsd", "cachedInputUsd", "outputUsd")
        ),
        ("complete", "pricedModelCalls", 0),
        ("complete", "unpricedModelCalls", 1),
        ("partial", "pricedModelCalls", 0),
        ("partial", "unpricedModelCalls", 0),
        ("unavailable", "pricedModelCalls", 1),
        ("unavailable", "unpricedModelCalls", 0),
    ],
)
def test_cost_coverage_branches_reject_inconsistent_estimates(
    coverage: str, field: str, value: Any
) -> None:
    payload = trajectory()
    estimate = cost_estimate(coverage)
    estimate[field] = value
    payload["stats"]["cost"] = estimate

    assert not VALIDATOR.is_valid(payload)


@pytest.mark.parametrize(
    ("owner", "field"),
    [
        ("stats", "tokens"),
        ("session", "git"),
    ],
)
def test_nullable_objects_reject_ambiguous_empty_objects(owner: str, field: str) -> None:
    payload = trajectory()
    payload[owner][field] = {}

    assert not VALIDATOR.is_valid(payload)


def test_nonempty_token_usage_and_git_objects_remain_valid() -> None:
    payload = trajectory()
    payload["stats"]["tokens"] = {"input_tokens": 0}
    payload["session"]["git"] = {"branch": "main"}

    VALIDATOR.validate(payload)


def integer_schemas(value: Any, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], Any]]:
    """Collect every schema node that accepts integers."""
    result: list[tuple[tuple[str, ...], Any]] = []
    if isinstance(value, dict):
        value_type = value.get("type")
        if value_type == "integer" or (isinstance(value_type, list) and "integer" in value_type):
            result.append((path, value))
        for key, nested in value.items():
            result.extend(integer_schemas(nested, (*path, key)))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            result.extend(integer_schemas(nested, (*path, str(index))))
    return result


def test_every_public_integer_is_bounded_to_javascript_safe_range() -> None:
    unbounded = [
        "/".join(path)
        for path, definition in integer_schemas(SCHEMA)
        if not isinstance(definition.get("maximum"), int)
        or definition["maximum"] > MAX_SAFE_INTEGER
    ]

    assert unbounded == []


def test_values_above_javascript_safe_integer_are_rejected() -> None:
    payload = deepcopy(trajectory())
    payload["stats"]["turns"] = MAX_SAFE_INTEGER + 1

    assert not VALIDATOR.is_valid(payload)

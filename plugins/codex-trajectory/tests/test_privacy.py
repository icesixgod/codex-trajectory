"""Detail formatting preserves small JSON and bounds allocation for oversized values."""

from __future__ import annotations

import json
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from codex_trajectory import projection
from codex_trajectory.privacy import DETAIL_LIMIT, json_text
from codex_trajectory.tools import call_tool
from conftest import write_rollout


def measured_details(operation: Callable[[], Any]) -> tuple[Any, int]:
    """Measure transient formatting allocations after the source value already exists."""
    tracemalloc.start()
    try:
        result = operation()
        return result, tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


@pytest.mark.parametrize(
    "value",
    [
        None,
        True,
        123,
        -2.5,
        [],
        {},
        {"text": '中文😀\n\t"\\\x00', "array": [None, False, 3.5, {"nested": []}]},
        {None: "null", False: "false", 2: "integer", 3.5: "float"},
        (1, {"tuple": (2, 3)}),
    ],
)
def test_small_json_keeps_standard_pretty_format(value: Any) -> None:
    expected = json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2)
    assert json_text(value) == expected
    assert json_text(json.dumps(value, ensure_ascii=False)) == expected


def nested_array() -> list[Any]:
    value: list[Any] = [0] * 100_000
    for _ in range(200):
        value = [value]
    return value


@pytest.mark.parametrize("encoded", [False, True])
def test_deep_wide_json_formats_only_bounded_prefix(encoded: bool) -> None:
    value: Any = nested_array()
    if encoded:
        value = json.dumps(value, separators=(",", ":"))
    text, peak = measured_details(lambda: json_text(value))
    assert len(text) == DETAIL_LIMIT
    assert text.endswith("… truncated additional characters")
    assert peak < 4 * 1024 * 1024


@pytest.mark.parametrize("as_key", [False, True])
def test_large_escaped_json_strings_do_not_allocate_complete_encoding(as_key: bool) -> None:
    text = "\x00" * 2_000_000
    value = {text: "value"} if as_key else {"value": text}
    result, peak = measured_details(lambda: json_text(value))
    assert len(result) == DETAIL_LIMIT
    assert "\\u0000" in result
    assert result.endswith("… truncated additional characters")
    assert peak < 1024 * 1024


@pytest.mark.parametrize("extra", [0, 1])
def test_detail_budget_boundary_preserves_exact_fit(extra: int) -> None:
    value = ["x" * (DETAIL_LIMIT - 8 + extra)]
    expected = json.dumps(value, ensure_ascii=False, indent=2)
    assert len(expected) == DETAIL_LIMIT + extra
    if not extra:
        assert json_text(value) == expected
    else:
        result = json_text(value)
        prefix, _ = result.split("\n\n… truncated")
        assert result.startswith(expected[: len(prefix)])
        assert len(result) == DETAIL_LIMIT


def test_non_json_and_cyclic_fallback_remains_bounded() -> None:
    value = nested_array()
    value.append({1, 2})
    invalid = {"invalid": {1, 2}, "wide": value}
    result, peak = measured_details(lambda: json_text(invalid))
    assert result.startswith("{")
    assert len(result) <= DETAIL_LIMIT
    assert peak < 1024 * 1024
    cycle: list[Any] = []
    cycle.append(cycle)
    assert "..." in json_text(cycle)
    assert json_text(float("nan")) == "nan"


def test_full_tool_read_bounds_formatting_memory_and_preserves_privacy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    path = write_rollout(
        tmp_path / "sessions" / "review.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {"id": "detail-memory", "base_instructions": "private-instructions"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "review",
                    "call_id": "memory-test",
                    "arguments": "{}",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "memory-test",
                    "output": nested_array(),
                },
            },
        ],
    )
    try:
        summary = call_tool("get_codex_trajectory", {"sessionId": "detail-memory"})
        assert summary["structuredContent"]["records"][0]["output"] is None
        assert path.stat().st_size < 512 * 1024
        full, peak = measured_details(
            lambda: call_tool(
                "get_codex_trajectory", {"sessionId": "detail-memory", "detailLevel": "full"}
            )
        )
        assert not full.get("isError")
        output = full["structuredContent"]["records"][0]["output"]
        assert len(output) == DETAIL_LIMIT
        assert output.endswith("… truncated additional characters")
        assert "private-instructions" not in json.dumps(full)
        assert peak < 8 * 1024 * 1024
    finally:
        projection._close_page_indexes()

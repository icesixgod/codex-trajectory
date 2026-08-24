"""Tests for standard API token-cost estimation."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest
from codex_trajectory.pricing import (
    MODEL_PRICES_USD_PER_MILLION,
    PRICING_SOURCE,
    PUBLISHED_DATED_MODEL_SNAPSHOTS,
    canonical_priced_model,
    estimate_usage_cost,
    merge_cost_estimates,
)

OFFICIAL_STANDARD_PRICES: dict[str, tuple[str, str | None, str]] = {
    "gpt-5.6": ("4", "0.4", "20"),
    "gpt-5.6-sol": ("4", "0.4", "20"),
    "gpt-5.6-terra": ("2", "0.2", "12"),
    "gpt-5.6-luna": ("0.2", "0.02", "1.2"),
    "gpt-5.5": ("5", "0.5", "30"),
    "gpt-5.5-pro": ("30", None, "180"),
    "gpt-5.4": ("2.5", "0.25", "15"),
    "gpt-5.4-mini": ("0.75", "0.075", "4.5"),
    "gpt-5.4-nano": ("0.2", "0.02", "1.25"),
    "gpt-5.4-pro": ("30", None, "180"),
    "gpt-5.3-codex": ("1.75", "0.175", "14"),
    "gpt-5.2": ("1.75", "0.175", "14"),
    "gpt-5.2-codex": ("1.75", "0.175", "14"),
    "gpt-5.2-chat-latest": ("1.75", "0.175", "14"),
    "gpt-5.2-pro": ("21", None, "168"),
    "gpt-5.1": ("1.25", "0.125", "10"),
    "gpt-5.1-codex": ("1.25", "0.125", "10"),
    "gpt-5.1-codex-max": ("1.25", "0.125", "10"),
    "gpt-5.1-codex-mini": ("0.25", "0.025", "2"),
    "gpt-5.1-chat-latest": ("1.25", "0.125", "10"),
    "gpt-5": ("1.25", "0.125", "10"),
    "gpt-5-codex": ("1.25", "0.125", "10"),
    "gpt-5-chat-latest": ("1.25", "0.125", "10"),
    "gpt-5-pro": ("15", None, "120"),
    "gpt-5-mini": ("0.25", "0.025", "2"),
    "gpt-5-nano": ("0.05", "0.005", "0.4"),
    "gpt-4.1": ("2", "0.5", "8"),
    "gpt-4.1-mini": ("0.4", "0.1", "1.6"),
    "gpt-4.1-nano": ("0.1", "0.025", "0.4"),
    "o3": ("2", "0.5", "8"),
    "o4-mini": ("1.1", "0.275", "4.4"),
    "codex-mini-latest": ("1.5", "0.375", "6"),
}

OFFICIAL_DATED_SNAPSHOTS = {
    "gpt-5.5-2026-04-23": "gpt-5.5",
    "gpt-5.5-pro-2026-04-23": "gpt-5.5-pro",
    "gpt-5.4-2026-03-05": "gpt-5.4",
    "gpt-5.4-mini-2026-03-17": "gpt-5.4-mini",
    "gpt-5.4-nano-2026-03-17": "gpt-5.4-nano",
    "gpt-5.4-pro-2026-03-05": "gpt-5.4-pro",
    "gpt-5.2-2025-12-11": "gpt-5.2",
    "gpt-5.2-pro-2025-12-11": "gpt-5.2-pro",
    "gpt-5.1-2025-11-13": "gpt-5.1",
    "gpt-5-2025-08-07": "gpt-5",
    "gpt-5-pro-2025-10-06": "gpt-5-pro",
    "gpt-5-mini-2025-08-07": "gpt-5-mini",
    "gpt-5-nano-2025-08-07": "gpt-5-nano",
    "gpt-4.1-2025-04-14": "gpt-4.1",
    "gpt-4.1-mini-2025-04-14": "gpt-4.1-mini",
    "gpt-4.1-nano-2025-04-14": "gpt-4.1-nano",
    "o3-2025-04-16": "o3",
    "o4-mini-2025-04-16": "o4-mini",
}


def test_complete_official_standard_price_matrix() -> None:
    actual = {
        model: (price.input, price.cached_input, price.output)
        for model, price in MODEL_PRICES_USD_PER_MILLION.items()
    }
    expected = {
        model: (
            Decimal(input_rate),
            Decimal(cached_rate) if cached_rate is not None else None,
            Decimal(output_rate),
        )
        for model, (input_rate, cached_rate, output_rate) in OFFICIAL_STANDARD_PRICES.items()
    }
    assert actual == expected

    for model, (input_rate, cached_rate, output_rate) in OFFICIAL_STANDARD_PRICES.items():
        uncached = estimate_usage_cost(
            model,
            {"input_tokens": 1_000_000, "cached_input_tokens": 0, "output_tokens": 1_000_000},
        )
        assert uncached["coverage"] == "complete"
        assert uncached["uncachedInputUsd"] == float(Decimal(input_rate))
        assert uncached["cachedInputUsd"] == 0.0
        assert uncached["outputUsd"] == float(Decimal(output_rate))
        assert uncached["totalUsd"] == float(Decimal(input_rate) + Decimal(output_rate))

        cached = estimate_usage_cost(
            model,
            {"input_tokens": 1_000_000, "cached_input_tokens": 1_000_000, "output_tokens": 0},
        )
        if cached_rate is None:
            assert cached["coverage"] == "unavailable"
            assert cached["totalUsd"] is None
            assert cached["pricedModelCalls"] == 0
            assert cached["unpricedModelCalls"] == 1
        else:
            assert cached["coverage"] == "complete"
            assert cached["cachedInputUsd"] == float(Decimal(cached_rate))
            assert cached["totalUsd"] == float(Decimal(cached_rate))


def test_only_explicit_official_dated_snapshots_are_priced() -> None:
    assert PUBLISHED_DATED_MODEL_SNAPSHOTS == OFFICIAL_DATED_SNAPSHOTS
    assert {
        snapshot: canonical_priced_model(snapshot) for snapshot in OFFICIAL_DATED_SNAPSHOTS
    } == OFFICIAL_DATED_SNAPSHOTS
    for unknown in (
        "gpt-5-2025-08-08",
        "gpt-5-1900-01-01",
        "gpt-5.6-sol-2026-08-24",
        "gpt-5.3-codex-2026-02-30",
    ):
        assert canonical_priced_model(unknown) is None


def test_prices_uncached_cached_and_output_tokens_without_double_counting_reasoning() -> None:
    estimate = estimate_usage_cost(
        "gpt-5-2025-08-07",
        {
            "input_tokens": 1_000_000,
            "cached_input_tokens": 250_000,
            "output_tokens": 100_000,
            "reasoning_output_tokens": 80_000,
            "total_tokens": 1_100_000,
        },
    )

    assert canonical_priced_model(" GPT-5-2025-08-07 ") == "gpt-5"
    assert estimate == {
        "currency": "USD",
        "estimated": True,
        "totalUsd": 1.96875,
        "uncachedInputUsd": 0.9375,
        "cachedInputUsd": 0.03125,
        "outputUsd": 1.0,
        "coverage": "complete",
        "pricedModelCalls": 1,
        "unpricedModelCalls": 0,
        "pricingUpdatedAt": "2026-08-24",
    }
    assert PRICING_SOURCE == "https://developers.openai.com/api/docs/pricing"


def test_current_gpt_5_6_standard_short_context_prices() -> None:
    usage = {
        "input_tokens": 1_000_000,
        "cached_input_tokens": 250_000,
        "output_tokens": 100_000,
    }

    expected = {
        "gpt-5.6": (5.1, 3.0, 0.1, 2.0),
        "gpt-5.6-sol": (5.1, 3.0, 0.1, 2.0),
        "gpt-5.6-terra": (2.75, 1.5, 0.05, 1.2),
        "gpt-5.6-luna": (0.275, 0.15, 0.005, 0.12),
    }

    for model, components in expected.items():
        estimate = estimate_usage_cost(model, usage)
        assert (
            estimate["totalUsd"],
            estimate["uncachedInputUsd"],
            estimate["cachedInputUsd"],
            estimate["outputUsd"],
        ) == components


def test_codex_only_alias_without_published_api_price_is_not_guessed() -> None:
    estimate = estimate_usage_cost(
        "gpt-5.3-codex-spark",
        {"input_tokens": 100, "cached_input_tokens": 50, "output_tokens": 10},
    )

    assert canonical_priced_model("gpt-5.3-codex-spark") is None
    assert estimate["coverage"] == "unavailable"
    assert estimate["totalUsd"] is None
    assert estimate["pricedModelCalls"] == 0
    assert estimate["unpricedModelCalls"] == 1


def test_merge_cost_estimates_marks_known_total_as_partial() -> None:
    known = estimate_usage_cost(
        "gpt-5.6-terra",
        {"input_tokens": 1_000, "cached_input_tokens": 800, "output_tokens": 100},
    )
    unknown = estimate_usage_cost("private-model", {"input_tokens": 10, "output_tokens": 2})

    estimate = merge_cost_estimates(known, unknown)

    assert estimate["coverage"] == "partial"
    assert estimate["totalUsd"] == 0.00176
    assert estimate["uncachedInputUsd"] == 0.0004
    assert estimate["cachedInputUsd"] == 0.00016
    assert estimate["outputUsd"] == 0.0012
    assert estimate["pricedModelCalls"] == 1
    assert estimate["unpricedModelCalls"] == 1


@pytest.mark.parametrize(
    "usage",
    [
        {"total_tokens": 12},
        {"input_tokens": 12},
        {"output_tokens": 12},
        {"input_tokens": 12, "output_tokens": None},
        {"input_tokens": None, "output_tokens": 12},
        {"input_tokens": 12, "cached_input_tokens": -1, "output_tokens": 2},
        {"input_tokens": 12, "cached_input_tokens": 13, "output_tokens": 2},
    ],
)
def test_incomplete_or_inconsistent_billable_breakdown_is_unavailable(
    usage: dict[str, object],
) -> None:
    estimate = estimate_usage_cost("gpt-5", usage)

    assert estimate["coverage"] == "unavailable"
    assert estimate["totalUsd"] is None
    assert estimate["pricedModelCalls"] == 0
    assert estimate["unpricedModelCalls"] == 1


def test_unpublished_cached_input_rate_is_not_guessed() -> None:
    without_cache = estimate_usage_cost(
        "gpt-5-pro", {"input_tokens": 1_000_000, "output_tokens": 100_000}
    )
    with_cache = estimate_usage_cost(
        "gpt-5-pro",
        {"input_tokens": 1_000_000, "cached_input_tokens": 250_000, "output_tokens": 100_000},
    )

    assert without_cache["coverage"] == "complete"
    assert without_cache["totalUsd"] == 27.0
    assert without_cache["cachedInputUsd"] == 0.0
    assert with_cache["coverage"] == "unavailable"
    assert with_cache["totalUsd"] is None


def test_large_valid_cost_aggregate_does_not_exhaust_decimal_precision() -> None:
    call = estimate_usage_cost(
        "gpt-5-pro",
        {"input_tokens": 2**53 - 1, "output_tokens": 2**53 - 1},
    )
    aggregate = None
    for _ in range(9_000):
        aggregate = merge_cost_estimates(aggregate, call)

    assert aggregate["coverage"] == "complete"
    assert aggregate["pricedModelCalls"] == 9_000
    assert math.isfinite(aggregate["totalUsd"])
    assert aggregate["totalUsd"] > call["totalUsd"]

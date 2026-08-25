"""Estimate standard OpenAI API text-token costs for projected model calls."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any

PRICING_UPDATED_AT = "2026-08-24"
PRICING_SOURCE = "https://developers.openai.com/api/docs/pricing"
MILLION_TOKENS = Decimal(1_000_000)
_COST_PRECISION = Decimal("0.000000000001")


@dataclass(frozen=True)
class ModelPrice:
    """USD rates per one million standard API text tokens."""

    input: Decimal
    cached_input: Decimal | None
    output: Decimal


def _price(input_rate: str, cached_rate: str | None, output_rate: str) -> ModelPrice:
    return ModelPrice(
        Decimal(input_rate),
        Decimal(cached_rate) if cached_rate is not None else None,
        Decimal(output_rate),
    )


# This intentionally remains an explicit allowlist. Codex-only aliases without a published
# API price (for example, gpt-5.3-codex-spark) stay unpriced instead of inheriting a guessed
# rate. Only dated snapshots explicitly published in the model catalog resolve to a base model.
MODEL_PRICES_USD_PER_MILLION: dict[str, ModelPrice] = {
    # The current standard short-context Sol rate is promotional through at least 2026-11-21.
    "gpt-5.6": _price("4", "0.4", "20"),
    "gpt-5.6-sol": _price("4", "0.4", "20"),
    "gpt-5.6-terra": _price("2", "0.2", "12"),
    "gpt-5.6-luna": _price("0.2", "0.02", "1.2"),
    "gpt-5.5": _price("5", "0.5", "30"),
    "gpt-5.5-pro": _price("30", None, "180"),
    "gpt-5.4": _price("2.5", "0.25", "15"),
    "gpt-5.4-mini": _price("0.75", "0.075", "4.5"),
    "gpt-5.4-nano": _price("0.2", "0.02", "1.25"),
    "gpt-5.4-pro": _price("30", None, "180"),
    "gpt-5.3-codex": _price("1.75", "0.175", "14"),
    "gpt-5.2": _price("1.75", "0.175", "14"),
    "gpt-5.2-codex": _price("1.75", "0.175", "14"),
    "gpt-5.2-chat-latest": _price("1.75", "0.175", "14"),
    "gpt-5.2-pro": _price("21", None, "168"),
    "gpt-5.1": _price("1.25", "0.125", "10"),
    "gpt-5.1-codex": _price("1.25", "0.125", "10"),
    "gpt-5.1-codex-max": _price("1.25", "0.125", "10"),
    "gpt-5.1-codex-mini": _price("0.25", "0.025", "2"),
    "gpt-5.1-chat-latest": _price("1.25", "0.125", "10"),
    "gpt-5": _price("1.25", "0.125", "10"),
    "gpt-5-codex": _price("1.25", "0.125", "10"),
    "gpt-5-chat-latest": _price("1.25", "0.125", "10"),
    "gpt-5-pro": _price("15", None, "120"),
    "gpt-5-mini": _price("0.25", "0.025", "2"),
    "gpt-5-nano": _price("0.05", "0.005", "0.4"),
    "gpt-4.1": _price("2", "0.5", "8"),
    "gpt-4.1-mini": _price("0.4", "0.1", "1.6"),
    "gpt-4.1-nano": _price("0.1", "0.025", "0.4"),
    "o3": _price("2", "0.5", "8"),
    "o4-mini": _price("1.1", "0.275", "4.4"),
    "codex-mini-latest": _price("1.5", "0.375", "6"),
}

PUBLISHED_DATED_MODEL_SNAPSHOTS: dict[str, str] = {
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


def canonical_priced_model(model: Any) -> str | None:
    """Resolve an exact published model ID or one of its dated API snapshots."""
    if not isinstance(model, str):
        return None
    normalized = model.strip().casefold()
    if normalized in MODEL_PRICES_USD_PER_MILLION:
        return normalized
    return PUBLISHED_DATED_MODEL_SNAPSHOTS.get(normalized)


def _token_counter(usage: Mapping[str, Any], name: str) -> int:
    value = usage.get(name, 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _valid_token_counter(usage: Mapping[str, Any], name: str) -> bool:
    value = usage.get(name)
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _decimal_exponent(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    return exponent if isinstance(exponent, int) else 0


def _decimal_sum(*values: Decimal) -> Decimal:
    """Add bounded monetary values without the process-wide Decimal precision limit."""
    if not values:
        return Decimal(0)
    integer_digits = max(1, max(value.adjusted() + 1 for value in values))
    fractional_digits = max(0, max(-_decimal_exponent(value) for value in values))
    with localcontext() as context:
        context.prec = max(28, integer_digits + fractional_digits + len(str(len(values))) + 1)
        return sum(values, Decimal(0))


def _usd(value: Decimal) -> float:
    integer_digits = max(1, value.adjusted() + 1)
    with localcontext() as context:
        context.prec = max(28, integer_digits + 14)
        return float(value.quantize(_COST_PRECISION))


def _coverage(priced_calls: int, unpriced_calls: int) -> str:
    if priced_calls == 0:
        return "unavailable"
    return "partial" if unpriced_calls else "complete"


def _cost_estimate(
    *,
    uncached_input_usd: Decimal | None,
    cached_input_usd: Decimal | None,
    output_usd: Decimal | None,
    priced_calls: int,
    unpriced_calls: int,
) -> dict[str, Any]:
    if priced_calls:
        uncached = uncached_input_usd or Decimal(0)
        cached = cached_input_usd or Decimal(0)
        output = output_usd or Decimal(0)
        total = _decimal_sum(uncached, cached, output)
        component_values: tuple[float | None, float | None, float | None, float | None] = (
            _usd(total),
            _usd(uncached),
            _usd(cached),
            _usd(output),
        )
    else:
        component_values = (None, None, None, None)
    total_value, uncached_value, cached_value, output_value = component_values
    return {
        "currency": "USD",
        "estimated": True,
        "totalUsd": total_value,
        "uncachedInputUsd": uncached_value,
        "cachedInputUsd": cached_value,
        "outputUsd": output_value,
        "coverage": _coverage(priced_calls, unpriced_calls),
        "pricedModelCalls": priced_calls,
        "unpricedModelCalls": unpriced_calls,
        "pricingUpdatedAt": PRICING_UPDATED_AT,
    }


def estimate_usage_cost(model: Any, usage: Mapping[str, Any]) -> dict[str, Any]:
    """Price one model-call usage sample, or mark it explicitly as unpriced."""
    canonical_model = canonical_priced_model(model)
    has_billable_breakdown = all(
        _valid_token_counter(usage, name) for name in ("input_tokens", "output_tokens")
    )
    cached_counter_is_valid = "cached_input_tokens" not in usage or _valid_token_counter(
        usage, "cached_input_tokens"
    )
    input_tokens = _token_counter(usage, "input_tokens")
    cached_tokens = _token_counter(usage, "cached_input_tokens")
    output_tokens = _token_counter(usage, "output_tokens")
    if (
        canonical_model is None
        or not has_billable_breakdown
        or not cached_counter_is_valid
        or cached_tokens > input_tokens
    ):
        return _cost_estimate(
            uncached_input_usd=None,
            cached_input_usd=None,
            output_usd=None,
            priced_calls=0,
            unpriced_calls=1,
        )

    price = MODEL_PRICES_USD_PER_MILLION[canonical_model]
    if cached_tokens and price.cached_input is None:
        return _cost_estimate(
            uncached_input_usd=None,
            cached_input_usd=None,
            output_usd=None,
            priced_calls=0,
            unpriced_calls=1,
        )
    uncached_tokens = input_tokens - cached_tokens
    return _cost_estimate(
        uncached_input_usd=Decimal(uncached_tokens) * price.input / MILLION_TOKENS,
        cached_input_usd=(
            Decimal(cached_tokens) * price.cached_input / MILLION_TOKENS
            if price.cached_input is not None
            else Decimal(0)
        ),
        output_usd=Decimal(output_tokens) * price.output / MILLION_TOKENS,
        priced_calls=1,
        unpriced_calls=0,
    )


def merge_cost_estimates(current: Any, update: Mapping[str, Any]) -> dict[str, Any]:
    """Combine call or turn estimates while retaining explicit pricing coverage."""
    current_mapping = current if isinstance(current, Mapping) else {}
    priced_calls = _token_counter(current_mapping, "pricedModelCalls") + _token_counter(
        update, "pricedModelCalls"
    )
    unpriced_calls = _token_counter(current_mapping, "unpricedModelCalls") + _token_counter(
        update, "unpricedModelCalls"
    )

    def component(name: str) -> Decimal:
        values: list[Decimal] = []
        for source in (current_mapping, update):
            value = source.get(name)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                decimal = Decimal(str(value))
                if decimal.is_finite() and decimal >= 0:
                    values.append(decimal)
        return _decimal_sum(*values)

    return _cost_estimate(
        uncached_input_usd=component("uncachedInputUsd"),
        cached_input_usd=component("cachedInputUsd"),
        output_usd=component("outputUsd"),
        priced_calls=priced_calls,
        unpriced_calls=unpriced_calls,
    )

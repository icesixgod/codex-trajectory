"""Strict, resource-bounded JSON decoding shared by runtime boundaries."""

from __future__ import annotations

import json
import math
from typing import Any

MAX_JSON_INTEGER_DIGITS = 256
MAX_JSON_NESTING_DEPTH = 256


def _validate_json_nesting(value: str) -> None:
    """Reject excessive container nesting independently of Python's recursion limit."""
    depth = 0
    in_string = False
    escaped = False
    for character in value:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_NESTING_DEPTH:
                raise ValueError("JSON nesting exceeds the supported depth limit")
        elif character in "]}":
            depth -= 1


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def _bounded_json_integer(value: str) -> int:
    digits = value[1:] if value.startswith("-") else value
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError("JSON integer exceeds the supported precision limit")
    return int(value)


def _finite_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("JSON number exceeds the supported numeric range")
    return number


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def strict_json_loads(value: str) -> Any:
    """Decode interoperable JSON while bounding numeric conversion work."""
    _validate_json_nesting(value)
    return json.loads(
        value,
        parse_constant=_reject_json_constant,
        parse_float=_finite_json_float,
        parse_int=_bounded_json_integer,
        object_pairs_hook=_unique_json_object,
    )

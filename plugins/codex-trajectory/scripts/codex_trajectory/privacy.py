"""Privacy policy and bounded text helpers for trajectory projections."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal, cast

DETAIL_LIMIT = 12_000
SUMMARY_LIMIT = 220
DetailLevel = Literal["summary", "full"]


def normalize_detail_level(value: Any) -> DetailLevel:
    """Validate and normalize the public detail-level parameter."""
    if value not in {"summary", "full"}:
        raise ValueError("detailLevel must be 'summary' or 'full'.")
    return cast(DetailLevel, value)


def shorten(value: str, limit: int = SUMMARY_LIMIT) -> str:
    """Collapse whitespace and bound a model-visible summary."""
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def bounded(value: str, limit: int = DETAIL_LIMIT) -> str:
    """Bound a detail field while reporting the omitted character count."""
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n\n… truncated {len(value) - limit:,} characters"


def json_text(value: Any) -> str:
    """Render a JSON-compatible value for the full-detail inspector."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return bounded(value)
        return bounded(json.dumps(parsed, ensure_ascii=False, indent=2))
    try:
        return bounded(json.dumps(value, ensure_ascii=False, indent=2))
    except (TypeError, ValueError):
        return bounded(str(value))


def content_text(content: Any) -> str:
    """Extract visible text from a Responses-style content array."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
        elif isinstance(item, dict) and item.get("type") in {"input_image", "image"}:
            parts.append("[image]")
    return "\n\n".join(part for part in parts if part)


def reasoning_summary(summary: Any) -> str:
    """Extract persisted summaries without accessing encrypted reasoning."""
    if isinstance(summary, str):
        return summary
    if not isinstance(summary, list):
        return ""
    parts: list[str] = []
    for item in summary:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and isinstance(item.get("text"), str):
            parts.append(item["text"])
    return "\n\n".join(parts)


def display_path(value: Any) -> str | None:
    """Return a local path without exposing the user's absolute home path."""
    if not isinstance(value, str):
        return None
    try:
        path = Path(value).expanduser()
        home = Path.home().resolve()
        resolved = path.resolve(strict=False)
        if resolved == home:
            return "~"
        if resolved.is_relative_to(home):
            return str(Path("~") / resolved.relative_to(home))
        if resolved.is_absolute():
            return str(Path("<absolute>") / resolved.name)
    except (OSError, RuntimeError, ValueError):
        pass
    return value


def safe_git(value: Any) -> dict[str, str] | None:
    """Retain non-secret Git identity while excluding repository remotes."""
    if not isinstance(value, dict):
        return None
    result = {
        key: value[key] for key in ("branch", "commit_hash") if isinstance(value.get(key), str)
    }
    return result or None

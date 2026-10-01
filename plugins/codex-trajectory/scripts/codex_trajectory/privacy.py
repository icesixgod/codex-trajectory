"""Privacy policy and bounded text helpers for trajectory projections."""

from __future__ import annotations

import json
import re
import reprlib
from collections.abc import Iterator
from pathlib import Path, PureWindowsPath
from typing import Any, Literal, cast

from .json_support import MAX_JSON_NESTING_DEPTH, strict_json_loads

DETAIL_LIMIT = 12_000
SUMMARY_LIMIT = 220
DetailLevel = Literal["summary", "full"]


def normalize_detail_level(value: Any) -> DetailLevel:
    """Validate and normalize the public detail-level parameter."""
    if not isinstance(value, str) or value not in {"summary", "full"}:
        raise ValueError("detailLevel must be 'summary' or 'full'.")
    return cast(DetailLevel, value)


def shorten(value: str, limit: int = SUMMARY_LIMIT) -> str:
    """Collapse whitespace and bound a model-visible summary."""
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def safe_text(value: Any, limit: int = SUMMARY_LIMIT) -> str | None:
    """Return a bounded scalar string and reject structured metadata."""
    if not isinstance(value, str):
        return None
    return shorten(value, limit)


def source_kind(value: Any) -> str | None:
    """Expose only a session-source variant name, never its nested payload."""
    allowed = {"cli", "vscode", "exec", "mcp", "custom", "internal", "subagent", "unknown"}
    if isinstance(value, str) and value.casefold() in allowed:
        return value.casefold()
    if isinstance(value, dict) and len(value) == 1:
        key = next(iter(value))
        if isinstance(key, str) and key.casefold() in allowed:
            return key.casefold()
    return None


def bounded(value: str, limit: int = DETAIL_LIMIT) -> str:
    """Bound a detail field while reporting the omitted character count."""
    if len(value) <= limit:
        return value
    return value[:limit] + f"\n\n… truncated {len(value) - limit:,} characters"


def _json_chunks(value: Any, depth: int, active: set[int]) -> Iterator[str]:
    """Match indented JSON formatting without allocating entire strings or containers."""
    if isinstance(value, str):
        yield '"'
        for start in range(0, len(value), 1024):
            yield json.dumps(value[start : start + 1024], ensure_ascii=False)[1:-1]
        yield '"'
    elif isinstance(value, (dict, list, tuple)):
        if depth >= MAX_JSON_NESTING_DEPTH or id(value) in active:
            raise ValueError("Unsupported JSON container depth or cycle.")
        mapping = isinstance(value, dict)
        opening, closing = ("{", "}") if mapping else ("[", "]")
        if not value:
            yield opening + closing
            return
        active.add(id(value))
        try:
            indent = "  " * (depth + 1)
            yield opening + "\n" + indent
            for index, entry in enumerate(value.items() if isinstance(value, dict) else value):
                if index:
                    yield ",\n" + indent
                if mapping:
                    key, child = entry
                    if not isinstance(key, str):
                        if key is None or isinstance(key, (bool, int, float)):
                            key = json.dumps(key, allow_nan=False)
                        else:
                            raise TypeError("Unsupported JSON object key.")
                    yield from _json_chunks(key, depth + 1, active)
                    yield ": "
                else:
                    child = entry
                yield from _json_chunks(child, depth + 1, active)
            yield "\n" + "  " * depth + closing
        finally:
            active.remove(id(value))
    elif value is None or isinstance(value, (bool, int, float)):
        yield json.dumps(value, allow_nan=False)
    else:
        raise TypeError("Unsupported JSON value.")


def _bounded_json(value: Any) -> str:
    """Stop formatting at the detail budget; do not traverse omitted values to count them."""
    parts: list[str] = []
    remaining = DETAIL_LIMIT
    for chunk in _json_chunks(value, 0, set()):
        if len(chunk) > remaining:
            parts.append(chunk[:remaining])
            notice = "\n\n… truncated additional characters"
            return "".join(parts)[: DETAIL_LIMIT - len(notice)] + notice
        parts.append(chunk)
        remaining -= len(chunk)
    return "".join(parts)


def json_text(value: Any) -> str:
    """Render only the bounded JSON prefix needed by the full-detail inspector."""
    if isinstance(value, str):
        try:
            parsed = strict_json_loads(value)
        except (RecursionError, TypeError, ValueError):
            return bounded(value)
        try:
            return _bounded_json(parsed)
        except (RecursionError, TypeError, ValueError):
            return bounded(value)
    try:
        return _bounded_json(value)
    except (RecursionError, TypeError, ValueError):
        # Non-JSON fallback must not expand a complete large or cyclic container either.
        return bounded(reprlib.repr(value))


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
        elif isinstance(item, dict) and item.get("type") in {
            "input_image",
            "image",
            "local_image",
        }:
            parts.append("[image]")
        elif isinstance(item, dict) and item.get("type") in {
            "input_audio",
            "audio",
            "local_audio",
        }:
            parts.append("[audio]")
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

    def portable(path: Path) -> str:
        return path.as_posix()

    if not isinstance(value, str):
        return None
    try:
        path = Path(value).expanduser()
        foreign_windows_path = PureWindowsPath(value)
        if foreign_windows_path.is_absolute() and not path.is_absolute():
            name = foreign_windows_path.name
            return safe_text(f"<absolute>/{name}" if name else "<absolute>", 500)
        home = Path.home().resolve()
        resolved = path.resolve(strict=False)
        if resolved == home:
            return "~"
        if resolved.is_relative_to(home):
            return safe_text(portable(Path("~") / resolved.relative_to(home)), 500)
        if resolved.is_absolute():
            return safe_text(portable(Path("<absolute>") / resolved.name), 500)
    except (OSError, RuntimeError, ValueError):
        lexical = Path(value)
        if lexical.is_absolute():
            return safe_text(portable(Path("<absolute>") / lexical.name), 500)
    return safe_text(value, 500)


def safe_git(value: Any) -> dict[str, str] | None:
    """Retain non-secret Git identity while excluding repository remotes."""
    if not isinstance(value, dict):
        return None
    result: dict[str, str] = {}
    branch = safe_text(value.get("branch"), 200)
    if branch:
        result["branch"] = branch
    commit_hash = value.get("commit_hash")
    if isinstance(commit_hash, str) and re.fullmatch(r"[0-9a-fA-F]{4,64}", commit_hash):
        result["commit_hash"] = commit_hash
    return result or None

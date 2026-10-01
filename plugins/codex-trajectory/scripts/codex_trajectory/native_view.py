"""Per-request task context for the native conversation panel."""

from __future__ import annotations

import re
from typing import Any

from .sessions import MAX_OPT_IN_READ_BYTES, ReadLimitExceeded

_THREAD_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")


def calling_thread_id(metadata: dict[str, Any] | None) -> str | None:
    """Accept only matching, bounded task IDs supplied on this MCP call.

    A server can serve several conversations. Process environment and the anonymized
    openai/session value cannot identify the task that opened this particular panel.
    """
    if metadata is None:
        return None
    identifiers = [metadata[key] for key in ("thread_id", "threadId") if key in metadata]
    if not identifiers:
        return None
    if any(not isinstance(value, str) or not _THREAD_ID.fullmatch(value) for value in identifiers):
        raise ValueError("Invalid calling task context.")
    if len(set(identifiers)) != 1:
        raise ValueError("Conflicting calling task context.")
    return str(identifiers[0])


def session_selection_result(resource_uri: str, reason: str) -> dict[str, Any]:
    """Open a selector instead of silently substituting another task."""
    return {
        "structuredContent": {"viewerState": "select-session", "reason": reason},
        "content": [{"type": "text", "text": "Select a local Codex task to view its trajectory."}],
        "_meta": {"ui": {"resourceUri": resource_uri}},
    }


def read_limit_result(
    session_id: str | None,
    error: ReadLimitExceeded,
    *,
    resource_uri: str | None = None,
    is_error: bool = False,
) -> dict[str, Any]:
    """Let the viewer request a larger task-scoped budget without exposing paths."""
    suggested = ((error.required_bytes + 999_999_999) // 1_000_000_000) * 1_000_000_000
    result: dict[str, Any] = {
        "structuredContent": {
            "viewerState": "read-limit-exceeded",
            "sessionId": session_id,
            "readLimit": {
                "requiredBytes": error.required_bytes,
                "currentBytes": error.limit_bytes,
                "suggestedBytes": min(suggested, MAX_OPT_IN_READ_BYTES),
                "maximumBytes": MAX_OPT_IN_READ_BYTES,
                "atLeast": error.at_least,
            },
        },
        "content": [{"type": "text", "text": str(error)}],
    }
    if resource_uri is not None:
        result["_meta"] = {"ui": {"resourceUri": resource_uri}}
    if is_error:
        result["isError"] = True
    return result

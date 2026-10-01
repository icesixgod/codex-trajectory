"""MCP tool catalog and dispatch, separate from rollout projection."""

from __future__ import annotations

from typing import Any

from . import projection
from .native_view import calling_thread_id, read_limit_result, session_selection_result
from .projection import (
    DEFAULT_MAX_RECORDS,
    MAX_RECORDS,
    MAX_SAFE_INTEGER,
    MIN_RECORDS,
    UI_URI,
)
from .sessions import MAX_JSONL_TOTAL_BYTES, MAX_OPT_IN_READ_BYTES, ReadLimitExceeded
from .viewer_preferences import default_full_details, set_default_full_details


def current_trajectory_result(metadata: dict[str, Any] | None) -> dict[str, Any]:
    """Open the exact calling task, or let the user choose a local task."""
    try:
        session_id = calling_thread_id(metadata)
    except ValueError:
        return session_selection_result(UI_URI, "invalid-context")
    if session_id is None:
        return session_selection_result(UI_URI, "missing-context")
    try:
        result = projection.trajectory_result(
            {"sessionId": session_id, "includeArchived": True},
            with_ui=True,
            exact_session_id=session_id,
        )
    except ReadLimitExceeded as error:
        if not error.session_id_verified or error.session_id != session_id:
            return session_selection_result(UI_URI, "task-unavailable")
        return read_limit_result(session_id, error, resource_uri=UI_URI)
    except (OSError, ValueError):
        return session_selection_result(UI_URI, "task-unavailable")
    if result["structuredContent"]["session"]["id"] != session_id:
        return session_selection_result(UI_URI, "task-unavailable")
    return result


def tool_definitions() -> list[dict[str, Any]]:
    """Return MCP tool metadata."""
    read_only = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    trajectory_properties = {
        "maxReadBytes": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_OPT_IN_READ_BYTES,
            "default": MAX_JSONL_TOTAL_BYTES,
            "description": (
                "Explicit read byte limit for this selected task only. Defaults to 512 MiB; "
                "an override requires an explicit sessionId. Other read limits still apply."
            ),
        },
        "sessionId": {
            "type": "string",
            "minLength": 1,
            "maxLength": 240,
            "description": (
                "Exact or unambiguous-prefix Codex session ID. Omit for the most recently "
                "modified active task, not necessarily the calling task. "
                "Full details require an ID."
            ),
        },
        "maxRecords": {
            "type": "integer",
            "minimum": MIN_RECORDS,
            "maximum": MAX_RECORDS,
            "default": DEFAULT_MAX_RECORDS,
            "description": (
                "Maximum records in one page while preserving stable original indexes."
            ),
        },
        "beforeRecord": {
            "type": "integer",
            "minimum": 1,
            "maximum": MAX_SAFE_INTEGER,
            "description": (
                "Exclusive stable record index for loading the immediately preceding page. "
                "Omit to load the newest tail."
            ),
        },
        "includeArchived": {
            "type": "boolean",
            "default": False,
            "description": "Also resolve sessions from archived_sessions.",
        },
        "detailLevel": {
            "type": "string",
            "enum": ["summary", "full"],
            "default": "summary",
            "description": (
                "Safe summaries by default; full explicitly includes bounded record details."
            ),
        },
    }
    return [
        {
            "name": "list_codex_sessions",
            "title": "List local Codex tasks",
            "description": (
                "List recent task metadata, including a 100-character first-prompt title; "
                "updates a private search cache."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                    "query": {
                        "type": "string",
                        "maxLength": 500,
                        "description": "Filter by ID, title, cwd, or model.",
                    },
                    "includeArchived": {"type": "boolean", "default": False},
                },
                "additionalProperties": False,
            },
            "annotations": {**read_only, "readOnlyHint": False},
        },
        {
            "name": "get_codex_trajectory",
            "title": "Read a Codex trajectory",
            "description": (
                "Return a structured turn-aware trajectory for analysis without rendering UI."
            ),
            "inputSchema": {
                "type": "object",
                "properties": trajectory_properties,
                "additionalProperties": False,
            },
            "annotations": read_only,
        },
        {
            "name": "show_codex_trajectory",
            "title": "Show a Codex trajectory",
            "description": (
                "Render a local Codex task as an interactive timing overview, "
                "event ledger, and inspector."
            ),
            "inputSchema": {
                "type": "object",
                "properties": trajectory_properties,
                "additionalProperties": False,
            },
            "annotations": read_only,
            "_meta": {
                "ui": {"resourceUri": UI_URI},
                "openai/outputTemplate": UI_URI,
                "openai/toolInvocation/invoking": "Building trajectory…",
                "openai/toolInvocation/invoked": "Trajectory ready.",
            },
        },
        {
            "name": "open_codex_trajectory",
            "title": "Codex Trajectory",
            "description": (
                "Open the calling Codex task's safe trajectory in the native side panel."
            ),
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": read_only,
            "_meta": {
                "ui": {"resourceUri": UI_URI, "visibility": ["app"]},
                "openai/ui": {"entrypoints": [{"type": "thread"}]},
                "openai/visibility": "private",
                "openai/outputTemplate": UI_URI,
            },
        },
        {
            "name": "get_codex_trajectory_preferences",
            "title": "Read trajectory viewer preferences",
            "description": "Read the locally saved full-detail default for the viewer only.",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": read_only,
            "_meta": {"ui": {"visibility": ["app"]}, "openai/visibility": "private"},
        },
        {
            "name": "set_codex_trajectory_preferences",
            "title": "Save trajectory viewer preferences",
            "description": "Remember or revoke the user's full-detail default for future panels.",
            "inputSchema": {
                "type": "object",
                "properties": {"defaultFullDetails": {"type": "boolean"}},
                "required": ["defaultFullDetails"],
                "additionalProperties": False,
            },
            "annotations": {**read_only, "readOnlyHint": False},
            "_meta": {"ui": {"visibility": ["app"]}, "openai/visibility": "private"},
        },
        {
            "name": "get_codex_trajectory_update",
            "title": "Refresh the live trajectory window",
            "description": (
                "Return an app-only safe-summary update when a local Codex task changed."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "sessionId": trajectory_properties["sessionId"],
                    "revision": {
                        "type": "string",
                        "pattern": "^[0-9a-f]{64}$",
                        "description": "Opaque revision returned by the previous live update.",
                    },
                    "includeArchived": trajectory_properties["includeArchived"],
                    "maxReadBytes": trajectory_properties["maxReadBytes"],
                },
                "additionalProperties": False,
            },
            "annotations": read_only,
            "_meta": {
                "ui": {"visibility": ["app"]},
                "openai/visibility": "private",
            },
        },
    ]


def call_tool(
    name: str, arguments: Any, *, metadata: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Dispatch one MCP tool call."""
    args = arguments if isinstance(arguments, dict) else {}
    try:
        if name in {"get_codex_trajectory_preferences", "set_codex_trajectory_preferences"}:
            saving = name == "set_codex_trajectory_preferences"
            projection.reject_unknown_arguments(args, {"defaultFullDetails"} if saving else set())
            if saving:
                enabled = args.get("defaultFullDetails")
                if not isinstance(enabled, bool):
                    raise ValueError("defaultFullDetails must be a boolean.")
                set_default_full_details(enabled)
            else:
                enabled = default_full_details()
            return {
                "structuredContent": {"defaultFullDetails": enabled},
                "content": [
                    {
                        "type": "text",
                        "text": "Viewer preferences saved."
                        if saving
                        else "Viewer preferences loaded.",
                    }
                ],
            }
        if name == "open_codex_trajectory":
            projection.reject_unknown_arguments(args, set())
            return current_trajectory_result(metadata)
        if name == "list_codex_sessions":
            projection.reject_unknown_arguments(args, {"limit", "query", "includeArchived"})
            limit = args.get("limit", 20)
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise ValueError("limit must be an integer.")
            if not 1 <= limit <= 100:
                raise ValueError("limit must be between 1 and 100.")
            query = args.get("query", "")
            if not isinstance(query, str):
                raise ValueError("query must be a string.")
            if len(query) > 500:
                raise ValueError("query must contain at most 500 characters.")
            include_archived = args.get("includeArchived", False)
            if not isinstance(include_archived, bool):
                raise ValueError("includeArchived must be a boolean.")
            sessions = projection.list_session_overviews(
                limit=limit,
                query=query,
                include_archived=include_archived,
            )
            return {
                "structuredContent": {"sessions": sessions, "count": len(sessions)},
                "content": [{"type": "text", "text": f"Found {len(sessions)} local Codex tasks."}],
            }
        if name == "get_codex_trajectory":
            return projection.trajectory_result(args, with_ui=False)
        if name == "show_codex_trajectory":
            return projection.trajectory_result(args, with_ui=True)
        if name == "get_codex_trajectory_update":
            return projection.trajectory_update_result(args)
        raise ValueError(f"Unknown tool {name!r}.")
    except ReadLimitExceeded as error:
        return read_limit_result(
            error.session_id,
            error,
            resource_uri=UI_URI if name == "show_codex_trajectory" else None,
            is_error=True,
        )
    except OSError:
        return {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": "Could not save viewer preferences."
                    if name == "set_codex_trajectory_preferences"
                    else "Could not read local Codex task data.",
                }
            ],
        }
    except ValueError as error:
        return {
            "isError": True,
            "content": [{"type": "text", "text": str(error)}],
        }

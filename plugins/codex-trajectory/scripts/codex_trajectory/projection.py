#!/usr/bin/env python3
"""Project local Codex rollout logs into privacy-aware trajectories."""

from __future__ import annotations

import atexit
import hashlib
import json
import math
import sqlite3
import threading
from base64 import b64encode
from collections import OrderedDict, deque
from collections.abc import Iterator
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .command_summary import command_activity
from .json_support import strict_json_loads
from .page_index import PageIndex
from .pricing import estimate_usage_cost, merge_cost_estimates, normalized_service_tier
from .privacy import (
    DetailLevel,
    bounded,
    content_text,
    display_path,
    json_text,
    normalize_detail_level,
    reasoning_summary,
    safe_git,
    safe_text,
    shorten,
    source_kind,
)
from .session_index import SessionSearchIndex
from .sessions import (
    ReadLimitExceeded,
    first_session_metadata,
    is_archived_session,
    iter_session_jsonl,
    iter_session_search_jsonl,
    jsonl_byte_limit,
    recent_rate_limit_entries,
    rollout_id_from_path,
    session_files,
    session_read_limit,
    session_scan,
    session_signature,
    thread_id_from_path,
)

SERVER_NAME = "codex-trajectory"
SERVER_VERSION = __version__
TRAJECTORY_SCHEMA_VERSION = 2
UI_URI = "ui://codex-trajectory/trajectory-v2.html"
DEFAULT_MAX_RECORDS = 500
LIVE_MAX_RECORDS = 50
MIN_RECORDS = 50
MAX_RECORDS = 1_000
MAX_TURNS = 1_000
MAX_WARNINGS = 100
MAX_OVERVIEW_CACHE = 256
MAX_TRAJECTORY_CACHE = 16
MAX_TRAJECTORY_CACHE_BYTES = 24 * 1024 * 1024
MAX_TRAJECTORY_PAGE_BYTES = 5 * 1024 * 1024
MAX_TRACKED_CALLS = 4_096
MAX_SAFE_INTEGER = 2**53 - 1
_SESSION_OVERVIEW_CACHE: OrderedDict[
    Path, tuple[tuple[tuple[str, int, int, int], ...], dict[str, Any]]
] = OrderedDict()
_SESSION_OVERVIEW_CACHE_LOCK = threading.Lock()
_TRAJECTORY_CACHE: OrderedDict[
    tuple[
        Path,
        tuple[tuple[str, int, int, int], ...],
        int,
        DetailLevel,
        int | None,
        int,
    ],
    dict[str, Any],
] = OrderedDict()
_TRAJECTORY_CACHE_LOCK = threading.Lock()
_TRAJECTORY_CACHE_SIZES: dict[Any, int] = {}

_PAGE_INDEXES: OrderedDict[Any, PageIndex] = OrderedDict()
_PAGE_INDEX_LOCK = threading.RLock()
MAX_PAGE_INDEXES = 4
MAX_PAGE_INDEX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_QUOTA_FILES = 32
_QUOTA_CACHE: OrderedDict[Path, tuple[tuple[int, ...], dict[str, Any] | None]] = OrderedDict()
_QUOTA_CACHE_LOCK = threading.Lock()


def _close_page_indexes() -> None:
    """Close SQLite handles before temporary-directory cleanup (also on Windows)."""
    with _PAGE_INDEX_LOCK:
        for index in _PAGE_INDEXES.values():
            index.close()
        _PAGE_INDEXES.clear()


atexit.register(_close_page_indexes)


def parse_timestamp(value: Any) -> int | None:
    """Convert an ISO timestamp or Unix value to epoch milliseconds."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (OverflowError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        try:
            milliseconds = round(number * 1000) if abs(number) < 10_000_000_000 else round(number)
        except (OverflowError, ValueError):
            return None
        return milliseconds if abs(milliseconds) <= MAX_SAFE_INTEGER else None
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        milliseconds = round(parsed.timestamp() * 1000)
    except (OverflowError, OSError, ValueError):
        return None
    return milliseconds if abs(milliseconds) <= MAX_SAFE_INTEGER else None


def iso_timestamp(milliseconds: int | None) -> str | None:
    """Format epoch milliseconds as an ISO timestamp."""
    if milliseconds is None:
        return None
    try:
        return (
            datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except (OverflowError, OSError, ValueError):
        return None


def epoch_milliseconds(value: Any) -> int | None:
    """Validate a protocol field that is explicitly expressed in milliseconds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(number) or number < 0 or number > MAX_SAFE_INTEGER:
        return None
    try:
        return round(number)
    except (OverflowError, ValueError):
        return None


def duration_milliseconds(value: Any) -> int | None:
    """Normalize durations; zero marks unconfirmed sub-millisecond timing internally."""
    if isinstance(value, dict):
        seconds = value.get("secs")
        nanos = value.get("nanos", 0)
        if (
            isinstance(seconds, bool)
            or isinstance(nanos, bool)
            or not isinstance(seconds, int)
            or not isinstance(nanos, int)
        ):
            return None
        try:
            seconds_number = float(seconds)
            nanos_number = float(nanos)
        except (OverflowError, ValueError):
            return None
        if (
            not math.isfinite(seconds_number)
            or not math.isfinite(nanos_number)
            or seconds_number < 0
            or not 0 <= nanos_number < 1_000_000_000
        ):
            return None
        milliseconds = seconds_number * 1000 + nanos_number / 1_000_000
        if not math.isfinite(milliseconds) or milliseconds > MAX_SAFE_INTEGER:
            return None
        # Keep a zero marker so an explicit unconfirmed duration cannot fall back
        # to unrelated timestamps. Tool records expose this marker as null.
        return round(milliseconds) if milliseconds >= 1 else 0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    milliseconds = number * 1000
    if not math.isfinite(milliseconds) or milliseconds > MAX_SAFE_INTEGER:
        return None
    return round(milliseconds) if milliseconds >= 1 else 0


def elapsed_milliseconds(started_at: int | None, completed_at: int | None) -> int | None:
    """Return a positive, JSON-safe interval only when its boundaries distinguish it."""
    if started_at is None or completed_at is None:
        return None
    elapsed = completed_at - started_at
    return elapsed if 1 <= elapsed <= MAX_SAFE_INTEGER else None


def attachment_count(value: Any) -> int:
    """Count attachment arrays without calling ``len`` on malformed scalars."""
    return len(value) if isinstance(value, list) else 0


def add_warning(warnings: list[dict[str, Any]], code: str, line: int, message: str) -> None:
    """Bound diagnostics so hostile logs cannot grow projection memory indefinitely."""
    if len(warnings) < MAX_WARNINGS:
        warnings.append(
            {
                "code": safe_text(code, 100) or "warning",
                "line": max(1, line),
                "message": safe_text(message, 500) or "Projection warning.",
            }
        )


def protocol_identifier(value: Any, fallback: str | None = None) -> str | None:
    """Return a bounded protocol ID while retaining uniqueness for oversized values."""
    if not isinstance(value, str) or not value:
        return fallback
    if not value.strip():
        return fallback
    if len(value) <= 240:
        return value
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]
    return f"{value[:220]}…{digest}"


def metadata_identity(metadata: dict[str, Any], path: Path) -> str:
    """Prefer the concrete thread ID and retain legacy session-ID compatibility."""
    for key in ("id", "session_id"):
        value = protocol_identifier(metadata.get(key))
        if value:
            return value
    return protocol_identifier(path.stem, "unknown-session") or "unknown-session"


def item_type(value: Any) -> str:
    """Normalize snake_case and PascalCase protocol discriminators."""
    if not isinstance(value, str) or len(value) > 200:
        return ""
    normalized: list[str] = []
    for index, character in enumerate(value):
        if character.isupper() and index and value[index - 1].islower():
            normalized.append("_")
        normalized.append(character.casefold())
    return "".join(normalized).replace("-", "_")


def session_search_fields(path: Path) -> dict[str, Any]:
    """Build searchable metadata while decoding only metadata-bearing JSONL records."""
    metadata = first_session_metadata(path)
    paginated = str(metadata.get("history_mode") or "legacy").casefold() == "paginated"
    first_user = ""
    model: str | None = None
    for _, entry in iter_session_search_jsonl(path):
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        entry_type = entry.get("type")
        payload_type = payload.get("type")
        if entry_type == "turn_context":
            model = safe_text(payload.get("model"), 200) or model
        elif (
            not paginated
            and entry_type == "event_msg"
            and payload_type == "user_message"
            and not first_user
        ):
            message = payload.get("message")
            first_user = message if isinstance(message, str) else ""
        elif entry_type == "event_msg" and payload_type == "item_completed":
            item = payload.get("item")
            if (
                isinstance(item, dict)
                and item_type(item.get("type")) == "user_message"
                and not first_user
            ):
                first_user = content_text(item.get("content"))
    return {
        "id": metadata_identity(metadata, path),
        "title": shorten(first_user or "Untitled Codex task", 100),
        "cwd": display_path(metadata.get("cwd")),
        "model": model,
    }


def indexed_session_search_fields(
    index: SessionSearchIndex,
    path: Path,
) -> dict[str, Any]:
    """Return signature-validated fields, refreshing the lightweight index on misses."""
    signature = session_signature(path)
    fields = index.lookup(path, signature)
    if fields is None:
        fields = session_search_fields(path)
        index.store(path, signature, fields)
    return fields


def search_haystack(value: dict[str, Any]) -> str:
    """Normalize the public session-search fields for substring matching."""
    return " ".join(str(value.get(key) or "") for key in ("id", "title", "cwd", "model"))


def session_overview(path: Path) -> dict[str, Any]:
    """Build a compact session summary without returning transcript bodies."""
    signature = session_signature(path)
    with _SESSION_OVERVIEW_CACHE_LOCK:
        cached = _SESSION_OVERVIEW_CACHE.get(path)
        if cached is not None and cached[0] == signature:
            _SESSION_OVERVIEW_CACHE.move_to_end(path)
            return deepcopy(cached[1])

    metadata = first_session_metadata(path)
    paginated = str(metadata.get("history_mode") or "legacy").casefold() == "paginated"
    first_user = ""
    model: str | None = None
    effort: str | None = None
    collaboration: str | None = None
    first_time = parse_timestamp(metadata.get("timestamp"))
    last_time: int | None = None
    turns = 0
    tool_calls = 0
    seen_tool_calls: OrderedDict[str, None] = OrderedDict()
    overview_active_turn = False
    overview_turn_id: str | None = None
    latest_usage: dict[str, int | float] | None = None
    accounted_usage: dict[str, int | float] | None = None
    previous_total_usage: dict[str, int | float] = {}

    def count_tool_call(raw_id: Any) -> None:
        """Count a logical call once when legacy start and terminal records coexist."""
        nonlocal tool_calls
        call_id = protocol_identifier(raw_id)
        if call_id is not None:
            if call_id in seen_tool_calls:
                seen_tool_calls.move_to_end(call_id)
                return
            seen_tool_calls[call_id] = None
            while len(seen_tool_calls) > MAX_TRACKED_CALLS:
                seen_tool_calls.popitem(last=False)
        tool_calls += 1

    def ensure_overview_turn(raw_turn_id: Any = None) -> None:
        """Mirror the detailed projection's implicit and explicit turn creation."""
        nonlocal overview_active_turn, overview_turn_id, turns
        turn_id = protocol_identifier(raw_turn_id)
        if not overview_active_turn:
            turns += 1
            overview_active_turn = True
            overview_turn_id = turn_id
        elif turn_id and overview_turn_id and turn_id != overview_turn_id:
            turns += 1
            overview_turn_id = turn_id
        elif turn_id and overview_turn_id is None:
            overview_turn_id = turn_id

    def entry_creates_record(entry_type: Any, payload_type: Any, payload: dict[str, Any]) -> bool:
        if entry_type in {"compacted", "inter_agent_communication"}:
            return True
        if entry_type == "event_msg":
            if payload_type == "item_completed":
                return isinstance(payload.get("item"), dict)
            return not paginated and payload_type in {
                "user_message",
                "mcp_tool_call_end",
                "patch_apply_end",
                "web_search_end",
                "image_generation_end",
                "sub_agent_activity",
                "context_compacted",
                "entered_review_mode",
                "exited_review_mode",
            }
        if entry_type == "response_item" and payload_type == "agent_message":
            return True
        if paginated or entry_type != "response_item":
            return False
        if payload_type == "message":
            return payload.get("role") == "assistant"
        return payload_type in {
            "reasoning",
            "function_call",
            "custom_tool_call",
            "local_shell_call",
            "tool_search_call",
            "tool_search_output",
            "web_search_call",
            "image_generation_call",
            "compaction",
            "compaction_summary",
            "context_compaction",
            "function_call_output",
            "custom_tool_call_output",
            "agent_message",
        }

    for _, entry in iter_session_jsonl(path):
        timestamp = parse_timestamp(entry.get("timestamp"))
        if timestamp is not None:
            first_time = timestamp if first_time is None else min(first_time, timestamp)
            last_time = timestamp if last_time is None else max(last_time, timestamp)
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        entry_type = entry.get("type")
        payload_type = payload.get("type")
        if entry_type == "event_msg" and payload_type in {"task_started", "turn_started"}:
            ensure_overview_turn(payload.get("turn_id"))
            collaboration = safe_text(payload.get("collaboration_mode_kind"), 80) or collaboration
        elif entry_type == "event_msg" and payload_type in {
            "task_complete",
            "turn_complete",
            "turn_aborted",
        }:
            ensure_overview_turn(payload.get("turn_id"))
            overview_active_turn = False
            overview_turn_id = None
        elif entry_creates_record(entry_type, payload_type, payload):
            ensure_overview_turn(payload.get("turn_id"))
        if entry_type == "turn_context":
            model = safe_text(payload.get("model"), 200) or model
            effort = safe_text(payload.get("effort"), 80) or effort
            mode = payload.get("collaboration_mode")
            if isinstance(mode, dict):
                collaboration = safe_text(mode.get("mode"), 80) or collaboration
            else:
                collaboration = (
                    safe_text(payload.get("collaboration_mode_kind"), 80) or collaboration
                )
        elif (
            not paginated
            and entry_type == "event_msg"
            and payload_type == "user_message"
            and not first_user
        ):
            message = payload.get("message")
            first_user = message if isinstance(message, str) else ""
        elif entry_type == "event_msg" and payload_type == "item_completed":
            item = payload.get("item")
            if not isinstance(item, dict):
                continue
            completed_type = item_type(item.get("type"))
            if completed_type == "user_message" and not first_user:
                first_user = content_text(item.get("content"))
            if completed_type in {
                "command_execution",
                "dynamic_tool_call",
                "collab_agent_tool_call",
                "web_search",
                "image_view",
                "extension",
                "image_generation",
                "file_change",
                "mcp_tool_call",
            }:
                count_tool_call(item.get("id"))
        elif entry_type == "event_msg" and payload_type == "token_count":
            info = payload.get("info")
            if isinstance(info, dict):
                (
                    previous_total_usage,
                    total_usage,
                    last_usage,
                    usage_changed,
                ) = token_usage_snapshot(info, previous_total_usage)
                if total_usage is not None:
                    latest_usage = total_usage
                if usage_changed and last_usage:
                    accounted_usage = merge_token_usage(accounted_usage, last_usage)
        elif not paginated and (
            (
                entry_type == "response_item"
                and payload_type
                in {
                    "function_call",
                    "custom_tool_call",
                    "local_shell_call",
                    "tool_search_call",
                    "tool_search_output",
                    "web_search_call",
                    "image_generation_call",
                    "function_call_output",
                    "custom_tool_call_output",
                }
            )
            or (
                entry_type == "event_msg"
                and payload_type
                in {
                    "mcp_tool_call_end",
                    "patch_apply_end",
                    "web_search_end",
                    "image_generation_end",
                }
            )
        ):
            count_tool_call(payload.get("call_id") or payload.get("id"))
    # Reuse the descriptor-derived signature instead of reopening the path by name.
    updated_ms = (signature[-1][1] + 500_000) // 1_000_000
    overview = {
        "id": metadata_identity(metadata, path),
        "title": shorten(first_user or "Untitled Codex task", 100),
        "cwd": display_path(metadata.get("cwd")),
        "model": model,
        "effort": effort,
        "collaborationMode": collaboration,
        "startedAt": iso_timestamp(first_time),
        "updatedAt": iso_timestamp(last_time if last_time is not None else updated_ms),
        "turns": turns,
        "toolCalls": tool_calls,
        "tokens": accounted_usage if accounted_usage is not None else latest_usage,
        "archived": is_archived_session(path),
        "parentThreadId": safe_text(metadata.get("parent_thread_id"), 100),
        "agentPath": safe_text(metadata.get("agent_path"), 200),
        "git": safe_git(metadata.get("git")),
    }
    # The source can append while an overview is being projected. Do not cache
    # a mixed snapshot under a signature that no longer describes the lineage.
    if session_signature(path) == signature:
        with _SESSION_OVERVIEW_CACHE_LOCK:
            _SESSION_OVERVIEW_CACHE[path] = (signature, overview)
            _SESSION_OVERVIEW_CACHE.move_to_end(path)
            while len(_SESSION_OVERVIEW_CACHE) > MAX_OVERVIEW_CACHE:
                _SESSION_OVERVIEW_CACHE.popitem(last=False)
    return deepcopy(overview)


@session_scan()
def list_session_overviews(
    limit: int = 20, query: str = "", include_archived: bool = False
) -> list[dict[str, Any]]:
    """List recent session summaries with an optional case-insensitive filter."""
    if len(query) > 500:
        raise ValueError("query must contain at most 500 characters.")
    needle = query.casefold().strip()
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    index = SessionSearchIndex()
    try:
        for path in session_files(include_archived):
            if needle:
                try:
                    fields = indexed_session_search_fields(index, path)
                except (OSError, RuntimeError, ValueError):
                    continue
                overview_id = str(fields["id"])
                if overview_id in seen_ids:
                    continue
                seen_ids.add(overview_id)
                if needle not in search_haystack(fields).casefold():
                    continue
            try:
                overview = session_overview(path)
            except (OSError, RuntimeError, ValueError):
                continue
            overview_id = str(overview["id"])
            if not needle:
                if overview_id in seen_ids:
                    continue
                seen_ids.add(overview_id)
                try:
                    signature = session_signature(path)
                    index.store(
                        path,
                        signature,
                        {key: overview.get(key) for key in ("id", "title", "cwd", "model")},
                    )
                except (OSError, RuntimeError, ValueError):
                    pass
            result.append(overview)
            if len(result) >= limit:
                break
    finally:
        index.flush()
    return result


@session_scan()
def resolve_session(session_id: str | None, include_archived: bool) -> Path:
    """Resolve a session identifier without accepting arbitrary filesystem paths."""
    paths = session_files(include_archived)
    if not paths:
        raise ValueError("No local Codex session logs were found.")
    if session_id is None:
        return paths[0]
    requested = session_id.strip()
    if not requested:
        raise ValueError("sessionId must not be empty.")
    if requested == "latest":
        return paths[0]
    if len(requested) > 240:
        raise ValueError("sessionId must contain at most 240 characters.")
    if "/" in requested or "\\" in requested:
        raise ValueError("sessionId must be an identifier, not a filesystem path.")
    prefix: dict[str, Path] = {}
    aliases: set[Path] = set()
    size_error: ReadLimitExceeded | None = None
    with session_read_limit():
        default_read_limit = jsonl_byte_limit()
    for path in paths:
        try:
            filename_ids = (thread_id_from_path(path), rollout_id_from_path(path), path.stem)
            selected_file = any(
                value is not None and value.casefold().startswith(requested.casefold())
                for value in filename_ids
            )
            # A larger task budget must not expand probes of unrelated compressed logs.
            if jsonl_byte_limit() > default_read_limit and not selected_file:
                with session_read_limit():
                    candidate = metadata_identity(first_session_metadata(path), path)
            else:
                candidate = metadata_identity(first_session_metadata(path), path)
        except ReadLimitExceeded as error:
            if requested.casefold() in {thread_id_from_path(path), rollout_id_from_path(path)}:
                error.session_id = thread_id_from_path(path)
                size_error = size_error or error
            continue
        except (OSError, RuntimeError, ValueError):
            continue
        if candidate == requested:
            return path
        if candidate.startswith(requested):
            prefix.setdefault(f"thread:{candidate}", path)
        physical_id = rollout_id_from_path(path)
        if path.stem == requested or physical_id == requested.casefold():
            aliases.add(path)
        if physical_id is not None and physical_id.startswith(requested.casefold()):
            prefix.setdefault(f"rollout:{physical_id}", path)
        if path.stem.startswith(requested):
            prefix.setdefault(f"file:{path.stem}", path)
    if len(aliases) == 1:
        return next(iter(aliases))
    if len(aliases) > 1:
        raise ValueError(f"Codex session alias {requested!r} is ambiguous.")
    if not prefix:
        if size_error is not None:
            raise size_error
        raise ValueError(f"Codex session {requested!r} was not found.")
    matched_paths = set(prefix.values())
    if len(matched_paths) > 1:
        raise ValueError(f"Codex session prefix {requested!r} is ambiguous.")
    return next(iter(matched_paths))


def output_is_error(value: Any) -> bool:
    """Read common MCP/tool failure markers without interpreting body text."""
    parsed = value
    if isinstance(value, str):
        try:
            parsed = strict_json_loads(value)
        except (RecursionError, ValueError):
            return False
    for _ in range(8):
        if not isinstance(parsed, dict):
            return False
        if parsed.get("isError") is True or parsed.get("success") is False or "Err" in parsed:
            return True
        if "Ok" not in parsed:
            return False
        parsed = parsed["Ok"]
    return False


def merge_token_usage(current: Any, update: dict[str, Any]) -> dict[str, Any]:
    """Add flat numeric token counters while ignoring unknown structured details."""
    result = numeric_token_usage(current)
    for key, value in numeric_token_usage(update).items():
        existing = result.get(key)
        if not isinstance(existing, (int, float)):
            existing = 0
        combined = existing + value
        if combined <= MAX_SAFE_INTEGER:
            result[key] = combined
    return result


def numeric_token_usage(value: Any) -> dict[str, int | float]:
    """Return only the flat numeric counters used to compare cumulative snapshots."""
    if not isinstance(value, dict):
        return {}
    allowed = {
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    }
    return {
        key: counter
        for key, counter in value.items()
        if key in allowed
        and not isinstance(counter, bool)
        and isinstance(counter, int)
        and 0 <= counter <= MAX_SAFE_INTEGER
        and _finite_number(counter)
    }


def token_usage_snapshot(
    info: dict[str, Any], previous_total_usage: dict[str, int | float]
) -> tuple[
    dict[str, int | float],
    dict[str, int | float] | None,
    dict[str, int | float],
    bool,
]:
    """Normalize one token event and report whether it represents a new model call."""
    usage_changed = "total_token_usage" not in info
    current_total_usage: dict[str, int | float] | None = None
    last_usage = numeric_token_usage(info.get("last_token_usage"))
    usage = info.get("total_token_usage")
    if isinstance(usage, dict):
        usage_update = numeric_token_usage(usage)
        if usage_update:
            reset = any(
                value < previous_total_usage.get(key, 0) for key, value in usage_update.items()
            )
            current_total_usage = {**({} if reset else previous_total_usage), **usage_update}
            counter_names = current_total_usage.keys() | previous_total_usage.keys()
            usage_changed = any(
                current_total_usage.get(name, 0) != previous_total_usage.get(name, 0)
                for name in counter_names
            )
            # Cumulative deltas are authoritative when present. A missing or
            # inconsistent last-call sample must not silently lose token usage.
            # A decreasing counter starts a new cumulative epoch (e.g. resume).
            last_usage = {
                **{key: value for key, value in last_usage.items() if key not in usage_update},
                **{
                    key: value if reset else value - previous_total_usage.get(key, 0)
                    for key, value in usage_update.items()
                },
            }
            previous_total_usage = current_total_usage
    return (
        previous_total_usage,
        current_total_usage,
        last_usage,
        usage_changed,
    )


def safe_rate_limits(value: Any) -> dict[str, dict[str, Any]] | None:
    """Project the bounded Codex rate-limit windows used by the live viewer."""
    if not isinstance(value, dict):
        return None
    if value.get("limit_id") not in (None, "codex"):
        return None
    result: dict[str, dict[str, Any]] = {}
    for name in ("primary", "secondary"):
        window = value.get(name)
        if not isinstance(window, dict):
            continue
        used_percent = window.get("used_percent")
        window_minutes = window.get("window_minutes")
        if (
            isinstance(used_percent, bool)
            or not isinstance(used_percent, (int, float))
            or not _finite_number(used_percent)
            or not 0 <= used_percent <= 100
            or isinstance(window_minutes, bool)
            or not isinstance(window_minutes, int)
            or not 0 < window_minutes <= MAX_SAFE_INTEGER
        ):
            continue
        result[name] = {
            "usedPercent": used_percent,
            "windowMinutes": window_minutes,
            "resetsAt": iso_timestamp(parse_timestamp(window.get("resets_at"))),
        }
    return result or None


def merge_rate_limits(
    previous: dict[str, dict[str, Any]] | None, raw: Any
) -> dict[str, dict[str, Any]] | None:
    """Keep partial valid updates, but remove windows explicitly retired by Codex."""
    if not isinstance(raw, dict) or raw.get("limit_id") not in (None, "codex"):
        return previous
    result = {**(previous or {}), **(safe_rate_limits(raw) or {})}
    for key in ("primary", "secondary"):
        if key in raw and raw[key] is None:
            result.pop(key, None)
    return result or None


def latest_account_quota() -> dict[str, Any]:
    """Find the newest Codex sample independently of the selected task's activity."""
    newest: dict[str, Any] = {"rateLimits": None, "sampledAt": None}
    newest_time = -1
    try:
        paths = session_files(False)[:MAX_QUOTA_FILES]
    except (OSError, ValueError):
        return newest
    now = round(datetime.now(timezone.utc).timestamp() * 1000)
    with _QUOTA_CACHE_LOCK:
        for path in paths:
            cached = _QUOTA_CACHE.get(path)
            try:
                signature, entries = recent_rate_limit_entries(path, cached[0] if cached else None)
            except (OSError, ValueError):
                _QUOTA_CACHE.pop(path, None)
                continue
            sample = None
            if cached is not None:
                previous_signature, previous_sample = cached
                # Preserve samples that have rolled out of the bounded tail when
                # the same file grows; replacements and non-growing edits reset it.
                if signature == previous_signature or (
                    len(signature) == len(previous_signature) == 5
                    and signature[:2] == previous_signature[:2]
                    and signature[2] > previous_signature[2]
                ):
                    sample = previous_sample
            if entries is not None:
                cached_time = parse_timestamp(sample["sampledAt"]) if sample else None
                sample_time = cached_time if cached_time is not None else -1
                for entry in entries:
                    payload = entry.get("payload")
                    if entry.get("type") != "event_msg" or not isinstance(payload, dict):
                        continue
                    raw = payload.get("rate_limits")
                    timestamp = parse_timestamp(entry.get("timestamp"))
                    if (
                        payload.get("type") != "token_count"
                        or not isinstance(raw, dict)
                        or raw.get("limit_id") not in (None, "codex")
                        or timestamp is None
                        or timestamp < sample_time
                        or timestamp > now + 300_000
                        or not (
                            safe_rate_limits(raw)
                            or any(
                                key in raw and raw[key] is None for key in ("primary", "secondary")
                            )
                        )
                    ):
                        continue
                    sample_time = timestamp
                    sample = {
                        "rateLimits": merge_rate_limits(
                            sample["rateLimits"] if sample else None, raw
                        ),
                        "sampledAt": iso_timestamp(timestamp),
                    }
                _QUOTA_CACHE[path] = (signature, sample)
            _QUOTA_CACHE.move_to_end(path)
            timestamp = parse_timestamp(sample["sampledAt"]) if sample else None
            if sample is not None and timestamp is not None and timestamp > newest_time:
                newest_time = timestamp
                newest = sample
        while len(_QUOTA_CACHE) > MAX_QUOTA_FILES:
            _QUOTA_CACHE.popitem(last=False)
    return deepcopy(newest)


def _finite_number(value: int | float) -> bool:
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def parse_session(
    path: Path,
    max_records: int = DEFAULT_MAX_RECORDS,
    detail_level: DetailLevel = "summary",
    before_record: int | None = None,
    *,
    _index: PageIndex | None = None,
    _entries: Iterator[tuple[int, int, dict[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Project one Codex rollout log into a turn-aware trajectory page."""
    detail_level = normalize_detail_level(detail_level)
    include_details = detail_level == "full"

    def detail_json_text(value: Any) -> str | None:
        # Summary reads never format inputs or outputs that will be discarded.
        return json_text(value) if include_details else None

    limited = max(MIN_RECORDS, min(int(max_records), MAX_RECORDS))
    records: deque[dict[str, Any]] = deque(maxlen=limited)
    visible_record_ids: set[str] = set()
    tracked_calls: OrderedDict[str, dict[str, Any]] = OrderedDict()
    call_states: OrderedDict[str, dict[str, bool]] = OrderedDict()
    turns: deque[dict[str, Any]] = deque(maxlen=MAX_TURNS)
    retained_turns: dict[int, dict[str, Any]] = {}
    retained_turn_refcounts: dict[int, int] = {}
    metadata = first_session_metadata(path)
    paginated = str(metadata.get("history_mode") or "legacy").casefold() == "paginated"
    context: dict[str, Any] = {}
    latest_usage: dict[str, int | float] | None = None
    accounted_usage: dict[str, int | float] | None = None
    session_cost: dict[str, Any] | None = None
    fast_cost_calls = 0
    last_model_service_tier: str | None = None
    latest_rate_limits: dict[str, dict[str, Any]] | None = None
    previous_total_usage: dict[str, int | float] = {}
    context_window: int | None = None
    current_turn = 0
    current_step = 0
    active_turn = False
    after_tool_result = False
    last_model_record: dict[str, Any] | None = None
    last_model_turn: dict[str, Any] | None = None
    first_user = ""
    first_time = parse_timestamp(metadata.get("timestamp"))
    last_time: int | None = None
    all_record_count = 0
    tool_calls = 0
    failed_tools = 0
    compactions = 0
    pending_compaction: dict[str, Any] | None = None

    def ensure_turn(timestamp: int | None, turn_id: str | None = None) -> dict[str, Any]:
        nonlocal current_turn, current_step, active_turn, after_tool_result
        turn_id = protocol_identifier(turn_id)
        if not active_turn:
            turn_model = context.get("model")
            current_turn += 1
            current_step = 0
            after_tool_result = False
            active_turn = True
            if _index is not None and len(turns) == MAX_TURNS:
                _index.save_turn(turns[0])
            turns.append(
                {
                    "index": current_turn,
                    "id": turn_id,
                    "startedAt": iso_timestamp(timestamp),
                    "completedAt": None,
                    "durationMs": None,
                    "timeToFirstTokenMs": None,
                    "status": "running",
                    "error": None,
                    "records": 0,
                    "steps": 0,
                    "modelCalls": 0,
                    "usage": None,
                    "cost": None,
                    "model": turn_model if isinstance(turn_model, str) else None,
                    "_serviceTier": context.get("service_tier"),
                }
            )
        turn = turns[-1]
        if turn_id and not turn.get("id"):
            turn["id"] = turn_id
        if turn.get("startedAt") is None and timestamp is not None:
            turn["startedAt"] = iso_timestamp(timestamp)
        return turn

    def model_step(timestamp: int | None) -> tuple[int, dict[str, Any]]:
        nonlocal current_step, after_tool_result, last_model_service_tier
        turn = ensure_turn(timestamp)
        last_model_service_tier = turn.get("_serviceTier")
        if current_step == 0 or after_tool_result:
            current_step += 1
            after_tool_result = False
        turn["steps"] = max(turn["steps"], current_step)
        return current_step, turn

    def add_record(
        *,
        timestamp: int | None,
        kind: str,
        event: Any,
        summary: Any,
        step: int | None = None,
        record_id: Any = None,
        input_detail: str | None = None,
        output_detail: str | None = None,
        status: str = "complete",
        call_id: str | None = None,
        metadata_detail: dict[str, Any] | None = None,
        count_tool: bool = True,
        has_start_boundary: bool = True,
        inherited_call: bool = False,
    ) -> dict[str, Any]:
        nonlocal all_record_count, tool_calls, compactions
        turn = ensure_turn(timestamp)
        all_record_count += 1
        record_id = protocol_identifier(record_id, f"record-{all_record_count}") or (
            f"record-{all_record_count}"
        )
        event_text = safe_text(event, 260) or "Event"
        summary_text = safe_text(summary) or event_text
        call_id = protocol_identifier(call_id)
        if _index is not None:
            _index.track_identifiers(record_id, None if inherited_call else call_id)
        if record_id in visible_record_ids:
            salt = 0
            while record_id in visible_record_ids:
                digest = hashlib.sha256(
                    f"{record_id}\0{all_record_count}\0{salt}".encode()
                ).hexdigest()[:16]
                record_id = f"{record_id[:220]}…{digest}"
                salt += 1
        counts_as_tool = kind == "tool" and count_tool
        state: dict[str, bool] | None = None
        if kind == "tool" and call_id:
            state = call_states.get(call_id)
            if state is None:
                state = {"countsAsTool": counts_as_tool, "failedCounted": False}
                call_states[call_id] = state
            elif counts_as_tool and state["countsAsTool"]:
                counts_as_tool = False
            elif counts_as_tool:
                state["countsAsTool"] = True
            call_states.move_to_end(call_id)
            while len(call_states) > MAX_TRACKED_CALLS:
                call_states.popitem(last=False)
        record = {
            "index": all_record_count,
            "id": record_id,
            "turn": current_turn,
            "step": step,
            "kind": kind,
            "event": event_text,
            "summary": summary_text,
            "startedAt": iso_timestamp(timestamp),
            "completedAt": iso_timestamp(timestamp) if status != "running" else None,
            # A single persisted event timestamp places the record on the timeline but
            # does not establish an elapsed interval. Correlated tool boundaries and
            # canonical item timing fill this value when the log actually measured it.
            "durationMs": None,
            "status": status,
            "callId": call_id,
            "input": bounded(input_detail) if include_details and input_detail else None,
            "output": bounded(output_detail) if include_details and output_detail else None,
            "error": None,
            "usage": None,
            "cost": None,
            "metadata": (metadata_detail or {}) if include_details else {},
            "_countsAsTool": counts_as_tool,
            "_failedCounted": state["failedCounted"] if state is not None else False,
            "_durationAuthoritative": False,
            "_outputAuthoritative": False,
            "_hasStartBoundary": has_start_boundary and timestamp is not None,
        }
        if _index is not None:
            _index.track(record)
        if kind == "tool" and call_id:
            tracked_calls[call_id] = record
            tracked_calls.move_to_end(call_id)
            while len(tracked_calls) > MAX_TRACKED_CALLS:
                tracked_calls.popitem(last=False)
        if counts_as_tool:
            tool_calls += 1
        elif kind == "compaction":
            compactions += 1
        turn["records"] += 1

        # ``before_record`` is an exclusive cursor over stable projected indexes.
        # Keep parsing the complete source so aggregate stats and terminal tool
        # updates remain correct, but only retain records that belong to this page.
        if before_record is not None and all_record_count >= before_record:
            return record
        if len(records) == limited:
            evicted = records[0]
            visible_record_ids.discard(str(evicted.get("id") or ""))
            evicted_turn = evicted.get("turn")
            if isinstance(evicted_turn, int):
                remaining_refs = retained_turn_refcounts.get(evicted_turn, 0) - 1
                if remaining_refs > 0:
                    retained_turn_refcounts[evicted_turn] = remaining_refs
                else:
                    retained_turn_refcounts.pop(evicted_turn, None)
                    retained_turns.pop(evicted_turn, None)
        records.append(record)
        retained_turns[current_turn] = turn
        retained_turn_refcounts[current_turn] = retained_turn_refcounts.get(current_turn, 0) + 1
        visible_record_ids.add(record_id)
        return record

    def late_tool_marker(record: dict[str, Any], timestamp: int | None) -> dict[str, Any] | None:
        """Materialize a terminal marker when call and result cannot share the smallest page."""
        record_index = record.get("index")
        if not isinstance(record_index, int) or all_record_count - record_index < MIN_RECORDS:
            return None
        raw_call_id = record.get("callId")
        call_id = raw_call_id if isinstance(raw_call_id, str) else None
        marker_id = f"tool-result-{call_id or 'unknown'}-{all_record_count + 1}"
        marker = add_record(
            timestamp=timestamp,
            kind="tool",
            event=str(record.get("event") or "Tool result"),
            summary=f"{record.get('event') or 'Tool result'} · running",
            step=record.get("step") if isinstance(record.get("step"), int) else None,
            record_id=marker_id,
            input_detail=record.get("input") if isinstance(record.get("input"), str) else None,
            status="running",
            call_id=call_id,
            metadata_detail=(
                record.get("metadata") if isinstance(record.get("metadata"), dict) else None
            ),
            count_tool=False,
            inherited_call=True,
        )
        if _index is not None:
            _index.track(marker, inherit=int(record["index"]))
        # The marker is ordered at completion but retains the measured call
        # boundary. Otherwise finishing it fabricates a zero-length duration.
        marker["startedAt"] = record.get("startedAt")
        for key in (
            "durationMs",
            "completedAt",
            "_durationAuthoritative",
            "_outputAuthoritative",
            "_hasStartBoundary",
            "_commandActivity",
            "_commandExitCode",
            "output",
        ):
            marker[key] = record.get(key)
        return marker

    def tool_summary(record: dict[str, Any], status: str) -> str:
        activity = record.get("_commandActivity")
        prefix = f"Command: {activity}" if activity else record["event"]
        exit_code = record.get("_commandExitCode")
        suffix = f" · exit {exit_code}" if exit_code is not None else ""
        return shorten(f"{prefix} · {status}{suffix}")

    def mark_tool_error(record: dict[str, Any], message: str) -> None:
        """Mark one retained tool record and its aggregate state as failed."""
        nonlocal failed_tools
        if _index is not None:
            _index.track(record)
        record["status"] = "error"
        record["error"] = record.get("error") or message
        record["summary"] = tool_summary(record, "error")
        raw_call_id = record.get("callId")
        state = call_states.get(raw_call_id) if isinstance(raw_call_id, str) else None
        counts_as_tool = (
            state["countsAsTool"] if state is not None else bool(record.get("_countsAsTool"))
        )
        failed_counted = (
            state["failedCounted"] if state is not None else bool(record.get("_failedCounted"))
        )
        if counts_as_tool and not failed_counted:
            failed_tools += 1
            record["_failedCounted"] = True
            if state is not None and isinstance(raw_call_id, str):
                state["failedCounted"] = True
                call_states.move_to_end(raw_call_id)

    def finish_tool(
        record: dict[str, Any],
        *,
        timestamp: int | None,
        output: Any = None,
        failed: bool = False,
        error_message: str = "Tool result reported an error.",
        duration_ms: int | None = None,
        authoritative: bool = False,
    ) -> None:
        """Apply a terminal tool event without losing an earlier authoritative result."""
        if _index is not None:
            _index.track(record)
        completed_at = timestamp
        started_at = (
            parse_timestamp(record.get("startedAt")) if record.get("_hasStartBoundary") else None
        )
        if (authoritative or not record.get("_durationAuthoritative")) and (
            completed_at is not None or record.get("completedAt") is None
        ):
            record["completedAt"] = iso_timestamp(completed_at)
        if duration_ms is not None and (authoritative or not record.get("_durationAuthoritative")):
            record["durationMs"] = duration_ms if duration_ms >= 1 else None
            if authoritative:
                record["_durationAuthoritative"] = True
        elif not record.get("_durationAuthoritative"):
            elapsed = elapsed_milliseconds(started_at, completed_at)
            if elapsed is not None or record.get("durationMs") is None:
                record["durationMs"] = elapsed
            if authoritative:
                record["_durationAuthoritative"] = True
        if include_details and output is not None and not record.get("_outputAuthoritative"):
            record["output"] = detail_json_text(output)
            if authoritative:
                record["_outputAuthoritative"] = True
        if failed or record.get("status") == "error":
            mark_tool_error(record, error_message)
        else:
            record["status"] = "complete"
            record["summary"] = tool_summary(record, "complete")

    def terminal_tool(
        *,
        event: str,
        timestamp: int | None,
        started_at: int | None = None,
        record_id: str,
        call_id: str | None,
        input_value: Any = None,
        output_value: Any = None,
        failed: bool = False,
        duration_ms: int | None = None,
        metadata_detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or finish a tool record from an authoritative terminal event."""
        nonlocal after_tool_result, last_model_record, last_model_turn
        call_id = protocol_identifier(call_id)
        record = tracked_calls.get(call_id) if call_id else None
        has_duration = duration_ms is not None and duration_ms >= 1
        duration_unavailable = record is None and started_at is None and not has_duration
        if record is None:
            record_time = started_at
            if (
                record_time is None
                and timestamp is not None
                and duration_ms is not None
                and has_duration
            ):
                record_time = max(0, timestamp - duration_ms)
            if record_time is None:
                record_time = timestamp
            if current_step == 0:
                step, _ = model_step(record_time)
            else:
                turn = ensure_turn(record_time)
                step = current_step
                turn["steps"] = max(turn["steps"], step)
            record = add_record(
                timestamp=record_time,
                kind="tool",
                event=event,
                summary=f"{event} · running",
                step=step,
                record_id=record_id,
                input_detail=detail_json_text(input_value) if input_value is not None else None,
                status="running",
                call_id=call_id,
                metadata_detail=metadata_detail,
                count_tool=(
                    call_id is None
                    or call_id not in call_states
                    or not call_states[call_id]["countsAsTool"]
                ),
                has_start_boundary=started_at is not None or has_duration,
            )
        else:
            if started_at is not None and not record.get("_hasStartBoundary"):
                record["startedAt"] = iso_timestamp(started_at)
                record["_hasStartBoundary"] = True
            authoritative_event = safe_text(event, 260)
            existing_event = str(record.get("event") or "")
            generic_events = {"tool", "mcp tool"}
            if authoritative_event and (
                authoritative_event.casefold() not in generic_events
                or existing_event.casefold() in generic_events
            ):
                record["event"] = authoritative_event
        if record is not None and include_details:
            if record.get("input") is None and input_value is not None:
                record["input"] = detail_json_text(input_value)
            if metadata_detail:
                record["metadata"].update(metadata_detail)
        if event == "Command":
            command = input_value.get("command") if isinstance(input_value, dict) else None
            if command is not None or "_commandActivity" not in record:
                record["_commandActivity"] = command_activity(command)
            exit_code = output_value.get("exit_code") if isinstance(output_value, dict) else None
            if (
                isinstance(exit_code, int)
                and not isinstance(exit_code, bool)
                and -(2**31) <= exit_code < 2**31
            ):
                record["_commandExitCode"] = exit_code
        marker = late_tool_marker(record, timestamp)
        if marker is not None:
            finish_tool(
                record,
                timestamp=timestamp,
                output=output_value,
                failed=failed,
                duration_ms=duration_ms,
                authoritative=True,
                error_message="Tool completion event reported an error.",
            )
            record = marker
        finish_tool(
            record,
            timestamp=timestamp,
            output=output_value,
            failed=failed,
            duration_ms=duration_ms,
            authoritative=True,
            error_message="Tool completion event reported an error.",
        )
        if duration_unavailable:
            # A terminal-only tool item has no call boundary from which to infer
            # elapsed time. Its completion timestamp is still useful for ordering.
            record["durationMs"] = None
        if last_model_record is None or last_model_record.get("turn") != current_turn:
            last_model_record = record
            owner_index = record.get("turn")
            last_model_turn = next(
                (turn for turn in reversed(turns) if turn.get("index") == owner_index),
                None,
            )
        after_tool_result = True
        return record

    def finish_record_timing(
        record: dict[str, Any], started_at: int | None, completed_at: int | None
    ) -> dict[str, Any]:
        """Apply persisted item timing to a non-tool record."""
        if _index is not None:
            _index.track(record)
        record["completedAt"] = iso_timestamp(completed_at)
        record["durationMs"] = elapsed_milliseconds(started_at, completed_at)
        return record

    def handle_completed_item(
        payload: dict[str, Any], timestamp: int | None, event_number: int, line_number: int
    ) -> None:
        """Project the canonical paginated ``TurnItem`` carried by ItemCompleted."""
        nonlocal active_turn, after_tool_result, current_step
        nonlocal first_user, last_model_record, last_model_turn, pending_compaction
        item = payload.get("item")
        if not isinstance(item, dict):
            add_warning(
                warnings,
                "malformed_item_completed",
                line_number,
                f"Skipped malformed item_completed event on line {line_number}.",
            )
            return
        completed_at = epoch_milliseconds(payload.get("completed_at_ms"))
        if completed_at is None or completed_at <= 0:
            completed_at = timestamp
        timing_started_at = epoch_milliseconds(payload.get("started_at_ms"))
        if timing_started_at is not None and timing_started_at <= 0:
            timing_started_at = None
        # Keep point-only completed items chronologically placeable without claiming
        # that their completion timestamp is also a measured start boundary.
        started_at = timing_started_at if timing_started_at is not None else completed_at
        turn_id = payload.get("turn_id")
        normalized_turn_id = protocol_identifier(turn_id)
        if active_turn and turns:
            active_id = turns[-1].get("id")
            if active_id and normalized_turn_id and active_id != normalized_turn_id:
                previous = turns[-1]
                previous["completedAt"] = iso_timestamp(started_at)
                previous_started = parse_timestamp(previous.get("startedAt"))
                previous["durationMs"] = elapsed_milliseconds(previous_started, started_at)
                previous["status"] = "aborted"
                previous["error"] = (
                    "Turn was superseded by an item from another persisted turn."
                    if include_details
                    else "Turn ended without a matching completion event."
                )
                if _index is not None:
                    _index.track_turn_error(previous, "mismatched_item_turn")
                add_warning(
                    warnings,
                    "mismatched_item_turn",
                    line_number,
                    f"Completed item on line {line_number} did not match the active turn.",
                )
                active_turn = False
                current_step = 0
                after_tool_result = False
        ensure_turn(started_at, turn_id if isinstance(turn_id, str) else None)
        completed_type = item_type(item.get("type"))
        raw_id = item.get("id")
        item_id = raw_id if isinstance(raw_id, str) and raw_id else f"item-{event_number}"

        if completed_type == "user_message":
            text = content_text(item.get("content"))
            if not first_user and text:
                first_user = text
            content = item.get("content")
            content_items = content if isinstance(content, list) else []
            record = add_record(
                timestamp=started_at,
                kind="user",
                event="User",
                summary=text or "User message",
                record_id=item_id,
                input_detail=text or None,
                metadata_detail={
                    "images": sum(
                        isinstance(value, dict)
                        and value.get("type") in {"image", "local_image", "input_image"}
                        for value in content_items
                    ),
                    "audio": sum(
                        isinstance(value, dict)
                        and value.get("type") in {"audio", "local_audio", "input_audio"}
                        for value in content_items
                    ),
                },
            )
            finish_record_timing(record, timing_started_at, completed_at)
            return
        if completed_type == "hook_prompt":
            record = add_record(
                timestamp=started_at,
                kind="user",
                event="Hook prompt",
                summary="Internal hook prompt",
                record_id=item_id,
                input_detail=detail_json_text(item.get("fragments")),
            )
            finish_record_timing(record, timing_started_at, completed_at)
            return
        if completed_type in {"agent_message", "plan", "reasoning"}:
            if completed_type == "agent_message":
                text = content_text(item.get("content"))
                kind = "assistant"
                phase = safe_text(item.get("phase"), 80)
                label = "Assistant" + (f" · {phase}" if phase else "")
            elif completed_type == "plan":
                value = item.get("text")
                text = value if isinstance(value, str) else ""
                kind = "assistant"
                label = "Plan"
                phase = None
            else:
                text = reasoning_summary(item.get("summary_text"))
                kind = "reasoning"
                label = "Reasoning"
                phase = None
            step, turn = model_step(started_at)
            last_model_record = add_record(
                timestamp=started_at,
                kind=kind,
                event=label,
                summary=text or label,
                step=step,
                record_id=item_id,
                output_detail=text or None,
                metadata_detail={"phase": phase} if phase else None,
            )
            last_model_turn = turn
            finish_record_timing(last_model_record, timing_started_at, completed_at)
            return
        if completed_type == "sub_agent_activity":
            activity = safe_text(item.get("kind"), 80) or "activity"
            agent_path = safe_text(item.get("agent_path"), 200) or "subagent"
            record = add_record(
                timestamp=started_at,
                kind="subagent",
                event=f"Subagent · {activity}",
                summary=f"{agent_path} · {activity}",
                step=current_step or None,
                record_id=item_id,
                metadata_detail={
                    "agentPath": agent_path,
                    "agentThreadId": safe_text(item.get("agent_thread_id"), 100),
                    "activity": activity,
                },
            )
            finish_record_timing(record, timing_started_at, completed_at)
            return
        if completed_type == "context_compaction":
            if pending_compaction is None or pending_compaction.get("turn") != current_turn:
                pending_compaction = add_record(
                    timestamp=started_at,
                    kind="compaction",
                    event="Compaction",
                    summary="Context compacted",
                    step=current_step or None,
                    record_id=item_id,
                )
            else:
                pending_compaction["id"] = protocol_identifier(
                    item_id, pending_compaction.get("id")
                )
                pending_compaction["startedAt"] = iso_timestamp(started_at)
            finish_record_timing(pending_compaction, timing_started_at, completed_at)
            pending_compaction = None
            return
        if completed_type in {"entered_review_mode", "exited_review_mode"}:
            label = (
                "Entered review mode"
                if completed_type.startswith("entered")
                else "Exited review mode"
            )
            record = add_record(
                timestamp=started_at,
                kind="assistant",
                event="Review mode",
                summary=label,
                step=current_step or None,
                record_id=item_id,
                output_detail=detail_json_text(
                    {
                        key: item[key]
                        for key in ("target", "user_facing_hint", "review_output")
                        if key in item
                    }
                ),
            )
            finish_record_timing(record, timing_started_at, completed_at)
            return

        status = safe_text(item.get("status"), 80) or "completed"
        failed = status.casefold() in {"failed", "declined", "incomplete", "error"}
        duration = duration_milliseconds(item.get("duration"))
        if duration is None and "duration" in item:
            # An explicitly unconfirmed duration must not be replaced by a guessed interval.
            duration = 0
        event = "Tool"
        input_value: Any = None
        output_value: Any = None
        metadata_detail: dict[str, Any] = {"protocolType": completed_type}
        if completed_type == "command_execution":
            event = "Command"
            input_value = {key: item.get(key) for key in ("command", "cwd", "source")}
            output_value = {
                key: item.get(key)
                for key in ("stdout", "stderr", "aggregated_output", "exit_code")
                if item.get(key) is not None
            }
        elif completed_type == "dynamic_tool_call":
            tool = safe_text(item.get("tool"), 160) or "dynamic tool"
            namespace = safe_text(item.get("namespace"), 100)
            event = f"{namespace}.{tool}" if namespace else tool
            input_value = item.get("arguments")
            output_value = {
                key: item.get(key)
                for key in ("content_items", "error")
                if item.get(key) is not None
            }
            failed = failed or item.get("success") is False or bool(item.get("error"))
        elif completed_type == "collab_agent_tool_call":
            event = safe_text(item.get("tool"), 160) or "Agent tool"
            input_value = {
                key: item.get(key)
                for key in ("prompt", "model", "reasoning_effort", "receiver_thread_ids")
                if item.get(key) is not None
            }
            output_value = item.get("agents_states")
        elif completed_type == "web_search":
            action = item.get("action")
            action_kind = safe_text(action.get("type"), 80) if isinstance(action, dict) else None
            event = "Web search" + (f" · {action_kind}" if action_kind else "")
            input_value = {"query": item.get("query"), "action": action}
            output_value = item.get("results")
        elif completed_type == "image_view":
            event = "View image"
            input_value = {"path": item.get("path")}
        elif completed_type == "image_generation":
            event = "Image generation"
            input_value = {"revised_prompt": item.get("revised_prompt")}
            output_value = {"result": item.get("result"), "saved_path": item.get("saved_path")}
            failed = failed or status.casefold() != "completed"
        elif completed_type == "file_change":
            event = "Apply patch"
            input_value = item.get("changes")
            output_value = {key: item.get(key) for key in ("stdout", "stderr")}
        elif completed_type == "mcp_tool_call":
            server = safe_text(item.get("server"), 100)
            tool = safe_text(item.get("tool"), 160) or "MCP tool"
            event = f"{server}.{tool}" if server else tool
            input_value = item.get("arguments")
            result_value = item.get("result")
            output_value = result_value if result_value is not None else item.get("error")
            failed = failed or item.get("error") is not None or output_is_error(item.get("result"))
        elif completed_type == "extension":
            extension_kind = safe_text(item.get("kind"), 100) or "extension"
            metadata_detail["extensionKind"] = extension_kind
            if extension_kind == "clock.sleep":
                event = "Sleep"
                raw_duration = item.get("durationMs")
                duration = epoch_milliseconds(raw_duration)
                if duration is None and "durationMs" in item:
                    duration = 0
                input_value = {"durationMs": raw_duration}
            elif extension_kind == "web.search":
                action = item.get("action")
                action_kind = (
                    safe_text(action.get("type"), 80) if isinstance(action, dict) else None
                )
                event = "Web search" + (f" · {action_kind}" if action_kind else "")
                input_value = {"query": item.get("query"), "action": action}
                output_value = item.get("results")
            elif extension_kind == "image_gen.generation":
                event = "Image generation"
                input_value = {"revisedPrompt": item.get("revisedPrompt")}
                output_value = {
                    key: item.get(key)
                    for key in ("result", "savedPath", "failure")
                    if item.get(key) is not None
                }
                failed = failed or item.get("failure") is not None
            else:
                event = "Extension"
                input_value = {"kind": extension_kind}
                output_value = item
        else:
            add_warning(
                warnings,
                "unsupported_turn_item",
                line_number,
                f"Skipped unsupported persisted turn item {completed_type or '<missing>'}.",
            )
            return

        terminal_tool(
            event=event,
            timestamp=completed_at,
            started_at=timing_started_at,
            record_id=item_id,
            call_id=item_id,
            input_value=input_value,
            output_value=output_value,
            failed=failed,
            duration_ms=duration,
            metadata_detail=metadata_detail,
        )

    warnings: list[dict[str, Any]] = []

    def source_entries() -> Iterator[tuple[int, int, dict[str, Any]]]:
        for event_number, (line_number, entry) in enumerate(iter_session_jsonl(path, warnings), 1):
            if _index is not None:
                _index.enter_event(event_number, line_number)
            yield event_number, line_number, entry
            if _index is not None:
                _index.flush()

    for event_number, line_number, entry in _entries if _entries is not None else source_entries():
        entry_type = entry.get("type")
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        payload_type = payload.get("type")
        timestamp = parse_timestamp(entry.get("timestamp"))
        if timestamp is not None:
            first_time = timestamp if first_time is None else min(first_time, timestamp)
            last_time = timestamp if last_time is None else max(last_time, timestamp)

        if entry_type == "session_meta":
            continue
        if entry_type == "turn_context":
            context = {
                key: value
                for key, limit in (("model", 200), ("effort", 80))
                if (value := safe_text(payload.get(key), limit)) is not None
            }
            context["service_tier"] = normalized_service_tier(payload.get("service_tier"))
            turn_model = safe_text(payload.get("model"), 200)
            if active_turn and turns:
                turns[-1]["_serviceTier"] = context["service_tier"]
            if active_turn and turns and turn_model is not None:
                turns[-1]["model"] = turn_model
            continue
        if entry_type == "event_msg" and payload_type in {"task_started", "turn_started"}:
            started = parse_timestamp(payload.get("started_at"))
            if started is None:
                started = timestamp
            turn_id = payload.get("turn_id")
            normalized_turn_id = protocol_identifier(turn_id)
            if active_turn and turns:
                active_id = turns[-1].get("id")
                if active_id and normalized_turn_id and active_id != normalized_turn_id:
                    previous = turns[-1]
                    previous["completedAt"] = iso_timestamp(started)
                    previous_started = parse_timestamp(previous.get("startedAt"))
                    previous["durationMs"] = elapsed_milliseconds(previous_started, started)
                    previous["status"] = "aborted"
                    previous["error"] = (
                        "Turn was superseded by another persisted start event."
                        if include_details
                        else "Turn ended without a matching completion event."
                    )
                    if _index is not None:
                        _index.track_turn_error(previous, "overlapping_turn_start")
                    add_warning(
                        warnings,
                        "overlapping_turn_start",
                        line_number,
                        (
                            f"Started a new turn on line {line_number} before the prior turn "
                            "completed."
                        ),
                    )
                    active_turn = False
                    current_step = 0
                    after_tool_result = False
            turn = ensure_turn(started, turn_id if isinstance(turn_id, str) else None)
            started_tier = normalized_service_tier(payload.get("service_tier"))
            if started_tier is not None:
                turn["_serviceTier"] = started_tier
            turn_model = safe_text(payload.get("model"), 200)
            if turn_model is not None:
                turn["model"] = turn_model
            window = payload.get("model_context_window")
            if (
                not isinstance(window, bool)
                and isinstance(window, int)
                and 0 < window <= MAX_SAFE_INTEGER
            ):
                context_window = window
            if started is not None:
                turn["startedAt"] = iso_timestamp(started)
            continue
        if entry_type == "event_msg" and payload_type in {
            "task_complete",
            "turn_complete",
            "turn_aborted",
        }:
            completed = parse_timestamp(payload.get("completed_at"))
            if completed is None:
                completed = timestamp
            persisted_started = parse_timestamp(payload.get("started_at"))
            boundary = persisted_started if persisted_started is not None else completed
            if boundary is None:
                boundary = timestamp
            turn_id = payload.get("turn_id")
            normalized_turn_id = protocol_identifier(turn_id)
            if active_turn and turns:
                active_id = turns[-1].get("id")
                if active_id and normalized_turn_id and active_id != normalized_turn_id:
                    add_warning(
                        warnings,
                        "mismatched_turn_completion",
                        line_number,
                        f"Turn completion on line {line_number} did not match the active turn.",
                    )
                    previous = turns[-1]
                    previous["completedAt"] = iso_timestamp(boundary)
                    previous_started = parse_timestamp(previous.get("startedAt"))
                    previous["durationMs"] = elapsed_milliseconds(previous_started, boundary)
                    previous["status"] = "aborted"
                    previous["error"] = (
                        "Turn was superseded by a completion for another persisted turn."
                        if include_details
                        else "Turn ended without a matching completion event."
                    )
                    if _index is not None:
                        _index.track_turn_error(previous, "mismatched_turn_completion")
                    active_turn = False
                    current_step = 0
                    after_tool_result = False
            has_turn_start = active_turn
            turn = ensure_turn(boundary, turn_id if isinstance(turn_id, str) else None)
            started = persisted_started
            if started is None and has_turn_start:
                started = parse_timestamp(turn.get("startedAt"))
            turn["completedAt"] = iso_timestamp(completed)
            duration = payload.get("duration_ms")
            if (
                not isinstance(duration, bool)
                and isinstance(duration, (int, float))
                and _finite_number(duration)
                and duration >= 0
                and duration <= MAX_SAFE_INTEGER
            ):
                turn["durationMs"] = round(duration) if duration >= 1 else None
            elif "duration_ms" in payload:
                turn["durationMs"] = None
            else:
                turn["durationMs"] = elapsed_milliseconds(started, completed)
            ttft = payload.get("time_to_first_token_ms")
            if (
                not isinstance(ttft, bool)
                and isinstance(ttft, (int, float))
                and _finite_number(ttft)
                and ttft >= 0
                and ttft <= MAX_SAFE_INTEGER
            ):
                turn["timeToFirstTokenMs"] = round(ttft)
            aborted = payload_type == "turn_aborted"
            error_value = payload.get("reason") if aborted else payload.get("error")
            error_text: str | None = None
            if isinstance(error_value, str):
                error_text = error_value
            elif isinstance(error_value, dict) and isinstance(error_value.get("message"), str):
                error_text = error_value["message"]
            if aborted:
                turn["status"] = "aborted"
            elif error_value is not None:
                turn["status"] = "error"
            else:
                turn["status"] = "complete"
            if aborted:
                turn["error"] = (
                    safe_text(error_text, 1_000)
                    if include_details and error_text
                    else "Turn was aborted."
                )
            elif error_value is not None:
                turn["error"] = (
                    safe_text(error_text, 1_000)
                    if include_details and error_text
                    else "Turn completed with an error."
                )
            if _index is not None and turn["error"] is not None:
                _index.track_turn_error(turn, "turn_aborted" if aborted else "turn_error")
            active_turn = False
            current_step = 0
            after_tool_result = False
            continue
        if not paginated and entry_type == "event_msg" and payload_type == "user_message":
            message = payload.get("message")
            text = message if isinstance(message, str) else ""
            if not first_user and text:
                first_user = text
            turn = ensure_turn(timestamp)
            add_record(
                timestamp=timestamp,
                kind="user",
                event="User",
                summary=text or "User message",
                record_id=f"user-{event_number}",
                input_detail=text or None,
                metadata_detail={
                    "images": attachment_count(payload.get("images")),
                    "audio": attachment_count(payload.get("audio")),
                },
            )
            turn["steps"] = max(turn["steps"], current_step)
            continue
        if entry_type == "event_msg" and payload_type == "token_count":
            latest_rate_limits = merge_rate_limits(latest_rate_limits, payload.get("rate_limits"))
            info = payload.get("info")
            if isinstance(info, dict):
                (
                    previous_total_usage,
                    total_usage,
                    safe_last_usage,
                    usage_changed,
                ) = token_usage_snapshot(info, previous_total_usage)
                if total_usage is not None:
                    latest_usage = total_usage
                window = info.get("model_context_window")
                if (
                    not isinstance(window, bool)
                    and isinstance(window, int)
                    and 0 < window <= MAX_SAFE_INTEGER
                ):
                    context_window = window
                if usage_changed and safe_last_usage:
                    accounted_usage = merge_token_usage(accounted_usage, safe_last_usage)
                    usage_turn: dict[str, Any] | None = last_model_turn
                    if usage_turn is None and current_turn > 0 and turns:
                        usage_turn = turns[-1]
                    if usage_turn is not None:
                        usage_turn["usage"] = merge_token_usage(
                            usage_turn.get("usage"), safe_last_usage
                        )
                        usage_turn["modelCalls"] += 1
                        service_tier = (
                            normalized_service_tier(info.get("service_tier"))
                            or normalized_service_tier(payload.get("service_tier"))
                            or (
                                last_model_service_tier
                                if last_model_turn is not None
                                else usage_turn.get("_serviceTier")
                            )
                        )
                        call_cost = estimate_usage_cost(
                            usage_turn.get("model") or context.get("model"),
                            safe_last_usage,
                            service_tier=service_tier,
                        )
                        if service_tier in {"fast", "priority"}:
                            fast_cost_calls += 1
                        usage_turn["cost"] = merge_cost_estimates(usage_turn.get("cost"), call_cost)
                        if _index is not None:
                            _index.save_turn(usage_turn)
                        session_cost = merge_cost_estimates(session_cost, call_cost)
                    last_model_record = None
                    last_model_turn = None
                    last_model_service_tier = None
            continue
        if entry_type == "event_msg" and payload_type == "thread_rolled_back":
            rolled_back_turns = payload.get("num_turns")
            if (
                isinstance(rolled_back_turns, bool)
                or not isinstance(rolled_back_turns, int)
                or not 0 <= rolled_back_turns <= MAX_SAFE_INTEGER
            ):
                add_warning(
                    warnings,
                    "malformed_thread_rollback",
                    line_number,
                    f"Thread rollback on line {line_number} had an invalid turn count.",
                )
            else:
                add_warning(
                    warnings,
                    "thread_rolled_back",
                    line_number,
                    (
                        f"Thread history rolled back {rolled_back_turns} user turn(s); "
                        "preceding records remain visible as historical execution."
                    ),
                )
            continue
        if entry_type == "event_msg" and payload_type == "item_completed":
            handle_completed_item(payload, timestamp, event_number, line_number)
            continue
        if paginated and entry_type == "response_item" and payload_type != "agent_message":
            continue
        if entry_type == "response_item" and payload_type == "reasoning":
            text = reasoning_summary(payload.get("summary"))
            step, turn = model_step(timestamp)
            last_model_record = add_record(
                timestamp=timestamp,
                kind="reasoning",
                event="Reasoning",
                summary=text or "Encrypted reasoning (summary unavailable)",
                step=step,
                record_id=protocol_identifier(payload.get("id"), f"reasoning-{event_number}"),
                output_detail=text or None,
                metadata_detail={"encrypted": bool(payload.get("encrypted_content"))},
            )
            last_model_turn = turn
            continue
        if entry_type == "response_item" and payload_type == "message":
            role = payload.get("role")
            if role != "assistant":
                continue
            text = content_text(payload.get("content"))
            step, turn = model_step(timestamp)
            phase = safe_text(payload.get("phase"), 80)
            label = "Assistant" + (f" · {phase}" if phase else "")
            last_model_record = add_record(
                timestamp=timestamp,
                kind="assistant",
                event=label,
                summary=text or label,
                step=step,
                record_id=protocol_identifier(payload.get("id"), f"assistant-{event_number}"),
                output_detail=text or None,
                metadata_detail={"phase": phase} if phase else None,
            )
            last_model_turn = turn
            continue
        if entry_type == "response_item" and payload_type in {
            "function_call",
            "custom_tool_call",
        }:
            step, turn = model_step(timestamp)
            name = safe_text(payload.get("name"), 160) or "tool"
            namespace = safe_text(payload.get("namespace"), 100)
            if namespace:
                name = f"{namespace}.{name}"
            tool_name = name
            arguments = payload.get("arguments", payload.get("input"))
            raw_call_id = payload.get("call_id") or payload.get("id")
            call_id = protocol_identifier(raw_call_id)
            tool_record = add_record(
                timestamp=timestamp,
                kind="tool",
                event=tool_name,
                summary=f"{tool_name} · running",
                step=step,
                record_id=f"tool-{call_id or f'anonymous-{event_number}'}",
                input_detail=detail_json_text(arguments) if arguments is not None else None,
                status="running",
                call_id=call_id,
                metadata_detail={"protocolType": payload_type},
            )
            if last_model_record is None or last_model_record["turn"] != current_turn:
                last_model_record = tool_record
                last_model_turn = turn
            continue
        if entry_type == "response_item" and payload_type in {
            "local_shell_call",
            "tool_search_call",
        }:
            raw_call_id = payload.get("call_id") or payload.get("id")
            call_id = protocol_identifier(raw_call_id)
            if payload_type == "local_shell_call":
                event = "Local shell"
                input_value = payload.get("action")
            else:
                event = safe_text(payload.get("execution"), 160) or "Tool search"
                input_value = payload.get("arguments")
            status = safe_text(payload.get("status"), 80) or "in_progress"
            if status.casefold() in {"completed", "incomplete", "failed"}:
                terminal_tool(
                    event=event,
                    timestamp=timestamp,
                    record_id=f"tool-{call_id or f'anonymous-{event_number}'}",
                    call_id=call_id,
                    input_value=input_value,
                    failed=status.casefold() != "completed",
                    metadata_detail={"protocolType": payload_type},
                )
            else:
                step, turn = model_step(timestamp)
                tool_record = add_record(
                    timestamp=timestamp,
                    kind="tool",
                    event=event,
                    summary=f"{event} · running",
                    step=step,
                    record_id=f"tool-{call_id or f'anonymous-{event_number}'}",
                    input_detail=detail_json_text(input_value) if input_value is not None else None,
                    status="running",
                    call_id=call_id,
                    metadata_detail={"protocolType": payload_type},
                )
                if last_model_record is None or last_model_record["turn"] != current_turn:
                    last_model_record = tool_record
                    last_model_turn = turn
            continue
        if entry_type == "response_item" and payload_type == "tool_search_output":
            call_id = protocol_identifier(payload.get("call_id")) or ""
            record = tracked_calls.get(call_id)
            matched_evicted_call = call_id in call_states
            if record is None:
                record = add_record(
                    timestamp=timestamp,
                    kind="tool",
                    event="Tool search result",
                    summary="Tool search result without a retained call",
                    step=current_step or None,
                    record_id=f"tool-result-{call_id or event_number}",
                    call_id=call_id or None,
                    status="running",
                    count_tool=not matched_evicted_call,
                    has_start_boundary=False,
                )
                if not matched_evicted_call:
                    add_warning(
                        warnings,
                        "unmatched_tool_result",
                        line_number,
                        f"Tool search result on line {line_number} had no matching call record.",
                    )
            status = safe_text(payload.get("status"), 80) or "completed"
            failed = status.casefold() in {"failed", "incomplete", "error"}
            marker = late_tool_marker(record, timestamp)
            if marker is not None:
                finish_tool(
                    record,
                    timestamp=timestamp,
                    output=payload.get("tools"),
                    failed=failed,
                )
                record = marker
            finish_tool(
                record,
                timestamp=timestamp,
                output=payload.get("tools"),
                failed=failed,
            )
            after_tool_result = True
            continue
        if entry_type == "response_item" and payload_type in {
            "web_search_call",
            "image_generation_call",
        }:
            raw_call_id = payload.get("call_id") or payload.get("id")
            call_id = protocol_identifier(raw_call_id)
            status = safe_text(payload.get("status"), 80) or "completed"
            if payload_type == "web_search_call":
                action = payload.get("action")
                action_kind = (
                    safe_text(action.get("type"), 80) if isinstance(action, dict) else None
                )
                event = "Web search" + (f" · {action_kind}" if action_kind else "")
                input_value = action
                output_value = None
            else:
                event = "Image generation"
                input_value = {"revised_prompt": payload.get("revised_prompt")}
                output_value = payload.get("result")
            terminal_tool(
                event=event,
                timestamp=timestamp,
                record_id=f"tool-{call_id or f'anonymous-{event_number}'}",
                call_id=call_id,
                input_value=input_value,
                output_value=output_value,
                failed=status.casefold() not in {"completed", "complete", "succeeded"},
                metadata_detail={"protocolType": payload_type},
            )
            continue
        if entry_type == "response_item" and payload_type in {
            "compaction",
            "compaction_summary",
            "context_compaction",
        }:
            add_record(
                timestamp=timestamp,
                kind="compaction",
                event="Compaction",
                summary="Context compacted",
                step=current_step or None,
                record_id=protocol_identifier(payload.get("id"), f"compaction-{event_number}"),
            )
            continue
        if entry_type == "response_item" and payload_type in {
            "function_call_output",
            "custom_tool_call_output",
        }:
            call_id = protocol_identifier(payload.get("call_id")) or ""
            output = payload.get("output")
            completed_record = tracked_calls.get(call_id)
            matched_evicted_call = call_id in call_states
            if completed_record is None:
                completed_record = add_record(
                    timestamp=timestamp,
                    kind="tool",
                    event="Tool result",
                    summary="Tool result without a retained call",
                    step=current_step or None,
                    record_id=f"tool-result-{call_id or event_number}",
                    call_id=call_id or None,
                    status="running",
                    count_tool=not matched_evicted_call,
                    has_start_boundary=False,
                )
                if not matched_evicted_call:
                    add_warning(
                        warnings,
                        "unmatched_tool_result",
                        line_number,
                        f"Tool result on line {line_number} had no matching call record.",
                    )
            is_error = completed_record["status"] == "error" or output_is_error(output)
            marker = late_tool_marker(completed_record, timestamp)
            if marker is not None:
                finish_tool(
                    completed_record,
                    timestamp=timestamp,
                    output=output,
                    failed=is_error,
                )
                completed_record = marker
            finish_tool(
                completed_record,
                timestamp=timestamp,
                output=output,
                failed=is_error,
            )
            after_tool_result = True
            continue
        if (
            not paginated
            and entry_type == "event_msg"
            and payload_type
            in {
                "mcp_tool_call_end",
                "patch_apply_end",
                "web_search_end",
                "image_generation_end",
            }
        ):
            raw_completion_call_id = payload.get("call_id")
            completion_call_id = protocol_identifier(raw_completion_call_id)
            event = "Tool"
            completion_input: Any = None
            completion_output: Any = None
            failed = False
            duration = duration_milliseconds(payload.get("duration"))
            if duration is None and "duration" in payload:
                duration = 0
            if payload_type == "mcp_tool_call_end":
                invocation = payload.get("invocation")
                invocation_value = invocation if isinstance(invocation, dict) else {}
                server = safe_text(invocation_value.get("server"), 100)
                tool = safe_text(invocation_value.get("tool"), 160) or "MCP tool"
                event = f"{server}.{tool}" if server else tool
                completion_input = invocation_value.get("arguments")
                completion_output = payload.get("result")
                failed = payload.get("success") is False or output_is_error(completion_output)
            elif payload_type == "patch_apply_end":
                event = "Apply patch"
                completion_input = payload.get("changes")
                completion_output = {
                    key: payload.get(key)
                    for key in ("stdout", "stderr", "status")
                    if payload.get(key) is not None
                }
                status = safe_text(payload.get("status"), 80) or "completed"
                failed = payload.get("success") is False or status.casefold() in {
                    "failed",
                    "declined",
                    "error",
                }
            elif payload_type == "web_search_end":
                action = payload.get("action")
                action_kind = (
                    safe_text(action.get("type"), 80) if isinstance(action, dict) else None
                )
                event = "Web search" + (f" · {action_kind}" if action_kind else "")
                completion_input = {"query": payload.get("query"), "action": action}
                completion_output = payload.get("results")
            else:
                event = "Image generation"
                status = safe_text(payload.get("status"), 80) or "completed"
                completion_input = {"revised_prompt": payload.get("revised_prompt")}
                completion_output = {
                    key: payload.get(key)
                    for key in ("result", "saved_path", "failure")
                    if payload.get(key) is not None
                }
                failed = status.casefold() not in {"completed", "complete", "succeeded"}
                failed = failed or payload.get("failure") is not None
            terminal_tool(
                event=event,
                timestamp=timestamp,
                record_id=f"tool-{completion_call_id or event_number}",
                call_id=completion_call_id,
                input_value=completion_input,
                output_value=completion_output,
                failed=failed,
                duration_ms=duration,
                metadata_detail={"protocolType": payload_type},
            )
            continue
        if not paginated and entry_type == "event_msg" and payload_type == "sub_agent_activity":
            activity = safe_text(payload.get("kind"), 80) or "activity"
            agent_path = safe_text(payload.get("agent_path"), 200) or "subagent"
            event_id = protocol_identifier(payload.get("event_id"))
            occurred_at = epoch_milliseconds(payload.get("occurred_at_ms"))
            if occurred_at is None:
                occurred_at = timestamp
            add_record(
                timestamp=occurred_at,
                kind="subagent",
                event=f"Subagent · {activity}",
                summary=f"{agent_path} · {activity}",
                step=current_step or None,
                record_id=f"subagent-{event_id or event_number}",
                metadata_detail={
                    "agentPath": agent_path,
                    "agentThreadId": safe_text(payload.get("agent_thread_id"), 100),
                    "activity": activity,
                },
            )
            continue
        if (
            not paginated
            and entry_type == "event_msg"
            and payload_type
            in {
                "entered_review_mode",
                "exited_review_mode",
            }
        ):
            entered = payload_type == "entered_review_mode"
            label = "Entered review mode" if entered else "Exited review mode"
            add_record(
                timestamp=timestamp,
                kind="assistant",
                event="Review mode",
                summary=label,
                step=current_step or None,
                record_id=protocol_identifier(payload.get("item_id"), f"review-{event_number}"),
                output_detail=detail_json_text(
                    {
                        key: payload[key]
                        for key in ("target", "user_facing_hint", "review_output")
                        if key in payload
                    }
                ),
            )
            continue
        if entry_type == "compacted":
            message = payload.get("message")
            details = message if isinstance(message, str) else None
            pending_compaction = add_record(
                timestamp=timestamp,
                kind="compaction",
                event="Compaction",
                summary="Context compacted",
                step=current_step or None,
                record_id=f"compaction-{event_number}",
                output_detail=details,
            )
            continue
        if not paginated and entry_type == "event_msg" and payload_type == "context_compacted":
            if pending_compaction is None or pending_compaction.get("turn") != current_turn:
                pending_compaction = add_record(
                    timestamp=timestamp,
                    kind="compaction",
                    event="Compaction",
                    summary="Context compacted",
                    step=current_step or None,
                    record_id=f"compaction-{event_number}",
                )
            if _index is not None:
                _index.track(pending_compaction)
            pending_compaction["completedAt"] = iso_timestamp(timestamp)
            pending_compaction = None
            continue
        if entry_type == "response_item" and payload_type == "agent_message":
            text = content_text(payload.get("content"))
            passthrough = payload.get("internal_chat_message_metadata_passthrough")
            response_turn_id = passthrough.get("turn_id") if isinstance(passthrough, dict) else None
            ensure_turn(timestamp, response_turn_id if isinstance(response_turn_id, str) else None)
            add_record(
                timestamp=timestamp,
                kind="subagent",
                event="Agent message",
                summary=text or "Inter-agent message",
                step=current_step or None,
                record_id=protocol_identifier(payload.get("id"), f"agent-message-{event_number}"),
                output_detail=text or None,
                metadata_detail={
                    "author": safe_text(payload.get("author"), 200),
                    "recipient": safe_text(payload.get("recipient"), 200),
                },
            )
            continue
        if entry_type == "inter_agent_communication":
            communication_content = payload.get("content")
            communication_text = (
                communication_content if isinstance(communication_content, str) else ""
            )
            raw_recipients = payload.get("other_recipients")
            recipients = raw_recipients if isinstance(raw_recipients, list) else []
            add_record(
                timestamp=timestamp,
                kind="subagent",
                event="Agent communication",
                summary=communication_text or "Inter-agent communication",
                step=current_step or None,
                record_id=protocol_identifier(
                    payload.get("id"), f"agent-communication-{event_number}"
                ),
                output_detail=communication_text or None,
                metadata_detail={
                    "author": safe_text(payload.get("author"), 200),
                    "recipient": safe_text(payload.get("recipient"), 200),
                    "otherRecipients": [
                        safe for value in recipients if (safe := safe_text(value, 200)) is not None
                    ][:20],
                    "triggerTurn": payload.get("trigger_turn") is True,
                },
            )
            continue
        if entry_type == "response_item" and isinstance(payload_type, str):
            add_warning(
                warnings,
                "unsupported_response_item",
                line_number,
                f"Skipped unsupported persisted response item {payload_type}.",
            )

    if active_turn and turns:
        turn = turns[-1]
        turn["status"] = "running"
        if last_time is not None and turn.get("startedAt"):
            started = parse_timestamp(turn["startedAt"])
            turn["durationMs"] = elapsed_milliseconds(started, last_time)

    for record in records:
        if record["status"] == "running":
            record["summary"] = tool_summary(record, "running")

    if _index is not None:
        for record in records:
            _index.pending[int(record["index"])] = record
        _index.flush()
        for turn in {**retained_turns, **{turn["index"]: turn for turn in turns}}.values():
            _index.save_turn(turn)

    visible_records = list(records)
    # Bound serialized detail size as well as record count. Escaping is included
    # so unusual Unicode/control characters cannot exceed the transport limit.
    retained_bytes = 0
    for record in reversed(visible_records):
        record_bytes = len(json.dumps(record, ensure_ascii=True).encode("ascii"))
        if retained_bytes + record_bytes > MAX_TRAJECTORY_PAGE_BYTES:
            visible_records = [item for item in visible_records if item["index"] > record["index"]]
            add_warning(
                warnings,
                "page_byte_limit",
                0,
                "Page size was reduced; load earlier records to continue.",
            )
            break
        retained_bytes += record_bytes
    first_record = visible_records[0]["index"] if visible_records else None
    last_record = visible_records[-1]["index"] if visible_records else None
    earlier_records = first_record - 1 if first_record is not None else 0
    later_records = all_record_count - last_record if last_record is not None else all_record_count
    omitted = earlier_records + later_records
    for record in visible_records:
        for private_key in tuple(key for key in record if key.startswith("_")):
            del record[private_key]
    visible_turn_indices = {record["turn"] for record in visible_records}
    visible_turns = [
        turn for index, turn in retained_turns.items() if index in visible_turn_indices
    ]
    for turn in reversed(turns):
        if len(visible_turns) >= MAX_TURNS:
            break
        turn_index = turn["index"]
        if turn_index not in visible_turn_indices:
            visible_turns.append(turn)
            visible_turn_indices.add(turn_index)
    visible_turns.sort(key=lambda turn: turn["index"])
    for turn in visible_turns:
        turn.pop("_serviceTier", None)
    if fast_cost_calls:
        # Retain this accounting notice even if malformed input filled the warning budget.
        pricing_notices: list[dict[str, Any]] = []
        add_warning(
            pricing_notices,
            "codex_fast_cost_multiplier",
            1,
            f"Fast x2.5 included-usage estimate applied to {fast_cost_calls} usage sample(s); "
            "this is not an API invoice or purchased-credit bill.",
        )
        warnings = (pricing_notices + warnings)[:MAX_WARNINGS]
    git = safe_git(metadata.get("git"))
    model = context.get("model") if isinstance(context.get("model"), str) else None
    effort = context.get("effort") if isinstance(context.get("effort"), str) else None
    session = {
        "id": metadata_identity(metadata, path),
        "title": shorten(first_user or "Untitled Codex task", 120),
        "cwd": display_path(metadata.get("cwd")),
        "model": model,
        "effort": effort,
        "originator": safe_text(metadata.get("originator"), 120),
        "sourceKind": source_kind(metadata.get("source")),
        "startedAt": iso_timestamp(first_time),
        "updatedAt": iso_timestamp(last_time),
        "archived": is_archived_session(path),
        "parentThreadId": safe_text(metadata.get("parent_thread_id"), 100),
        "agentPath": safe_text(metadata.get("agent_path"), 200),
        "git": git,
    }
    return {
        "schemaVersion": TRAJECTORY_SCHEMA_VERSION,
        "detailLevel": detail_level,
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "session": session,
        "pagination": {
            "firstRecord": first_record,
            "lastRecord": last_record,
            "earlierRecords": earlier_records,
            "laterRecords": later_records,
            "hasEarlier": earlier_records > 0,
            "hasLater": later_records > 0,
            "nextBeforeRecord": first_record if earlier_records > 0 else None,
        },
        "stats": {
            "turns": current_turn,
            **(
                {
                    "visibleTurns": len(visible_turns),
                    "omittedTurns": current_turn - len(visible_turns),
                }
                if current_turn > len(visible_turns)
                else {}
            ),
            "records": all_record_count,
            "visibleRecords": len(visible_records),
            "omittedRecords": omitted,
            "toolCalls": tool_calls,
            "failedTools": failed_tools,
            "compactions": compactions,
            "tokens": accounted_usage if accounted_usage is not None else latest_usage,
            "cost": session_cost,
            "contextWindow": context_window,
            "rateLimits": latest_rate_limits,
        },
        "turns": visible_turns,
        "records": visible_records,
        "warnings": warnings,
    }


def cached_trajectory(
    path: Path,
    max_records: int = DEFAULT_MAX_RECORDS,
    detail_level: DetailLevel = "summary",
    before_record: int | None = None,
) -> dict[str, Any]:
    """Return one immutable-signature projection from a small process cache."""
    normalized_detail = normalize_detail_level(detail_level)
    limited = max(MIN_RECORDS, min(int(max_records), MAX_RECORDS))
    signature = session_signature(path)
    key = (path, signature, limited, normalized_detail, before_record, jsonl_byte_limit())
    with _TRAJECTORY_CACHE_LOCK:
        cached = _TRAJECTORY_CACHE.get(key)
        if cached is not None:
            _TRAJECTORY_CACHE.move_to_end(key)
            return deepcopy(cached)

    index_key = (path, signature, jsonl_byte_limit())
    with _PAGE_INDEX_LOCK:
        index = _PAGE_INDEXES.get(index_key)
        if index is None:
            try:
                index = PageIndex()
                summary = parse_session(path, limited, "summary", before_record, _index=index)
                index.finish(summary)
                if not index.enabled or session_signature(path) != signature:
                    index.close()
                    index = None
                else:
                    _PAGE_INDEXES[index_key] = index
            except (OSError, sqlite3.Error):
                if index is not None:
                    index.close()
                index = None
            except BaseException:
                if index is not None:
                    index.close()
                raise
        if index is not None:
            _PAGE_INDEXES.move_to_end(index_key)
            trajectory = index.page(limited, before_record, MAX_TRAJECTORY_PAGE_BYTES)
            if index.details_ambiguous:
                # Indexed IDs reflect the initial page's collision window. Reparse
                # ambiguous summaries too, so enlarging a page cannot repeat IDs.
                trajectory = parse_session(path, limited, normalized_detail, before_record)
                if normalized_detail == "full" and session_signature(path) != signature:
                    raise ValueError("Codex rollout changed during detail read; refresh and retry.")
            elif normalized_detail == "full":
                details = parse_session(
                    path, MAX_RECORDS, "full", _entries=index.detail_entries(trajectory["records"])
                )
                by_id = {record["id"]: record for record in details["records"]}
                by_call = {
                    record["callId"]: record for record in details["records"] if record["callId"]
                }
                for record in trajectory["records"]:
                    detail = by_id.get(record["id"])
                    if detail is None and record["kind"] == "tool":
                        detail = by_call.get(record["callId"])
                    if detail is None:
                        # Unusual identifier collisions must retain the canonical projection.
                        trajectory = parse_session(path, limited, "full", before_record)
                        break
                    for field in ("input", "output", "metadata", "error", "summary"):
                        record[field] = detail[field]
                retained_bytes = 0
                kept: list[dict[str, Any]] = []
                for record in reversed(trajectory["records"]):
                    record_bytes = len(json.dumps(record, ensure_ascii=True).encode("ascii"))
                    if retained_bytes + record_bytes > MAX_TRAJECTORY_PAGE_BYTES:
                        break
                    kept.append(record)
                    retained_bytes += record_bytes
                if len(kept) < len(trajectory["records"]):
                    end = trajectory["pagination"]["lastRecord"] + 1
                    trajectory = index.page(len(kept), end, MAX_TRAJECTORY_PAGE_BYTES)
                    trajectory["records"] = list(reversed(kept))
                    add_warning(
                        trajectory["warnings"],
                        "page_byte_limit",
                        0,
                        "Page size was reduced; load earlier records to continue.",
                    )
                index.hydrate_turn_errors(trajectory["turns"])
                trajectory["detailLevel"] = "full"
                if session_signature(path) != signature:
                    raise ValueError("Codex rollout changed during detail read; refresh and retry.")
            while _PAGE_INDEXES and (
                len(_PAGE_INDEXES) > MAX_PAGE_INDEXES
                or sum(item.size for item in _PAGE_INDEXES.values()) > MAX_PAGE_INDEX_TOTAL_BYTES
            ):
                _, evicted_index = _PAGE_INDEXES.popitem(last=False)
                evicted_index.close()
        else:
            trajectory = parse_session(path, limited, normalized_detail, before_record)
    size = len(json.dumps(trajectory, ensure_ascii=True).encode("ascii"))
    if session_signature(path) == signature:
        with _TRAJECTORY_CACHE_LOCK:
            _TRAJECTORY_CACHE[key] = trajectory
            _TRAJECTORY_CACHE_SIZES[key] = size
            _TRAJECTORY_CACHE.move_to_end(key)
            while _TRAJECTORY_CACHE and (
                len(_TRAJECTORY_CACHE) > MAX_TRAJECTORY_CACHE
                or sum(_TRAJECTORY_CACHE_SIZES.get(item, 0) for item in _TRAJECTORY_CACHE)
                > MAX_TRAJECTORY_CACHE_BYTES
            ):
                evicted_key, _ = _TRAJECTORY_CACHE.popitem(last=False)
                _TRAJECTORY_CACHE_SIZES.pop(evicted_key, None)
            # Cache clears (including tests) must not retain stale size keys.
            for old_key in _TRAJECTORY_CACHE_SIZES.keys() - _TRAJECTORY_CACHE.keys():
                del _TRAJECTORY_CACHE_SIZES[old_key]
    return deepcopy(trajectory)


def prewarm_caches() -> None:
    """Warm the bounded session and latest-summary caches after MCP startup."""
    path = resolve_session(None, include_archived=False)
    cached_trajectory(path, DEFAULT_MAX_RECORDS, "summary")


def _read_limit_task_id(path: Path) -> str | None:
    try:
        metadata = first_session_metadata(path)
        for key in ("id", "session_id"):
            if identity := protocol_identifier(metadata.get(key)):
                return identity
        return None
    except (OSError, ValueError):
        return None


def trajectory_result(
    arguments: dict[str, Any], with_ui: bool, *, exact_session_id: str | None = None
) -> dict[str, Any]:
    """Resolve and project a session for one MCP tool call."""
    allowed = {
        "sessionId",
        "maxRecords",
        "beforeRecord",
        "includeArchived",
        "detailLevel",
        "maxReadBytes",
    }
    reject_unknown_arguments(arguments, allowed)
    include_archived = arguments.get("includeArchived", False)
    if not isinstance(include_archived, bool):
        raise ValueError("includeArchived must be a boolean.")
    session_id = arguments.get("sessionId")
    if session_id is not None and not isinstance(session_id, str):
        raise ValueError("sessionId must be a string.")
    requested_max = arguments.get("maxRecords", DEFAULT_MAX_RECORDS)
    if isinstance(requested_max, bool) or not isinstance(requested_max, int):
        raise ValueError("maxRecords must be an integer.")
    if not MIN_RECORDS <= requested_max <= MAX_RECORDS:
        raise ValueError(f"maxRecords must be between {MIN_RECORDS} and {MAX_RECORDS}.")
    before_record = arguments.get("beforeRecord")
    if before_record is not None:
        if isinstance(before_record, bool) or not isinstance(before_record, int):
            raise ValueError("beforeRecord must be an integer.")
        if not 1 <= before_record <= MAX_SAFE_INTEGER:
            raise ValueError(f"beforeRecord must be between 1 and {MAX_SAFE_INTEGER}.")
    detail_level = normalize_detail_level(arguments.get("detailLevel", "summary"))
    if detail_level == "full" and (session_id is None or session_id.strip() == "latest"):
        raise ValueError("Full details require an explicit sessionId selected from a summary.")
    with session_read_limit(arguments.get("maxReadBytes")):
        if arguments.get("maxReadBytes") is not None and (
            session_id is None or session_id.strip() == "latest"
        ):
            raise ValueError("maxReadBytes requires an explicit sessionId.")
        path = resolve_session(session_id, include_archived)
        try:
            if exact_session_id is not None and _read_limit_task_id(path) != exact_session_id:
                raise ValueError("Calling task identity is unavailable or does not match metadata.")
            revision: str | None = trajectory_revision(path)
            trajectory = cached_trajectory(path, requested_max, detail_level, before_record)
            if trajectory_revision(path) != revision:
                revision = None
        except ReadLimitExceeded as error:
            error.session_id = _read_limit_task_id(path)
            error.session_id_verified = error.session_id is not None
            raise
    stats = trajectory["stats"]
    summary = (
        f"Trajectory for {trajectory['session']['id']}: "
        f"{stats['turns']} turns, {stats['records']} records, "
        f"{stats['toolCalls']} tool calls, {stats['failedTools']} failed tools."
    )
    result: dict[str, Any] = {
        "structuredContent": trajectory,
        "content": [{"type": "text", "text": summary}],
        "_meta": {"codex-trajectory/revision": revision} if revision else {},
    }
    if with_ui:
        result["_meta"]["ui"] = {"resourceUri": UI_URI}
    return result


def trajectory_revision(path: Path) -> str:
    """Return an opaque revision for every file in a rollout lineage."""
    digest = hashlib.sha256()
    for item in session_signature(path):
        for value in item:
            encoded = str(value).encode("utf-8", errors="replace")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def trajectory_update_result(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return a safe live-view update only when the selected rollout changed."""
    reject_unknown_arguments(
        arguments, {"sessionId", "revision", "includeArchived", "maxReadBytes"}
    )
    include_archived = arguments.get("includeArchived", False)
    if not isinstance(include_archived, bool):
        raise ValueError("includeArchived must be a boolean.")
    session_id = arguments.get("sessionId")
    if session_id is not None and not isinstance(session_id, str):
        raise ValueError("sessionId must be a string.")
    revision = arguments.get("revision")
    if revision is not None:
        if not isinstance(revision, str):
            raise ValueError("revision must be a string.")
        if len(revision) != 64 or any(
            character not in "0123456789abcdef" for character in revision
        ):
            raise ValueError("revision must be a lowercase SHA-256 digest.")

    with session_read_limit(arguments.get("maxReadBytes")):
        if arguments.get("maxReadBytes") is not None and (
            session_id is None or session_id.strip() == "latest"
        ):
            raise ValueError("maxReadBytes requires an explicit sessionId.")
        path = resolve_session(session_id, include_archived)
        current_revision = trajectory_revision(path)
        update: dict[str, Any] = {
            "schemaVersion": TRAJECTORY_SCHEMA_VERSION,
            "unchanged": revision == current_revision,
            "revision": current_revision,
            "quota": latest_account_quota(),
        }
        if revision != current_revision:
            try:
                update["trajectory"] = cached_trajectory(path, LIVE_MAX_RECORDS, "summary")
            except ReadLimitExceeded as error:
                error.session_id = _read_limit_task_id(path)
                error.session_id_verified = error.session_id is not None
                raise
    state = "unchanged" if update["unchanged"] else "updated"
    return {
        "structuredContent": update,
        "content": [{"type": "text", "text": f"Live trajectory {state}."}],
    }


def tool_definitions() -> list[dict[str, Any]]:
    """Compatibility import for the separated MCP tool layer."""
    from .tools import tool_definitions as definitions

    return definitions()


def call_tool(
    name: str, arguments: Any, *, metadata: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Compatibility import for the separated MCP tool layer."""
    from .tools import call_tool as dispatch

    return dispatch(name, arguments, metadata=metadata)


def reject_unknown_arguments(arguments: dict[str, Any], allowed: set[str]) -> None:
    """Reject tool fields that are not declared by the public input schema."""
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise ValueError(f"Unknown argument(s): {', '.join(unknown)}.")


def ui_html() -> str:
    """Read the bundled, dependency-free trajectory component."""
    assets = Path(__file__).resolve().parent.parent.parent / "assets"
    html = (assets / "trajectory.html").read_text(encoding="utf-8")
    sprite = b64encode((assets / "whale-girl-mining-32f.png").read_bytes()).decode("ascii")
    bridge = (assets / "mcp-app-bridge.js").read_text(encoding="utf-8")
    return (
        html.replace("/* __MCP_APP_BRIDGE__ */", bridge)
        .replace("__TRAJECTORY_VERSION__", SERVER_VERSION)
        .replace(
            "__WHALE_MINING_SPRITE_DATA_URI__",
            f"data:image/png;base64,{sprite}",
        )
    )

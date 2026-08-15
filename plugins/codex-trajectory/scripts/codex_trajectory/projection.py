#!/usr/bin/env python3
"""Project local Codex rollout logs into privacy-aware trajectories."""

from __future__ import annotations

import json
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .privacy import (
    DetailLevel,
    bounded,
    content_text,
    display_path,
    json_text,
    normalize_detail_level,
    reasoning_summary,
    safe_git,
    shorten,
)
from .sessions import iter_jsonl, session_files

SERVER_NAME = "codex-trajectory"
SERVER_VERSION = "0.1.0"
UI_URI = "ui://codex-trajectory/trajectory-v1.html"
DEFAULT_MAX_RECORDS = 500
MAX_RECORDS = 1_000


def parse_timestamp(value: Any) -> int | None:
    """Convert an ISO timestamp or Unix value to epoch milliseconds."""
    if isinstance(value, (int, float)):
        number = float(value)
        return round(number * 1000) if number < 10_000_000_000 else round(number)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return round(parsed.timestamp() * 1000)


def iso_timestamp(milliseconds: int | None) -> str | None:
    """Format epoch milliseconds as an ISO timestamp."""
    if milliseconds is None:
        return None
    return (
        datetime.fromtimestamp(milliseconds / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
    )


def session_overview(path: Path) -> dict[str, Any]:
    """Build a compact session summary without returning transcript bodies."""
    metadata: dict[str, Any] = {}
    first_user = ""
    model: str | None = None
    effort: str | None = None
    collaboration: str | None = None
    first_time: int | None = None
    last_time: int | None = None
    turns = 0
    tool_calls = 0
    latest_usage: dict[str, Any] | None = None
    for _, entry in iter_jsonl(path):
        timestamp = parse_timestamp(entry.get("timestamp"))
        if timestamp is not None:
            first_time = timestamp if first_time is None else min(first_time, timestamp)
            last_time = timestamp if last_time is None else max(last_time, timestamp)
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        entry_type = entry.get("type")
        payload_type = payload.get("type")
        if entry_type == "session_meta":
            metadata = payload
        elif entry_type == "turn_context":
            if isinstance(payload.get("model"), str):
                model = payload["model"]
            if isinstance(payload.get("effort"), str):
                effort = payload["effort"]
            mode = payload.get("collaboration_mode")
            if isinstance(mode, dict) and isinstance(mode.get("mode"), str):
                collaboration = mode["mode"]
            elif isinstance(payload.get("collaboration_mode_kind"), str):
                collaboration = payload["collaboration_mode_kind"]
        elif entry_type == "event_msg" and payload_type == "user_message" and not first_user:
            message = payload.get("message")
            first_user = message if isinstance(message, str) else ""
        elif entry_type == "event_msg" and payload_type == "task_started":
            turns += 1
        elif entry_type == "event_msg" and payload_type == "token_count":
            info = payload.get("info")
            if isinstance(info, dict):
                usage = info.get("total_token_usage")
                if isinstance(usage, dict):
                    latest_usage = usage
        elif entry_type == "response_item" and payload_type in {
            "function_call",
            "custom_tool_call",
        }:
            tool_calls += 1
    session_id = metadata.get("session_id") or metadata.get("id") or path.stem
    updated_ms = round(path.stat().st_mtime * 1000)
    return {
        "id": str(session_id),
        "title": shorten(first_user or "Untitled Codex task", 100),
        "cwd": display_path(metadata.get("cwd")),
        "model": model,
        "effort": effort,
        "collaborationMode": collaboration,
        "startedAt": iso_timestamp(first_time),
        "updatedAt": iso_timestamp(last_time or updated_ms),
        "turns": turns,
        "toolCalls": tool_calls,
        "tokens": latest_usage,
        "archived": "archived_sessions" in path.parts,
        "parentThreadId": metadata.get("parent_thread_id"),
        "agentPath": metadata.get("agent_path"),
        "git": safe_git(metadata.get("git")),
    }


def list_session_overviews(
    limit: int = 20, query: str = "", include_archived: bool = False
) -> list[dict[str, Any]]:
    """List recent session summaries with an optional case-insensitive filter."""
    needle = query.casefold().strip()
    result: list[dict[str, Any]] = []
    for path in session_files(include_archived):
        overview = session_overview(path)
        haystack = " ".join(
            str(overview.get(key) or "") for key in ("id", "title", "cwd", "model")
        ).casefold()
        if needle and needle not in haystack:
            continue
        result.append(overview)
        if len(result) >= limit:
            break
    return result


def resolve_session(session_id: str | None, include_archived: bool) -> Path:
    """Resolve a session identifier without accepting arbitrary filesystem paths."""
    paths = session_files(include_archived)
    if not paths:
        raise ValueError("No local Codex session logs were found.")
    if session_id is None or not session_id.strip() or session_id == "latest":
        return paths[0]
    requested = session_id.strip()
    exact: list[Path] = []
    prefix: list[Path] = []
    for path in paths:
        overview = session_overview(path)
        candidate = str(overview["id"])
        if candidate == requested or path.stem == requested:
            exact.append(path)
        elif candidate.startswith(requested) or requested in path.stem:
            prefix.append(path)
    matches = exact or prefix
    if not matches:
        raise ValueError(f"Codex session {requested!r} was not found.")
    if len(matches) > 1 and not exact:
        raise ValueError(f"Codex session prefix {requested!r} is ambiguous.")
    return matches[0]


def output_is_error(value: Any) -> bool:
    """Read common MCP/tool failure markers without interpreting body text."""
    parsed = value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return False
    if isinstance(parsed, dict):
        return parsed.get("isError") is True or parsed.get("success") is False or "Err" in parsed
    return False


def parse_session(
    path: Path,
    max_records: int = DEFAULT_MAX_RECORDS,
    detail_level: DetailLevel = "summary",
) -> dict[str, Any]:
    """Project one Codex rollout log into a turn-aware trajectory."""
    detail_level = normalize_detail_level(detail_level)
    include_details = detail_level == "full"
    limited = max(50, min(int(max_records), MAX_RECORDS))
    records: deque[dict[str, Any]] = deque(maxlen=limited)
    visible_calls: dict[str, dict[str, Any]] = {}
    tool_failures: dict[str, bool] = {}
    turns: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}
    context: dict[str, Any] = {}
    latest_usage: dict[str, Any] | None = None
    context_window: int | None = None
    current_turn = 0
    current_step = 0
    active_turn = False
    after_tool_result = False
    last_model_record: dict[str, Any] | None = None
    first_user = ""
    first_time: int | None = None
    last_time: int | None = None
    all_record_count = 0
    tool_calls = 0
    compactions = 0

    def ensure_turn(timestamp: int | None, turn_id: str | None = None) -> dict[str, Any]:
        nonlocal current_turn, current_step, active_turn, after_tool_result
        if not active_turn:
            current_turn += 1
            current_step = 0
            after_tool_result = False
            active_turn = True
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
                }
            )
        turn = turns[-1]
        if turn_id and not turn.get("id"):
            turn["id"] = turn_id
        if turn.get("startedAt") is None and timestamp is not None:
            turn["startedAt"] = iso_timestamp(timestamp)
        return turn

    def model_step(timestamp: int | None) -> tuple[int, dict[str, Any]]:
        nonlocal current_step, after_tool_result
        turn = ensure_turn(timestamp)
        if current_step == 0 or after_tool_result:
            current_step += 1
            after_tool_result = False
        turn["steps"] = max(turn["steps"], current_step)
        return current_step, turn

    def add_record(
        *,
        timestamp: int | None,
        kind: str,
        event: str,
        summary: str,
        step: int | None = None,
        record_id: str | None = None,
        input_detail: str | None = None,
        output_detail: str | None = None,
        status: str = "complete",
        call_id: str | None = None,
        metadata_detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        nonlocal all_record_count, tool_calls, compactions
        turn = ensure_turn(timestamp)
        all_record_count += 1
        record = {
            "index": all_record_count,
            "id": record_id or f"record-{all_record_count}",
            "turn": current_turn,
            "step": step,
            "kind": kind,
            "event": event,
            "summary": shorten(summary or event),
            "startedAt": iso_timestamp(timestamp),
            "completedAt": iso_timestamp(timestamp) if status != "running" else None,
            "durationMs": 0 if timestamp is not None and status != "running" else None,
            "status": status,
            "callId": call_id,
            "input": bounded(input_detail) if include_details and input_detail else None,
            "output": bounded(output_detail) if include_details and output_detail else None,
            "error": None,
            "usage": None,
            "metadata": (metadata_detail or {}) if include_details else {},
        }
        if len(records) == limited:
            evicted = records[0]
            evicted_call_id = evicted.get("callId")
            if isinstance(evicted_call_id, str) and visible_calls.get(evicted_call_id) is evicted:
                del visible_calls[evicted_call_id]
        records.append(record)
        if kind == "tool":
            tool_calls += 1
            tool_key = call_id or f"record:{record['index']}"
            tool_failures.setdefault(tool_key, False)
            if call_id:
                visible_calls[call_id] = record
        elif kind == "compaction":
            compactions += 1
        turn["records"] += 1
        return record

    def mark_tool_error(record: dict[str, Any], message: str) -> None:
        """Mark one retained tool record and its aggregate state as failed."""
        record["status"] = "error"
        record["error"] = record.get("error") or message
        record["summary"] = shorten(f"{record['event']} · error")
        tool_key = record.get("callId") or f"record:{record['index']}"
        tool_failures[str(tool_key)] = True

    def mark_call_error(call_id: str) -> None:
        """Remember a failure even when its record has fallen outside the visible tail."""
        if call_id in tool_failures:
            tool_failures[call_id] = True

    warnings: list[dict[str, Any]] = []
    for line_number, entry in iter_jsonl(path, warnings):
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
            metadata = payload
            continue
        if entry_type == "turn_context":
            context = payload
            continue
        if entry_type == "event_msg" and payload_type == "task_started":
            started = parse_timestamp(payload.get("started_at")) or timestamp
            turn = ensure_turn(started, str(payload.get("turn_id") or ""))
            window = payload.get("model_context_window")
            if isinstance(window, int):
                context_window = window
            turn["startedAt"] = iso_timestamp(started)
            continue
        if entry_type == "event_msg" and payload_type in {"task_complete", "turn_aborted"}:
            completed = parse_timestamp(payload.get("completed_at")) or timestamp
            turn = ensure_turn(parse_timestamp(payload.get("started_at")) or timestamp)
            turn["completedAt"] = iso_timestamp(completed)
            duration = payload.get("duration_ms")
            if isinstance(duration, (int, float)):
                turn["durationMs"] = round(duration)
            ttft = payload.get("time_to_first_token_ms")
            if isinstance(ttft, (int, float)):
                turn["timeToFirstTokenMs"] = round(ttft)
            aborted = payload_type == "turn_aborted"
            turn["status"] = "aborted" if aborted else "complete"
            reason = payload.get("reason") if aborted else None
            turn["error"] = str(reason) if reason else None
            active_turn = False
            current_step = 0
            after_tool_result = False
            continue
        if entry_type == "event_msg" and payload_type == "user_message":
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
                record_id=f"user-{line_number}",
                input_detail=text or None,
                metadata_detail={
                    "images": len(payload.get("images") or []),
                    "audio": len(payload.get("audio") or []),
                },
            )
            turn["steps"] = max(turn["steps"], current_step)
            continue
        if entry_type == "event_msg" and payload_type == "token_count":
            info = payload.get("info")
            if isinstance(info, dict):
                usage = info.get("total_token_usage")
                last_usage = info.get("last_token_usage")
                if isinstance(usage, dict):
                    latest_usage = usage
                window = info.get("model_context_window")
                if isinstance(window, int):
                    context_window = window
                if (
                    isinstance(last_usage, dict)
                    and last_model_record is not None
                    and last_model_record["turn"] == current_turn
                ):
                    last_model_record["usage"] = last_usage
            continue
        if entry_type == "response_item" and payload_type == "reasoning":
            text = reasoning_summary(payload.get("summary"))
            step, _ = model_step(timestamp)
            last_model_record = add_record(
                timestamp=timestamp,
                kind="reasoning",
                event="Reasoning",
                summary=text or "Encrypted reasoning (summary unavailable)",
                step=step,
                record_id=str(payload.get("id") or f"reasoning-{line_number}"),
                output_detail=text or None,
                metadata_detail={"encrypted": bool(payload.get("encrypted_content"))},
            )
            continue
        if entry_type == "response_item" and payload_type == "message":
            role = payload.get("role")
            if role != "assistant":
                continue
            text = content_text(payload.get("content"))
            step, _ = model_step(timestamp)
            phase = payload.get("phase")
            label = "Assistant" + (f" · {phase}" if isinstance(phase, str) else "")
            last_model_record = add_record(
                timestamp=timestamp,
                kind="assistant",
                event=label,
                summary=text or label,
                step=step,
                record_id=str(payload.get("id") or f"assistant-{line_number}"),
                output_detail=text or None,
                metadata_detail={"phase": phase} if phase else None,
            )
            continue
        if entry_type == "response_item" and payload_type in {
            "function_call",
            "custom_tool_call",
        }:
            step, _ = model_step(timestamp)
            name = payload.get("name")
            namespace = payload.get("namespace")
            if isinstance(namespace, str) and namespace:
                name = f"{namespace}.{name}"
            tool_name = str(name or "tool")
            arguments = payload.get("arguments", payload.get("input"))
            call_id = str(payload.get("call_id") or payload.get("id") or line_number)
            add_record(
                timestamp=timestamp,
                kind="tool",
                event=tool_name,
                summary=f"{tool_name} · running",
                step=step,
                record_id=f"tool-{call_id}",
                input_detail=json_text(arguments) if arguments is not None else None,
                status="running",
                call_id=call_id,
                metadata_detail={"protocolType": payload_type},
            )
            continue
        if entry_type == "response_item" and payload_type in {
            "function_call_output",
            "custom_tool_call_output",
        }:
            call_id = str(payload.get("call_id") or "")
            output = payload.get("output")
            completed_record = visible_calls.get(call_id)
            if completed_record is None:
                if call_id in tool_failures:
                    if output_is_error(output):
                        mark_call_error(call_id)
                    after_tool_result = True
                    continue
                else:
                    completed_record = add_record(
                        timestamp=timestamp,
                        kind="tool",
                        event="Tool result",
                        summary="Tool result without a recorded call",
                        step=current_step or None,
                        record_id=f"tool-result-{call_id or line_number}",
                        call_id=call_id or None,
                    )
            completed_at = timestamp
            started_at = parse_timestamp(completed_record.get("startedAt"))
            is_error = completed_record["status"] == "error" or output_is_error(output)
            completed_record["completedAt"] = iso_timestamp(completed_at)
            completed_record["durationMs"] = (
                max(0, completed_at - started_at)
                if completed_at is not None and started_at is not None
                else None
            )
            if is_error:
                mark_tool_error(completed_record, "Tool result reported an error.")
            else:
                completed_record["status"] = "complete"
            completed_record["output"] = json_text(output) if include_details else None
            completed_record["summary"] = shorten(
                f"{completed_record['event']} · {'error' if is_error else 'complete'}"
            )
            after_tool_result = True
            continue
        if entry_type == "event_msg" and payload_type in {
            "mcp_tool_call_end",
            "patch_apply_end",
            "web_search_end",
        }:
            call_id = str(payload.get("call_id") or "")
            completion_record = visible_calls.get(call_id)
            completion_failed = payload.get("success") is False or output_is_error(
                payload.get("result")
            )
            if completion_failed:
                mark_call_error(call_id)
            if completion_record is not None:
                if completion_failed:
                    mark_tool_error(
                        completion_record,
                        "Tool completion event reported an error.",
                    )
                duration = payload.get("duration")
                if isinstance(duration, (int, float)):
                    completion_record["durationMs"] = round(
                        duration * 1000 if duration < 10_000 else duration
                    )
                if (
                    include_details
                    and completion_record.get("output") is None
                    and payload.get("result") is not None
                ):
                    completion_record["output"] = json_text(payload.get("result"))
            continue
        if entry_type == "event_msg" and payload_type == "sub_agent_activity":
            activity = str(payload.get("kind") or "activity")
            agent_path = str(payload.get("agent_path") or "subagent")
            add_record(
                timestamp=parse_timestamp(payload.get("occurred_at_ms")) or timestamp,
                kind="subagent",
                event=f"Subagent · {activity}",
                summary=f"{agent_path} · {activity}",
                step=current_step or None,
                record_id=f"subagent-{payload.get('event_id') or line_number}",
                metadata_detail={
                    "agentPath": agent_path,
                    "agentThreadId": payload.get("agent_thread_id"),
                    "activity": activity,
                },
            )
            continue
        if entry_type == "compacted" or (
            entry_type == "event_msg" and payload_type == "context_compacted"
        ):
            details = payload if entry_type == "compacted" else {"type": payload_type}
            add_record(
                timestamp=timestamp,
                kind="compaction",
                event="Compaction",
                summary="Context compacted",
                step=current_step or None,
                record_id=f"compaction-{line_number}",
                output_detail=json_text(details),
            )
            continue
        if entry_type == "response_item" and payload_type == "agent_message":
            text = content_text(payload.get("content"))
            add_record(
                timestamp=timestamp,
                kind="subagent",
                event="Agent message",
                summary=text or "Inter-agent message",
                step=current_step or None,
                record_id=str(payload.get("id") or f"agent-message-{line_number}"),
                output_detail=text or None,
                metadata_detail={
                    "author": payload.get("author"),
                    "recipient": payload.get("recipient"),
                },
            )

    if active_turn and turns:
        turn = turns[-1]
        turn["status"] = "running"
        if last_time is not None and turn.get("startedAt"):
            started = parse_timestamp(turn["startedAt"])
            if started is not None:
                turn["durationMs"] = max(0, last_time - started)

    for record in records:
        if record["status"] == "running":
            record["summary"] = shorten(f"{record['event']} · running")

    omitted = max(0, all_record_count - len(records))
    visible_records = list(records)
    failed_tools = sum(tool_failures.values())
    session_id = metadata.get("session_id") or metadata.get("id") or path.stem
    git = safe_git(metadata.get("git"))
    model = context.get("model") if isinstance(context.get("model"), str) else None
    effort = context.get("effort") if isinstance(context.get("effort"), str) else None
    session = {
        "id": str(session_id),
        "title": shorten(first_user or "Untitled Codex task", 120),
        "cwd": display_path(metadata.get("cwd")),
        "model": model,
        "effort": effort,
        "originator": metadata.get("originator"),
        "sourceKind": metadata.get("source"),
        "startedAt": iso_timestamp(first_time),
        "updatedAt": iso_timestamp(last_time),
        "archived": "archived_sessions" in path.parts,
        "parentThreadId": metadata.get("parent_thread_id"),
        "agentPath": metadata.get("agent_path"),
        "git": git,
    }
    return {
        "schemaVersion": 1,
        "detailLevel": detail_level,
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "session": session,
        "stats": {
            "turns": len(turns),
            "records": all_record_count,
            "visibleRecords": len(visible_records),
            "omittedRecords": omitted,
            "toolCalls": tool_calls,
            "failedTools": failed_tools,
            "compactions": compactions,
            "tokens": latest_usage,
            "contextWindow": context_window,
        },
        "turns": turns,
        "records": visible_records,
        "warnings": warnings,
    }


def trajectory_result(arguments: dict[str, Any], with_ui: bool) -> dict[str, Any]:
    """Resolve and project a session for one MCP tool call."""
    allowed = {"sessionId", "maxRecords", "includeArchived", "detailLevel"}
    reject_unknown_arguments(arguments, allowed)
    include_archived = arguments.get("includeArchived", True)
    if not isinstance(include_archived, bool):
        raise ValueError("includeArchived must be a boolean.")
    session_id = arguments.get("sessionId")
    if session_id is not None and not isinstance(session_id, str):
        raise ValueError("sessionId must be a string.")
    requested_max = arguments.get("maxRecords", DEFAULT_MAX_RECORDS)
    if isinstance(requested_max, bool) or not isinstance(requested_max, int):
        raise ValueError("maxRecords must be an integer.")
    if not 50 <= requested_max <= MAX_RECORDS:
        raise ValueError(f"maxRecords must be between 50 and {MAX_RECORDS}.")
    detail_level = normalize_detail_level(arguments.get("detailLevel", "summary"))
    path = resolve_session(session_id, include_archived)
    trajectory = parse_session(path, requested_max, detail_level)
    trajectory["recentSessions"] = list_session_overviews(
        limit=20, include_archived=include_archived
    )
    stats = trajectory["stats"]
    summary = (
        f"Trajectory for {trajectory['session']['id']}: "
        f"{stats['turns']} turns, {stats['records']} records, "
        f"{stats['toolCalls']} tool calls, {stats['failedTools']} failed tools."
    )
    result: dict[str, Any] = {
        "structuredContent": trajectory,
        "content": [{"type": "text", "text": summary}],
    }
    if with_ui:
        result["_meta"] = {"ui": {"resourceUri": UI_URI}}
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
        "sessionId": {
            "type": "string",
            "description": (
                "Exact or unambiguous-prefix Codex session ID. Omit for the latest task."
            ),
        },
        "maxRecords": {
            "type": "integer",
            "minimum": 50,
            "maximum": MAX_RECORDS,
            "default": DEFAULT_MAX_RECORDS,
            "description": (
                "Maximum tail records returned while preserving stable original indexes."
            ),
        },
        "includeArchived": {
            "type": "boolean",
            "default": True,
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
            "description": "List recent local Codex task logs without returning transcript bodies.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                    "query": {
                        "type": "string",
                        "description": "Filter by ID, title, cwd, or model.",
                    },
                    "includeArchived": {"type": "boolean", "default": False},
                },
                "additionalProperties": False,
            },
            "annotations": read_only,
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
    ]


def call_tool(name: str, arguments: Any) -> dict[str, Any]:
    """Dispatch one MCP tool call."""
    args = arguments if isinstance(arguments, dict) else {}
    try:
        if name == "list_codex_sessions":
            reject_unknown_arguments(args, {"limit", "query", "includeArchived"})
            limit = args.get("limit", 20)
            if isinstance(limit, bool) or not isinstance(limit, int):
                raise ValueError("limit must be an integer.")
            if not 1 <= limit <= 100:
                raise ValueError("limit must be between 1 and 100.")
            query = args.get("query", "")
            if not isinstance(query, str):
                raise ValueError("query must be a string.")
            include_archived = args.get("includeArchived", False)
            if not isinstance(include_archived, bool):
                raise ValueError("includeArchived must be a boolean.")
            sessions = list_session_overviews(
                limit=limit,
                query=query,
                include_archived=include_archived,
            )
            return {
                "structuredContent": {"sessions": sessions, "count": len(sessions)},
                "content": [{"type": "text", "text": f"Found {len(sessions)} local Codex tasks."}],
            }
        if name == "get_codex_trajectory":
            return trajectory_result(args, with_ui=False)
        if name == "show_codex_trajectory":
            return trajectory_result(args, with_ui=True)
        raise ValueError(f"Unknown tool {name!r}.")
    except (OSError, ValueError) as error:
        return {
            "isError": True,
            "content": [{"type": "text", "text": str(error)}],
        }


def reject_unknown_arguments(arguments: dict[str, Any], allowed: set[str]) -> None:
    """Reject tool fields that are not declared by the public input schema."""
    unknown = sorted(set(arguments) - allowed)
    if unknown:
        raise ValueError(f"Unknown argument(s): {', '.join(unknown)}.")


def ui_html() -> str:
    """Read the bundled, dependency-free trajectory component."""
    path = Path(__file__).resolve().parent.parent.parent / "assets" / "trajectory.html"
    return path.read_text(encoding="utf-8")

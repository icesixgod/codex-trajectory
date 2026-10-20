"""Current-task timing from a complete safe projection, never a visible page."""

from __future__ import annotations

import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Iterable
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import projection
from .page_index import PageIndex
from .privacy import display_path
from .sessions import (
    ReadLimitExceeded,
    first_session_metadata,
    jsonl_byte_limit,
    session_read_limit,
    session_scan,
    session_signature,
)

Interval = tuple[int, int]
MAX_TIMING_CACHE = 256
_TIMING_CACHE: OrderedDict[Any, dict[str, Any]] = OrderedDict()
_TIMING_CACHE_LOCK = threading.Lock()
TIMING_FIELDS = (
    "totalMs",
    "localMs",
    "modelMs",
    "modelRecordedMs",
    "modelEstimatedMs",
    "unattributedMs",
    "turns",
    "timedTurns",
    "untimedTools",
    "missingTimingTools",
    "invalidTimingTools",
    "zeroDurationTools",
    "submillisecondTools",
)


def merge_intervals(values: Iterable[Interval]) -> list[Interval]:
    """Union overlapping execution, including nested/parallel tool calls."""
    result: list[Interval] = []
    for start, end in sorted(values):
        if end <= start:
            continue
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(result[-1][1], end))
        else:
            result.append((start, end))
    return result


def intersect_intervals(left: list[Interval], right: list[Interval]) -> list[Interval]:
    """Intersect two sorted unions without multiplying their sizes."""
    result: list[Interval] = []
    i = j = 0
    while i < len(left) and j < len(right):
        start = max(left[i][0], right[j][0])
        end = min(left[i][1], right[j][1])
        if end > start:
            result.append((start, end))
        if left[i][1] < right[j][1]:
            i += 1
        else:
            j += 1
    return result


def subtract_intervals(left: list[Interval], right: list[Interval]) -> list[Interval]:
    """Remove a sorted union from another sorted union."""
    result: list[Interval] = []
    j = 0
    for start, end in left:
        while j < len(right) and right[j][1] <= start:
            j += 1
        k = j
        while k < len(right) and right[k][0] < end:
            if right[k][0] > start:
                result.append((start, right[k][0]))
            start = max(start, right[k][1])
            if start >= end:
                break
            k += 1
        if start < end:
            result.append((start, end))
    return result


def _interval(value: dict[str, Any]) -> Interval | None:
    duration = value.get("durationMs")
    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 1:
        return None
    end = projection.parse_timestamp(value.get("completedAt"))
    start = projection.parse_timestamp(value.get("startedAt"))
    # Explicit durations take precedence over different wall-clock boundaries.
    if end is not None:
        return end - duration, end
    if start is not None:
        return start, start + duration
    return None


def _milliseconds(values: Iterable[Interval]) -> int:
    return sum(end - start for start, end in values)


def _unknown_tool_interval(
    record: dict[str, Any], envelope: Interval, anchor: int
) -> list[Interval]:
    """Confine uncertainty to observed call bounds, or a conservative model-step window."""
    start = projection.parse_timestamp(record.get("startedAt"))
    end = projection.parse_timestamp(record.get("completedAt"))
    # Old projections lack the private flag; equal timestamps alone are not a call boundary.
    has_start = record.get("_hasStartBoundary", start != end or record.get("status") == "running")
    if not has_start or start is None:
        start = anchor if end is None or anchor <= end else envelope[0]
    if end is None:
        end = envelope[1]
    if end < start:
        # Conflicting clocks do not justify silently treating the tool as instantaneous.
        return [envelope]
    return intersect_intervals([(start, end)], [envelope])


def summarize_timing(
    turns: Iterable[dict[str, Any]], records: Iterable[dict[str, Any]]
) -> dict[str, int]:
    """Partition active turns while limiting missing tool timing to its possible interval."""
    by_turn = {turn["index"]: turn for turn in turns}
    envelopes = {index: value for index, turn in by_turn.items() if (value := _interval(turn))}
    local: list[Interval] = []
    recorded: list[Interval] = []
    other: list[Interval] = []
    model_turns = {index for index, turn in by_turn.items() if turn.get("modelCalls", 0) > 0}
    tool_statuses: dict[tuple[int, str], str] = {}
    uncertain: dict[tuple[int, str], list[Interval]] = {}
    model_anchors: dict[int, int] = {}
    model_phases: dict[int, int] = {}
    batch_anchors: dict[tuple[int, int | None, int], int] = {}
    priority = {"measured": 0, "submillisecond": 1, "zero": 2, "invalid": 3, "missing": 4}
    for record in records:
        turn = record["turn"]
        kind = record["kind"]
        value = _interval(record)
        if kind == "tool":
            key = (turn, record.get("callId") or f"record-{record['index']}")
            status = "measured" if value is not None else record.get("_timingStatus", "missing")
            if status not in priority or (status == "measured" and value is None):
                status = "missing"
            previous = tool_statuses.get(key, "missing")
            tool_statuses[key] = min((previous, status), key=priority.__getitem__)
            if turn in envelopes:
                step = record.get("step")
                # Parallel calls in the same model step share an anchor. A previous
                # tool's completion cannot establish the next parallel call's start.
                batch = (turn, step, 0 if step is not None else model_phases.get(turn, 0))
                anchor = batch_anchors.setdefault(
                    batch, model_anchors.get(turn, envelopes[turn][0])
                )
                if status in {"missing", "invalid"}:
                    uncertain.setdefault(key, []).extend(
                        _unknown_tool_interval(record, envelopes[turn], anchor)
                    )
        if kind in {"assistant", "reasoning"}:
            model_turns.add(turn)
            boundary = projection.parse_timestamp(record.get("completedAt"))
            if boundary is None:
                boundary = projection.parse_timestamp(record.get("startedAt"))
            if boundary is not None:
                model_anchors[turn] = boundary
            model_phases[turn] = model_phases.get(turn, 0) + 1
        if value is None or turn not in envelopes:
            continue
        clipped = intersect_intervals([value], [envelopes[turn]])
        if kind == "tool":
            local.extend(clipped)
        elif kind in {"assistant", "reasoning"}:
            recorded.extend(clipped)
        else:
            other.extend(clipped)

    active = merge_intervals(envelopes.values())
    local = merge_intervals(local)
    recorded = subtract_intervals(merge_intervals(recorded), local)
    unknown = merge_intervals(
        interval
        for key, values in uncertain.items()
        if tool_statuses[key] in {"missing", "invalid"}
        for interval in values
    )
    blocked = merge_intervals([*local, *recorded, *other, *unknown])
    eligible = merge_intervals(value for index, value in envelopes.items() if index in model_turns)
    estimated = subtract_intervals(eligible, blocked)
    total_ms = _milliseconds(active)
    local_ms = _milliseconds(local)
    recorded_ms = _milliseconds(recorded)
    estimated_ms = _milliseconds(estimated)
    return {
        "totalMs": total_ms,
        "localMs": local_ms,
        "modelMs": recorded_ms + estimated_ms,
        "modelRecordedMs": recorded_ms,
        "modelEstimatedMs": estimated_ms,
        "unattributedMs": total_ms - local_ms - recorded_ms - estimated_ms,
        "turns": len(by_turn),
        "timedTurns": len(envelopes),
        "untimedTools": sum(status in {"missing", "invalid"} for status in tool_statuses.values()),
        "missingTimingTools": sum(status == "missing" for status in tool_statuses.values()),
        "invalidTimingTools": sum(status == "invalid" for status in tool_statuses.values()),
        "zeroDurationTools": sum(status == "zero" for status in tool_statuses.values()),
        "submillisecondTools": sum(status == "submillisecond" for status in tool_statuses.values()),
    }


def _session_timing(path: Path) -> dict[str, Any]:
    signature = session_signature(path)
    key = (path, signature, jsonl_byte_limit())
    with _TIMING_CACHE_LOCK:
        cached = _TIMING_CACHE.get(key)
        if cached is not None:
            _TIMING_CACHE.move_to_end(key)
            return deepcopy(cached)
    result = projection.session_timing_summary(path)
    if session_signature(path) == signature:
        with _TIMING_CACHE_LOCK:
            # Remove obsolete revisions of this task before enforcing the bound.
            for old in list(_TIMING_CACHE):
                if old[0] == path:
                    del _TIMING_CACHE[old]
            _TIMING_CACHE[key] = deepcopy(result)
            while len(_TIMING_CACHE) > MAX_TIMING_CACHE:
                _TIMING_CACHE.popitem(last=False)
    return result


@session_scan()
def project_timing_result(arguments: dict[str, Any]) -> dict[str, Any]:
    """Read only the selected task; retain the original tool name for compatibility."""
    projection.reject_unknown_arguments(arguments, {"sessionId", "includeArchived", "maxReadBytes"})
    session_id = arguments.get("sessionId")
    if not isinstance(session_id, str) or not session_id.strip() or session_id.strip() == "latest":
        raise ValueError("Task timing requires an explicit sessionId.")
    include_archived = arguments.get("includeArchived", True)
    if not isinstance(include_archived, bool):
        raise ValueError("includeArchived must be a boolean.")
    # Older servers may load this module from updated files after projection was
    # already imported. Do not mix the new timing reader with an older runtime.
    if not all(
        callable(capability)
        for capability in (
            getattr(projection, "resolve_timing_session", None),
            getattr(projection, "session_timing_summary", None),
            getattr(PageIndex, "timing_records", None),
        )
    ):
        return {
            "isError": True,
            "structuredContent": {"viewerState": "plugin-reload-required"},
            "content": [
                {
                    "type": "text",
                    "text": "The plugin runtime is out of date. "
                    "Fully quit and reopen Codex, then retry.",
                }
            ],
        }
    with session_read_limit(arguments.get("maxReadBytes")):
        selected_path = projection.resolve_timing_session(session_id, include_archived)
        metadata = first_session_metadata(selected_path)
        selected_id = projection.metadata_identity(metadata, selected_path)
        try:
            row = _session_timing(selected_path)
        except ReadLimitExceeded as error:
            error.session_id = selected_id
            error.session_id_verified = True
            raise
        except sqlite3.Error as error:
            raise ValueError("Could not build the selected task's timing summary.") from error
        if row["id"] != selected_id:
            raise ValueError("Selected task changed during timing read; retry.")
    cwd = display_path(row.pop("cwd", None))
    result = {
        "schemaVersion": 1,
        "scope": "session",
        "generatedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "project": {"cwd": cwd, "match": "selected-session"},
        "selectedSessionId": selected_id,
        "includeArchived": include_archived,
        "aggregation": "task-active-time",
        "timing": {field: row[field] for field in TIMING_FIELDS},
        "sessionCount": 1,
        "skippedSessions": 0,
        "unreadableMetadata": 0,
        "omittedSessions": 0,
        "sessions": [row],
    }
    return {
        "structuredContent": result,
        "content": [{"type": "text", "text": f"Timing for selected Codex task {selected_id}."}],
    }

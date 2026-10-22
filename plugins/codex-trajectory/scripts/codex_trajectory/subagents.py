"""Read-only task usage across explicitly related local agent sessions."""

from __future__ import annotations

import json
import threading
from collections import OrderedDict, defaultdict, deque
from contextlib import nullcontext
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import sessions
from .pricing import merge_cost_estimates

MAX_CACHED_USAGE = 256
MAX_USAGE_CACHE_BYTES = 8 * 1024 * 1024
MAX_CACHED_LINKS = 10_000
MAX_TASK_AGENTS = sessions.MAX_DISCOVERED_SESSION_FILES
Signature = tuple[tuple[str, int, int, int], ...]
_USAGE: OrderedDict[Any, tuple[Signature, dict[str, Any]]] = OrderedDict()
_USAGE_SIZES: dict[Any, int] = {}
_LINKS: OrderedDict[Any, tuple[tuple[int, ...], SessionLink]] = OrderedDict()
_LOCK = threading.Lock()


@dataclass(frozen=True)
class SessionLink:
    """Keep only identities, never raw session metadata, in discovery caches."""

    session_id: str | None
    parent_id: str | None


@dataclass
class TaskUsage:
    members: list[dict[str, Any]]
    signature: list[Any]
    unavailable: int = 0
    read_warnings: int = 0


def _identifier(value: Any) -> str | None:
    if (
        isinstance(value, str)
        and 0 < len(value) <= 240
        and value == value.strip()
        and not any(char in value for char in ("/", "\\", "\x00"))
    ):
        return value
    return None


def referenced_agents(entry: dict[str, Any]) -> set[str]:
    """Use structured activity IDs, never tool output or arbitrary message text."""
    payload = entry.get("payload")
    if entry.get("type") != "event_msg" or not isinstance(payload, dict):
        return set()
    if payload.get("type") == "item_completed":
        payload = payload.get("item")
    if not isinstance(payload, dict) or payload.get("type") not in {
        "sub_agent_activity",
        "SubAgentActivity",
    }:
        return set()
    identifier = _identifier(payload.get("agent_thread_id"))
    return {identifier} if identifier is not None else set()


def remember_usage(path: Path, signature: Signature, usage: dict[str, Any]) -> None:
    """Reuse compact own-agent accounting independently of displayed history pages."""
    key = (path, sessions.jsonl_byte_limit())
    size = len(json.dumps(usage, ensure_ascii=True).encode("ascii"))
    with _LOCK:
        _USAGE[key] = (signature, deepcopy(usage))
        _USAGE_SIZES[key] = size
        _USAGE.move_to_end(key)
        while _USAGE and (
            len(_USAGE) > MAX_CACHED_USAGE or sum(_USAGE_SIZES.values()) > MAX_USAGE_CACHE_BYTES
        ):
            old_key, _ = _USAGE.popitem(last=False)
            _USAGE_SIZES.pop(old_key, None)


def _own_usage(path: Path, *, prime_index: bool = False) -> tuple[Signature, dict[str, Any]]:
    from . import projection

    signature = sessions.session_signature(path)
    key = (path, sessions.jsonl_byte_limit())
    with _LOCK:
        cached = _USAGE.get(key)
        if cached is not None and cached[0] == signature:
            _USAGE.move_to_end(key)
            return signature, deepcopy(cached[1])
    # Prime the selected task's normal index so its first revision read does not
    # cause an extra full scan. Children need only a bounded streaming summary.
    if prime_index:
        projection._cached_session_trajectory(path, projection.MIN_RECORDS)
        with _LOCK:
            cached = _USAGE.get(key)
            if cached is not None and cached[0] == signature:
                return signature, deepcopy(cached[1])
    usage: dict[str, Any] = {}
    projection.parse_session(path, projection.MIN_RECORDS, _usage=usage)
    if sessions.session_signature(path) == signature:
        remember_usage(path, signature, usage)
    return signature, usage


def _session_link(path: Path, *, related: bool = False) -> SessionLink:
    stat = sessions._safe_file_stat(path)
    signature = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns)
    # A larger selected-task budget never expands unrelated metadata probes.
    with nullcontext() if related else sessions.session_read_limit():
        key = (path, sessions.jsonl_byte_limit())
        with _LOCK:
            cached = _LINKS.get(key)
            if cached is not None and cached[0] == signature:
                _LINKS.move_to_end(key)
                return cached[1]
        metadata = sessions.first_session_metadata(path)
    identifier = _identifier(metadata.get("id")) or _identifier(metadata.get("session_id"))
    parent = _identifier(metadata.get("parent_thread_id"))
    source = metadata.get("source")
    if isinstance(source, dict):
        subagent = source.get("subagent")
        if isinstance(subagent, dict):
            spawn = subagent.get("thread_spawn", subagent)
            if isinstance(spawn, dict):
                source_parent = _identifier(spawn.get("parent_thread_id"))
                if parent is not None and source_parent not in (None, parent):
                    raise ValueError("Conflicting subagent parent identities.")
                parent = parent or source_parent
    result = SessionLink(identifier, parent)
    with _LOCK:
        _LINKS[key] = (signature, result)
        _LINKS.move_to_end(key)
        while len(_LINKS) > MAX_CACHED_LINKS:
            _LINKS.popitem(last=False)
    return result


@sessions.session_scan()
def task_usage(path: Path) -> TaskUsage:
    """Follow metadata and activity links, counting each descendant task ID once."""
    from . import projection

    signature, own = _own_usage(path, prime_index=True)
    identifier = projection.metadata_identity(sessions.first_session_metadata(path), path)
    result = TaskUsage([own], [(identifier, signature)])
    # Standalone parser callers may use synthetic files outside Codex's roots.
    if not any(sessions.is_safe_session_file(path, root) for root in sessions.session_roots(True)):
        return result
    by_id: dict[str, tuple[Path, SessionLink]] = {}
    by_parent: dict[str, set[str]] = defaultdict(set)
    deferred: dict[str, Path] = {}
    referenced = {identifier, *own["children"]}
    for candidate in sessions.session_files(True):
        try:
            named_id = sessions.thread_id_from_path(candidate)
            link = _session_link(candidate, related=candidate == path or named_id in referenced)
        except sessions.ReadLimitExceeded:
            if named_id is not None:
                deferred.setdefault(named_id, candidate)
            continue
        except (OSError, RuntimeError, ValueError):
            continue
        if link.session_id is not None and link.session_id not in by_id:
            # Discovery orders newest rollouts first; resumes and archive copies
            # of the same logical agent must never be added a second time.
            by_id[link.session_id] = (candidate, link)
            if link.parent_id is not None:
                by_parent[link.parent_id].add(link.session_id)
    seen = {identifier}
    pending = deque([(identifier, own)])
    while pending:
        parent, parent_usage = pending.popleft()
        children = by_parent.get(parent, set()) | set(parent_usage["children"])
        for child in sorted(children):
            if child in seen:
                continue
            candidate_link = by_id.get(child)
            if candidate_link is None and child in deferred:
                candidate = deferred[child]
                # A structured exact reference can authorize a larger metadata
                # probe for this child, without expanding any other task's budget.
                link = _session_link(candidate, related=True)
                if link.session_id == child:
                    candidate_link = (candidate, link)
            if candidate_link is not None and candidate_link[1].parent_id not in (None, parent):
                # Root activity can mention a grandchild. Its immediate parent's
                # metadata edge owns it; a notification cannot reparent a task.
                continue
            seen.add(child)
            if len(seen) > MAX_TASK_AGENTS:
                raise ValueError("Task subagents exceed the supported discovery limit.")
            if candidate_link is None:
                result.unavailable += 1
                result.signature.append((child, "unavailable"))
                pending.append((child, {"children": ()}))
                continue
            candidate = candidate_link[0]
            try:
                child_signature, usage = _own_usage(candidate)
            except sessions.ReadLimitExceeded:
                raise
            except (OSError, RuntimeError, ValueError):
                result.unavailable += 1
                try:
                    stat = sessions._safe_file_stat(candidate)
                    result.signature.append(
                        (child, "unreadable", stat.st_mtime_ns, stat.st_size, stat.st_ctime_ns)
                    )
                except OSError:
                    result.signature.append((child, "unavailable"))
                pending.append((child, {"children": ()}))
                continue
            result.members.append(usage)
            result.signature.append((child, child_signature))
            result.read_warnings += bool(usage["readWarnings"])
            pending.append((child, usage))
    return result


def _with_total(usage: dict[str, Any] | None) -> dict[str, Any] | None:
    """A missing total in one agent must not disappear into another agent's total."""
    if usage is None or "total_tokens" in usage:
        return usage
    if "input_tokens" not in usage and "output_tokens" not in usage:
        return usage
    total = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
    if total > 2**53 - 1:
        raise ValueError("Task token totals exceed the supported integer limit.")
    return {**usage, "total_tokens": total}


def aggregate_usage(path: Path, trajectory: dict[str, Any]) -> dict[str, Any]:
    """Replace task totals only; each agent's records and turn totals stay its own."""
    from . import projection

    family = task_usage(path)
    stats = trajectory["stats"]
    stats["tokens"] = _with_total(stats["tokens"])
    fast = False
    for usage in family.members[1:]:
        child_tokens = _with_total(usage["tokens"])
        if child_tokens is not None:
            for key, value in child_tokens.items():
                if value + (stats["tokens"] or {}).get(key, 0) > projection.MAX_SAFE_INTEGER:
                    raise ValueError("Task token totals exceed the supported integer limit.")
            stats["tokens"] = projection.merge_token_usage(stats["tokens"], child_tokens)
        if usage["cost"] is not None:
            stats["cost"] = merge_cost_estimates(stats["cost"], usage["cost"])
        fast |= usage["fast"]
    warnings = trajectory["warnings"]
    notices: list[dict[str, Any]] = []
    if family.unavailable or family.read_warnings:
        projection.add_warning(
            notices,
            "subagent_usage_incomplete",
            1,
            "Task totals include available subagent usage only: "
            f"{family.unavailable} subagent log(s) unavailable and "
            f"{family.read_warnings} subagent log(s) with read warnings.",
        )
    if fast and not any(warning["code"] == "codex_fast_cost_multiplier" for warning in warnings):
        projection.add_warning(
            notices,
            "codex_fast_cost_multiplier",
            1,
            "Logged subagent Fast x2.5 included-usage estimates are included in task costs; "
            "this is not an API invoice or purchased-credit bill.",
        )
    trajectory["warnings"] = (notices + warnings)[: projection.MAX_WARNINGS]
    return trajectory

"""Safe discovery and reading of local Codex rollout logs."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .json_support import strict_json_loads

JsonEntry = tuple[int, dict[str, Any]]
MAX_DIAGNOSTICS = 100
MAX_JSONL_LINE_BYTES = 16 * 1024 * 1024
MAX_LINEAGE_SEGMENTS = 1_024
_UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


@dataclass(frozen=True)
class RolloutSegment:
    """One ordinal-bounded file segment in a paginated rollout lineage."""

    path: Path
    start_ordinal: int
    end_ordinal_exclusive: int | None = None
    end_byte_offset: int | None = None


@dataclass
class _JsonlReadState:
    """Internal evidence about complete rejected lines since the last valid object."""

    pending_rejected_lines: int = 0


def _add_diagnostic(diagnostics: list[dict[str, Any]], value: dict[str, Any]) -> None:
    if len(diagnostics) < MAX_DIAGNOSTICS:
        diagnostics.append(value)


def codex_home() -> Path:
    """Return the configured Codex data directory."""
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def session_roots(include_archived: bool) -> list[Path]:
    """Return canonical roots authorized for session reads."""
    roots = [codex_home() / "sessions"]
    if include_archived:
        roots.append(codex_home() / "archived_sessions")
    return roots


def is_archived_session(path: Path) -> bool:
    """Return whether a discovered rollout is inside the configured archive root."""
    try:
        archived_root = (codex_home() / "archived_sessions").resolve(strict=False)
        return path.resolve(strict=True).is_relative_to(archived_root)
    except (OSError, RuntimeError):
        return False


def is_safe_session_file(path: Path, root: Path) -> bool:
    """Check that a regular non-symlink file remains under its session root."""
    try:
        relative = path.relative_to(root)
        current = root
        for part in relative.parts:
            current /= part
            if current.is_symlink():
                return False
        file_stat = path.stat(follow_symlinks=False)
        return (
            stat.S_ISREG(file_stat.st_mode)
            and file_stat.st_nlink == 1
            and path.resolve(strict=True).is_relative_to(root.resolve(strict=True))
        )
    except (OSError, RuntimeError, ValueError):
        return False


def session_files(include_archived: bool) -> list[Path]:
    """Discover authorized rollout logs ordered by last modification."""
    paths: list[Path] = []
    for root in session_roots(include_archived):
        if not root.is_dir():
            continue
        paths.extend(
            sorted(
                (path for path in root.rglob("*.jsonl") if is_safe_session_file(path, root)),
                key=lambda path: path.as_posix(),
            )
        )

    def modified(path: Path) -> int:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return 0

    return sorted(paths, key=modified, reverse=True)


def iter_jsonl(
    path: Path,
    warnings: list[dict[str, Any]] | None = None,
    *,
    end_byte_offset: int | None = None,
    _state: _JsonlReadState | None = None,
) -> Iterator[JsonEntry]:
    """Yield valid JSON objects up to an optional complete-line byte boundary."""
    diagnostics = warnings if warnings is not None else []
    if end_byte_offset is not None:
        if isinstance(end_byte_offset, bool) or end_byte_offset < 0:
            raise ValueError("Invalid paginated history byte boundary.")
        try:
            file_size = path.stat().st_size
        except OSError:
            raise
        if end_byte_offset > file_size:
            raise ValueError("Paginated history byte boundary is past the source rollout.")
    with path.open("rb") as handle:
        line_number = 0
        while True:
            line_start = handle.tell()
            if end_byte_offset is not None and line_start >= end_byte_offset:
                break
            encoded_line = handle.readline(MAX_JSONL_LINE_BYTES + 1)
            if not encoded_line:
                break
            line_number += 1
            if len(encoded_line) > MAX_JSONL_LINE_BYTES:
                while encoded_line and not encoded_line.endswith(b"\n"):
                    if end_byte_offset is not None and handle.tell() >= end_byte_offset:
                        break
                    encoded_line = handle.readline(MAX_JSONL_LINE_BYTES + 1)
                line_end = handle.tell()
                if end_byte_offset is not None and line_end > end_byte_offset:
                    raise ValueError("Paginated history byte boundary splits a JSONL record.")
                complete = encoded_line.endswith(b"\n")
                if end_byte_offset is not None and line_end == end_byte_offset and not complete:
                    raise ValueError(
                        "Paginated history byte boundary omits the terminating newline."
                    )
                if complete:
                    if _state is not None:
                        _state.pending_rejected_lines += 1
                    _add_diagnostic(
                        diagnostics,
                        {
                            "code": "oversized_jsonl",
                            "line": line_number,
                            "message": (
                                f"Skipped JSONL line {line_number} because it exceeds "
                                f"{MAX_JSONL_LINE_BYTES} bytes."
                            ),
                        },
                    )
                continue
            line_end = handle.tell()
            if end_byte_offset is not None and line_end > end_byte_offset:
                raise ValueError("Paginated history byte boundary splits a JSONL record.")
            complete = encoded_line.endswith(b"\n")
            if end_byte_offset is not None and line_end == end_byte_offset and not complete:
                raise ValueError("Paginated history byte boundary omits the terminating newline.")
            if not complete:
                continue
            if not encoded_line.strip():
                continue
            try:
                line = encoded_line.decode("utf-8")
            except UnicodeDecodeError:
                if complete:
                    if _state is not None:
                        _state.pending_rejected_lines += 1
                    _add_diagnostic(
                        diagnostics,
                        {
                            "code": "malformed_utf8",
                            "line": line_number,
                            "message": f"Skipped malformed UTF-8 JSONL line {line_number}.",
                        },
                    )
                continue
            try:
                value = strict_json_loads(line)
            except (ValueError, RecursionError) as error:
                if complete:
                    if _state is not None:
                        _state.pending_rejected_lines += 1
                    message = (
                        error.msg if isinstance(error, json.JSONDecodeError) else "invalid JSON"
                    )
                    _add_diagnostic(
                        diagnostics,
                        {
                            "code": "malformed_jsonl",
                            "line": line_number,
                            "message": (f"Skipped malformed JSONL line {line_number}: {message}."),
                        },
                    )
                continue
            if isinstance(value, dict):
                yield line_number, value
            else:
                if _state is not None:
                    _state.pending_rejected_lines += 1
                _add_diagnostic(
                    diagnostics,
                    {
                        "code": "non_object_jsonl",
                        "line": line_number,
                        "message": f"Skipped non-object JSONL line {line_number}.",
                    },
                )


def first_session_metadata(path: Path) -> dict[str, Any]:
    """Return the target rollout's first metadata record, never an inherited copy."""
    for _, entry in iter_jsonl(path):
        if entry.get("type") != "session_meta":
            continue
        payload = entry.get("payload")
        return payload if isinstance(payload, dict) else {}
    return {}


def _contains_paginated_records(path: Path) -> bool:
    """Detect a paginated wire shape even when its required metadata is damaged."""
    for _, entry in iter_jsonl(path):
        return "ordinal" in entry
    return False


def rollout_id_from_path(path: Path) -> str | None:
    """Extract the UUID rollout identity carried at the end of canonical filenames."""
    match = _UUID_PATTERN.search(path.stem)
    if match is None or match.end() != len(path.stem):
        return None
    return match.group(0).lower()


def _history_position(value: Any) -> tuple[str, int, int]:
    if not isinstance(value, dict):
        raise ValueError("Paginated history base must be an object.")
    rollout_id = value.get("thread_id")
    end_ordinal = value.get("end_ordinal_exclusive")
    end_byte = value.get("end_byte_offset")
    if not isinstance(rollout_id, str) or _UUID_PATTERN.fullmatch(rollout_id) is None:
        raise ValueError("Paginated history base has an invalid rollout ID.")
    if isinstance(end_ordinal, bool) or not isinstance(end_ordinal, int) or end_ordinal <= 0:
        raise ValueError("Paginated history base has an invalid ordinal boundary.")
    if isinstance(end_byte, bool) or not isinstance(end_byte, int) or end_byte <= 0:
        raise ValueError("Paginated history base has an invalid byte boundary.")
    return rollout_id.lower(), end_ordinal, end_byte


def _rollout_index() -> dict[str, Path | None]:
    """Index authorized physical rollout IDs once for one lineage walk."""
    result: dict[str, Path | None] = {}
    for candidate in session_files(True):
        rollout_id = rollout_id_from_path(candidate)
        if rollout_id is None:
            continue
        if rollout_id in result and result[rollout_id] != candidate:
            result[rollout_id] = None
        else:
            result[rollout_id] = candidate
    return result


def _find_rollout_by_id(rollout_id: str, index: dict[str, Path | None]) -> Path:
    normalized = rollout_id.lower()
    if normalized not in index:
        raise ValueError("Paginated history source rollout was not found.")
    path = index[normalized]
    if path is None:
        raise ValueError("Paginated history source rollout ID is ambiguous.")
    return path


def rollout_lineage(path: Path) -> list[RolloutSegment]:
    """Resolve official ``history_base`` pointers into oldest-first segments."""
    target_metadata = first_session_metadata(path)
    if str(target_metadata.get("history_mode") or "legacy").casefold() != "paginated":
        if _contains_paginated_records(path):
            raise ValueError("Paginated rollout has no valid paginated session metadata.")
        return [RolloutSegment(path=path, start_ordinal=0)]

    segments: list[RolloutSegment] = []
    seen_rollout_ids: set[str] = set()
    rollout_index: dict[str, Path | None] | None = None
    current_path = path
    end_ordinal: int | None = None
    end_byte: int | None = None
    while True:
        if len(segments) >= MAX_LINEAGE_SEGMENTS:
            raise ValueError("Paginated history lineage exceeds the supported segment limit.")
        metadata = first_session_metadata(current_path)
        if str(metadata.get("history_mode") or "").casefold() != "paginated":
            raise ValueError("Paginated history source rollout uses a different history mode.")
        current_rollout_id = rollout_id_from_path(current_path)
        if current_rollout_id is None:
            raise ValueError("Paginated rollout filename does not contain a rollout UUID.")
        metadata_rollout_id = metadata.get("id")
        if (
            not isinstance(metadata_rollout_id, str)
            or _UUID_PATTERN.fullmatch(metadata_rollout_id) is None
            or metadata_rollout_id.casefold() != current_rollout_id
        ):
            raise ValueError("Paginated rollout metadata does not match its filename identity.")
        if current_rollout_id in seen_rollout_ids:
            raise ValueError("Paginated history lineage contains a cycle.")
        seen_rollout_ids.add(current_rollout_id)

        history_base = metadata.get("history_base")
        if history_base is None:
            start_ordinal = 1
        else:
            _, inherited_end_ordinal, _ = _history_position(history_base)
            start_ordinal = inherited_end_ordinal + 1
        segments.append(
            RolloutSegment(
                path=current_path,
                start_ordinal=start_ordinal,
                end_ordinal_exclusive=end_ordinal,
                end_byte_offset=end_byte,
            )
        )
        if history_base is None:
            break
        source_id, end_ordinal, end_byte = _history_position(history_base)
        if rollout_index is None:
            rollout_index = _rollout_index()
        current_path = _find_rollout_by_id(source_id, rollout_index)

    segments.reverse()
    return segments


def iter_session_jsonl(
    path: Path, warnings: list[dict[str, Any]] | None = None
) -> Iterator[JsonEntry]:
    """Yield the logical history of a legacy or paginated rollout."""
    diagnostics = warnings if warnings is not None else []
    segments = rollout_lineage(path)
    if len(segments) == 1 and segments[0].start_ordinal == 0:
        yield from iter_jsonl(path, diagnostics)
        return

    for segment in segments:
        segment_metadata = first_session_metadata(segment.path)
        subagent_start = segment_metadata.get("subagent_history_start_ordinal")
        if subagent_start is not None and (
            isinstance(subagent_start, bool)
            or not isinstance(subagent_start, int)
            or subagent_start <= 0
        ):
            raise ValueError("Paginated subagent history has an invalid ordinal boundary.")
        last_ordinal: int | None = None
        read_state = _JsonlReadState()
        for line_number, entry in iter_jsonl(
            segment.path,
            diagnostics,
            end_byte_offset=segment.end_byte_offset,
            _state=read_state,
        ):
            ordinal = entry.get("ordinal")
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
                raise ValueError("Paginated rollout record has no valid ordinal.")
            expected_ordinal = (
                segment.start_ordinal - 1 if last_ordinal is None else last_ordinal + 1
            )
            skipped_ordinals = ordinal - expected_ordinal
            if skipped_ordinals < 0 or skipped_ordinals > read_state.pending_rejected_lines:
                if last_ordinal is None:
                    raise ValueError("Paginated rollout starts at an unexpected ordinal.")
                raise ValueError("Paginated rollout ordinals are not contiguous.")
            read_state.pending_rejected_lines = 0
            last_ordinal = ordinal
            if ordinal < segment.start_ordinal:
                continue
            if subagent_start is not None and ordinal < subagent_start:
                continue
            if (
                segment.end_ordinal_exclusive is not None
                and ordinal >= segment.end_ordinal_exclusive
            ):
                raise ValueError("Paginated history ordinal and byte boundaries disagree.")
            yield line_number, entry
        if segment.end_ordinal_exclusive is not None:
            expected_last = segment.end_ordinal_exclusive - 1
            if last_ordinal != expected_last:
                raise ValueError("Paginated history boundary does not end at the declared ordinal.")


def session_signature(path: Path) -> tuple[tuple[str, int, int, int], ...]:
    """Return a cache signature that includes every inherited rollout segment."""
    signature: list[tuple[str, int, int, int]] = []
    for segment in rollout_lineage(path):
        stat = segment.path.stat()
        signature.append(
            (
                str(segment.path.resolve(strict=True)),
                stat.st_mtime_ns,
                stat.st_size,
                stat.st_ctime_ns,
            )
        )
    return tuple(signature)


def read_jsonl(path: Path) -> tuple[list[JsonEntry], list[dict[str, Any]]]:
    """Read valid JSON objects and report malformed complete lines."""
    warnings: list[dict[str, Any]] = []
    return list(iter_jsonl(path, warnings)), warnings

"""Safe discovery and reading of local Codex rollout logs."""

from __future__ import annotations

import errno
import importlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, cast

from .json_support import strict_json_loads

JsonEntry = tuple[int, dict[str, Any]]
MAX_DIAGNOSTICS = 100
MAX_JSONL_LINE_BYTES = 16 * 1024 * 1024
MAX_JSONL_TOTAL_BYTES = 512 * 1024 * 1024
MAX_JSONL_LINES = 1_000_000
MAX_SESSION_HEADER_BYTES = MAX_JSONL_LINE_BYTES
MAX_SESSION_HEADER_LINES = 10
MAX_DISCOVERY_ENTRIES = 100_000
MAX_DISCOVERED_SESSION_FILES = 10_000
MAX_SESSION_DIRECTORIES = 10_000
MAX_SESSION_DEPTH = 64
MAX_LINEAGE_SEGMENTS = 1_024
_SEARCH_LINE_MARKERS = (
    b'"turn_context"',
    b'"user_message"',
    b'"item_completed"',
)
ZSTD_COMPRESSED_CHUNK_BYTES = 1024
ZSTD_DECOMPRESSED_CHUNK_BYTES = 1024 * 1024
ZSTD_MAX_WINDOW_BYTES = 1 << 27
ZSTD_SPOOL_MEMORY_BYTES = 8 * 1024 * 1024
_UUID_TEXT = (
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_UUID_PATTERN = re.compile(_UUID_TEXT)
_ROLLOUT_IDENTITY_PATTERN = re.compile(rf"(?P<thread>{_UUID_TEXT})(?:_(?P<rollout>{_UUID_TEXT}))?$")
_WINDOWS_REPARSE_POINT = 0x400


class _FormatProbeLimit(ValueError):
    """A bounded metadata/format probe reached its work limit."""


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


@dataclass
class _JsonlBudget:
    """Bytes and physical lines consumed by one logical rollout read."""

    bytes_read: int = 0
    lines_read: int = 0


@dataclass
class _DiscoveryState:
    """Global work counters for one bounded discovery request."""

    entries: int = 0
    directories: int = 0
    files: int = 0


def _add_diagnostic(diagnostics: list[dict[str, Any]], value: dict[str, Any]) -> None:
    if len(diagnostics) < MAX_DIAGNOSTICS:
        diagnostics.append(value)


def _absolute_path(path: Path) -> Path:
    """Normalize dot components without resolving links."""
    return Path(os.path.abspath(os.fspath(path)))


def _file_identity(file_stat: os.stat_result) -> tuple[int, int, int]:
    return file_stat.st_dev, file_stat.st_ino, stat.S_IFMT(file_stat.st_mode)


def _is_link_or_reparse_point(file_stat: os.stat_result) -> bool:
    attributes = getattr(file_stat, "st_file_attributes", 0)
    return stat.S_ISLNK(file_stat.st_mode) or bool(attributes & _WINDOWS_REPARSE_POINT)


def _validate_regular_file(file_stat: os.stat_result) -> None:
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
        raise OSError(errno.EPERM, "Session source is not a single-link regular file.")


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        _absolute_path(path).relative_to(_absolute_path(root))
        return True
    except ValueError:
        return False


def _authorized_session_root(path: Path) -> Path | None:
    absolute = _absolute_path(path)
    try:
        canonical_candidate = absolute.parent.resolve(strict=True) / absolute.name
    except (OSError, RuntimeError):
        return None
    for configured_root in session_roots(True):
        try:
            root = configured_root.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if _path_is_within(absolute, _absolute_path(configured_root)) or _path_is_within(
            canonical_candidate, root
        ):
            return root
    return None


def _windows_descriptor_path(descriptor: int) -> Path:
    ctypes = importlib.import_module("ctypes")
    msvcrt = importlib.import_module("msvcrt")
    get_final_path = ctypes.windll.kernel32.GetFinalPathNameByHandleW
    get_final_path.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    get_final_path.restype = ctypes.c_uint32
    handle = ctypes.c_void_p(msvcrt.get_osfhandle(descriptor))
    required = get_final_path(handle, None, 0, 0)
    if not required:
        raise ctypes.WinError()
    buffer = ctypes.create_unicode_buffer(required + 1)
    written = get_final_path(handle, buffer, len(buffer), 0)
    if not written or written >= len(buffer):
        raise ctypes.WinError()
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def _open_regular_binary_fallback(path: Path, trusted_root: Path | None) -> BinaryIO:
    """Open safely on Windows and fail closed on unsupported platforms."""
    if os.name != "nt":
        raise OSError(errno.ENOTSUP, "Safe descriptor-relative file opens are unavailable.")
    absolute = _absolute_path(path)
    if not absolute.anchor or len(absolute.parts) < 2:
        raise OSError(errno.EPERM, "Session source path is invalid.")

    before: list[tuple[Path, tuple[int, int, int]]] = []
    current = Path(absolute.anchor)
    for index, part in enumerate(absolute.parts[1:]):
        current /= part
        component_stat = os.lstat(current)
        if _is_link_or_reparse_point(component_stat):
            raise OSError(errno.ELOOP, "Session source path contains a link.")
        if index < len(absolute.parts) - 2 and not stat.S_ISDIR(component_stat.st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "Session source parent is not a directory.")
        if index == len(absolute.parts) - 2:
            _validate_regular_file(component_stat)
        identity = _file_identity(component_stat)
        if identity[1] == 0:
            raise OSError(errno.ENOTSUP, "Stable filesystem identities are unavailable.")
        before.append((current, identity))

    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(absolute, flags)
    try:
        opened_stat = os.fstat(descriptor)
        _validate_regular_file(opened_stat)
        if trusted_root is not None and not _path_is_within(
            _windows_descriptor_path(descriptor), trusted_root
        ):
            raise OSError(errno.EPERM, "Session source escaped its authorized root.")
        after: list[tuple[Path, tuple[int, int, int]]] = []
        for component, _ in before:
            component_stat = os.lstat(component)
            if _is_link_or_reparse_point(component_stat):
                raise OSError(errno.ELOOP, "Session source path contains a link.")
            after.append((component, _file_identity(component_stat)))
        if before != after or _file_identity(opened_stat) != after[-1][1]:
            raise OSError(errno.EAGAIN, "Session source changed while it was opened.")
        return cast(BinaryIO, os.fdopen(descriptor, "rb", closefd=True))
    except BaseException:
        os.close(descriptor)
        raise


def _open_regular_binary(path: Path, *, trusted_root: Path | None = None) -> BinaryIO:
    """Open one regular file without following any path-component links."""
    absolute = _absolute_path(path)
    root = trusted_root if trusted_root is not None else _authorized_session_root(absolute)
    supports_dir_fd = os.open in getattr(os, "supports_dir_fd", set())
    no_follow = getattr(os, "O_NOFOLLOW", 0)
    directory_only = getattr(os, "O_DIRECTORY", 0)
    if not supports_dir_fd or not no_follow or not directory_only:
        fallback_root = root
        if fallback_root is None:
            try:
                fallback_root = absolute.parent.resolve(strict=True)
            except (OSError, RuntimeError):
                raise OSError(errno.ENOENT, "Session source parent was not found.") from None
        return _open_regular_binary_fallback(absolute, fallback_root)

    if not absolute.anchor or len(absolute.parts) < 2:
        raise OSError(errno.EPERM, "Session source path is invalid.")
    relative_parts: tuple[str, ...]
    try:
        canonical_candidate = absolute.parent.resolve(strict=True) / absolute.name
    except (OSError, RuntimeError):
        raise OSError(errno.ENOENT, "Session source parent was not found.") from None
    if root is None:
        anchor = canonical_candidate.parent
        relative_parts = (absolute.name,)
    else:
        anchor = _absolute_path(root)
        try:
            relative_parts = canonical_candidate.relative_to(anchor).parts
        except ValueError:
            raise OSError(errno.EPERM, "Session source escaped its authorized root.") from None
    if not relative_parts:
        raise OSError(errno.EPERM, "Session source path is invalid.")
    directory_flags = os.O_RDONLY | directory_only | no_follow | getattr(os, "O_CLOEXEC", 0)
    file_flags = (
        os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    filesystem_anchor = Path(anchor.anchor)
    directory_descriptor = os.open(filesystem_anchor, directory_flags)
    try:
        for part in anchor.parts[1:]:
            next_descriptor = os.open(part, directory_flags, dir_fd=directory_descriptor)
            os.close(directory_descriptor)
            directory_descriptor = next_descriptor
        for part in relative_parts[:-1]:
            next_descriptor = os.open(part, directory_flags, dir_fd=directory_descriptor)
            os.close(directory_descriptor)
            directory_descriptor = next_descriptor
        descriptor = os.open(relative_parts[-1], file_flags, dir_fd=directory_descriptor)
    finally:
        os.close(directory_descriptor)
    try:
        _validate_regular_file(os.fstat(descriptor))
        return cast(BinaryIO, os.fdopen(descriptor, "rb", closefd=True))
    except BaseException:
        os.close(descriptor)
        raise


def _safe_file_stat(path: Path, *, trusted_root: Path | None = None) -> os.stat_result:
    with _open_regular_binary(path, trusted_root=trusted_root) as handle:
        return os.fstat(handle.fileno())


def _is_compressed_rollout(path: Path) -> bool:
    return path.name.endswith(".jsonl.zst")


def _write_decompressed_chunk(spool: BinaryIO, chunk: bytes, total: int) -> int:
    next_total = total + len(chunk)
    if next_total > MAX_JSONL_TOTAL_BYTES:
        raise ValueError("Codex rollout exceeds the total JSONL byte limit.")
    spool.write(chunk)
    return next_total


def _copy_stdlib_zstd(source: BinaryIO, spool: BinaryIO, zstd: Any) -> None:
    options = {zstd.DecompressionParameter.window_log_max: 27}
    reader = cast(BinaryIO, zstd.open(source, "rb", options=options))
    try:
        total = 0
        while True:
            chunk = reader.read(ZSTD_DECOMPRESSED_CHUNK_BYTES)
            if not chunk:
                break
            total = _write_decompressed_chunk(spool, chunk, total)
    finally:
        reader.close()


def _copy_python_zstandard(source: BinaryIO, spool: BinaryIO, zstd: Any) -> None:
    """Decode every frame and reject truncated streams or trailing garbage."""
    pending = b""
    decoder: Any | None = None
    saw_frame = False
    total = 0
    while True:
        if not pending:
            pending = source.read(ZSTD_COMPRESSED_CHUNK_BYTES)
            if not pending:
                break
        if decoder is None:
            decoder = zstd.ZstdDecompressor(max_window_size=ZSTD_MAX_WINDOW_BYTES).decompressobj(
                write_size=64 * 1024,
                read_across_frames=False,
            )
        compressed_chunk = pending
        output = decoder.decompress(compressed_chunk)
        total = _write_decompressed_chunk(spool, output, total)
        pending = decoder.unconsumed_tail
        if decoder.eof:
            saw_frame = True
            pending = decoder.unused_data or pending
            decoder = None
        elif pending == compressed_chunk and not output:
            raise ValueError("Compressed Codex rollout decoder made no progress.")
    if decoder is not None or not saw_frame:
        raise EOFError("Compressed rollout ended before a complete zstd frame.")


def _decompress_rollout(source: BinaryIO) -> BinaryIO:
    spool = tempfile.SpooledTemporaryFile(  # noqa: SIM115 - ownership transfers to caller
        max_size=ZSTD_SPOOL_MEMORY_BYTES,
        mode="w+b",
    )
    binary_spool = cast(BinaryIO, spool)
    try:
        try:
            zstd = importlib.import_module("compression.zstd")
        except ModuleNotFoundError:
            try:
                zstd = importlib.import_module("zstandard")
            except ModuleNotFoundError:
                raise ValueError(
                    "Compressed Codex rollouts require compression.zstd or zstandard."
                ) from None
            _copy_python_zstandard(source, binary_spool, zstd)
        else:
            _copy_stdlib_zstd(source, binary_spool, zstd)
        spool.seek(0)
        return binary_spool
    except BaseException:
        spool.close()
        raise


def _open_rollout_binary(path: Path) -> BinaryIO:
    source = _open_regular_binary(path)
    if not _is_compressed_rollout(path):
        return source
    try:
        if os.fstat(source.fileno()).st_size > MAX_JSONL_TOTAL_BYTES:
            raise ValueError("Compressed Codex rollout exceeds the source byte limit.")
        return _decompress_rollout(source)
    except EOFError as error:
        raise ValueError("Compressed Codex rollout is not a valid zstd stream.") from error
    except (OSError, ValueError):
        raise
    except Exception as error:
        raise ValueError("Compressed Codex rollout is not a valid zstd stream.") from error
    finally:
        source.close()


def _rollout_stream_size(handle: BinaryIO, path: Path) -> int:
    if not _is_compressed_rollout(path):
        return os.fstat(handle.fileno()).st_size
    current = handle.tell()
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    handle.seek(current)
    return size


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
        canonical_root = root.resolve(strict=True)
        absolute = _absolute_path(path)
        relative = (absolute.parent.resolve(strict=True) / absolute.name).relative_to(
            canonical_root
        )
        if not relative.parts:
            return False
        _safe_file_stat(path, trusted_root=canonical_root)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _is_rollout_file_name(name: str) -> bool:
    return name.endswith(".jsonl") or name.endswith(".jsonl.zst")


def _plain_rollout_path(path: Path) -> Path:
    return path.with_suffix("") if _is_compressed_rollout(path) else path


def _discover_session_root(root: Path, state: _DiscoveryState) -> list[Path]:
    result: list[Path] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        state.directories += 1
        if state.directories > MAX_SESSION_DIRECTORIES:
            raise ValueError("Codex session discovery exceeds the directory limit.")
        try:
            iterator = os.scandir(directory)
        except OSError:
            continue
        with iterator:
            for entry in iterator:
                state.entries += 1
                if state.entries > MAX_DISCOVERY_ENTRIES:
                    raise ValueError("Codex session discovery exceeds the entry limit.")
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        if depth >= MAX_SESSION_DEPTH:
                            raise ValueError("Codex session discovery exceeds the depth limit.")
                        stack.append((Path(entry.path), depth + 1))
                        continue
                    if not _is_rollout_file_name(entry.name) or not entry.is_file(
                        follow_symlinks=False
                    ):
                        continue
                except OSError:
                    continue
                candidate = Path(entry.path)
                if not is_safe_session_file(candidate, root):
                    continue
                state.files += 1
                if state.files > MAX_DISCOVERED_SESSION_FILES:
                    raise ValueError("Codex session discovery exceeds the rollout file limit.")
                result.append(candidate)

    preferred: dict[Path, Path] = {}
    for candidate in sorted(result, key=lambda value: value.as_posix()):
        logical_path = _plain_rollout_path(candidate)
        existing = preferred.get(logical_path)
        if existing is None or (
            _is_compressed_rollout(existing) and not _is_compressed_rollout(candidate)
        ):
            preferred[logical_path] = candidate
    return sorted(preferred.values(), key=lambda value: value.as_posix())


def session_files(include_archived: bool) -> list[Path]:
    """Discover authorized rollout logs ordered by last modification."""
    paths: list[Path] = []
    discovery_state = _DiscoveryState()
    for root in session_roots(include_archived):
        try:
            canonical_root = root.resolve(strict=True)
        except (OSError, RuntimeError):
            continue
        if not canonical_root.is_dir():
            continue
        paths.extend(_discover_session_root(canonical_root, discovery_state))

    def modified(path: Path) -> int:
        try:
            return _safe_file_stat(path).st_mtime_ns
        except OSError:
            return 0

    return sorted(paths, key=modified, reverse=True)


def iter_jsonl(
    path: Path,
    warnings: list[dict[str, Any]] | None = None,
    *,
    end_byte_offset: int | None = None,
    _state: _JsonlReadState | None = None,
    _budget: _JsonlBudget | None = None,
    _scan_byte_limit: int | None = None,
    _scan_line_limit: int | None = None,
) -> Iterator[JsonEntry]:
    """Yield valid JSON objects up to an optional complete-line byte boundary."""
    diagnostics = warnings if warnings is not None else []
    budget = _budget if _budget is not None else _JsonlBudget()
    if end_byte_offset is not None and (
        isinstance(end_byte_offset, bool)
        or not isinstance(end_byte_offset, int)
        or end_byte_offset < 0
    ):
        raise ValueError("Invalid paginated history byte boundary.")
    with _open_rollout_binary(path) as handle:
        opened_size = _rollout_stream_size(handle, path)
        if end_byte_offset is not None and end_byte_offset > opened_size:
            raise ValueError("Paginated history byte boundary is past the source rollout.")
        read_end = opened_size if end_byte_offset is None else end_byte_offset
        if read_end > MAX_JSONL_TOTAL_BYTES:
            raise ValueError("Codex rollout exceeds the total JSONL byte limit.")
        line_number = 0
        while True:
            line_start = handle.tell()
            if line_start >= read_end:
                break
            if line_number >= MAX_JSONL_LINES or budget.lines_read >= MAX_JSONL_LINES:
                raise ValueError("Codex rollout exceeds the total JSONL line limit.")
            if _scan_line_limit is not None and line_number >= _scan_line_limit:
                raise _FormatProbeLimit("Codex session header exceeds the format-probe line limit.")
            if _scan_byte_limit is not None and line_start >= _scan_byte_limit:
                raise _FormatProbeLimit("Codex session header exceeds the format-probe byte limit.")
            read_size = min(MAX_JSONL_LINE_BYTES + 1, read_end - line_start)
            encoded_line = handle.readline(read_size)
            if not encoded_line:
                break
            line_number += 1
            budget.lines_read += 1
            budget.bytes_read += len(encoded_line)
            if budget.bytes_read > MAX_JSONL_TOTAL_BYTES:
                raise ValueError("Codex rollout exceeds the total JSONL byte limit.")
            if _scan_byte_limit is not None and handle.tell() > _scan_byte_limit:
                raise _FormatProbeLimit("Codex session header exceeds the format-probe byte limit.")
            if len(encoded_line) > MAX_JSONL_LINE_BYTES:
                while (
                    encoded_line and not encoded_line.endswith(b"\n") and handle.tell() < read_end
                ):
                    chunk_size = min(MAX_JSONL_LINE_BYTES + 1, read_end - handle.tell())
                    encoded_line = handle.readline(chunk_size)
                    budget.bytes_read += len(encoded_line)
                    if budget.bytes_read > MAX_JSONL_TOTAL_BYTES:
                        raise ValueError("Codex rollout exceeds the total JSONL byte limit.")
                    if _scan_byte_limit is not None and handle.tell() > _scan_byte_limit:
                        raise _FormatProbeLimit(
                            "Codex session header exceeds the format-probe byte limit."
                        )
                line_end = handle.tell()
                complete = encoded_line.endswith(b"\n")
                if end_byte_offset is not None and line_end == end_byte_offset and not complete:
                    boundary_width = end_byte_offset - line_start
                    if end_byte_offset < opened_size and boundary_width > MAX_JSONL_LINE_BYTES + 1:
                        raise ValueError("Paginated history byte boundary splits a JSONL record.")
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
            complete = encoded_line.endswith(b"\n")
            if end_byte_offset is not None and line_end == end_byte_offset and not complete:
                if end_byte_offset < opened_size:
                    raise ValueError("Paginated history byte boundary splits a JSONL record.")
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


def _leading_session_metadata(path: Path) -> tuple[bool, dict[str, Any]]:
    try:
        for line_number, entry in iter_jsonl(
            path,
            _scan_byte_limit=MAX_SESSION_HEADER_BYTES,
            _scan_line_limit=MAX_SESSION_HEADER_LINES,
        ):
            if entry.get("type") != "session_meta":
                continue
            payload = entry.get("payload")
            return line_number == 1, payload if isinstance(payload, dict) else {}
    except _FormatProbeLimit:
        pass
    return False, {}


def first_session_metadata(path: Path) -> dict[str, Any]:
    """Return bounded legacy metadata, while requiring paginated metadata to lead."""
    is_leading, metadata = _leading_session_metadata(path)
    if not is_leading and str(metadata.get("history_mode") or "").casefold() == "paginated":
        return {}
    return metadata


def _contains_paginated_records(path: Path) -> bool:
    """Detect a paginated wire shape even when its required metadata is damaged."""
    try:
        for _, entry in iter_jsonl(
            path,
            _scan_byte_limit=MAX_SESSION_HEADER_BYTES,
            _scan_line_limit=MAX_SESSION_HEADER_LINES,
        ):
            payload = entry.get("payload")
            if "ordinal" in entry or (
                entry.get("type") == "session_meta"
                and isinstance(payload, dict)
                and str(payload.get("history_mode") or "").casefold() == "paginated"
            ):
                return True
    except _FormatProbeLimit:
        pass
    return False


def _rollout_identity_from_path(path: Path) -> tuple[str, str] | None:
    plain_path = _plain_rollout_path(path)
    if not plain_path.name.endswith(".jsonl"):
        return None
    match = _ROLLOUT_IDENTITY_PATTERN.search(plain_path.name[: -len(".jsonl")])
    if match is None:
        return None
    thread_id = match.group("thread").lower()
    rollout_id = (match.group("rollout") or thread_id).lower()
    return thread_id, rollout_id


def rollout_id_from_path(path: Path) -> str | None:
    """Extract the immutable physical rollout identity from a canonical filename."""
    identity = _rollout_identity_from_path(path)
    return identity[1] if identity is not None else None


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
    has_leading_metadata, target_metadata = _leading_session_metadata(path)
    if str(target_metadata.get("history_mode") or "legacy").casefold() != "paginated":
        if not has_leading_metadata and _contains_paginated_records(path):
            raise ValueError(
                "Paginated rollout has no valid paginated session metadata at its leading record."
            )
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
        has_leading_metadata, metadata = _leading_session_metadata(current_path)
        if not has_leading_metadata:
            raise ValueError(
                "Paginated rollout has no valid paginated session metadata at its leading record."
            )
        if str(metadata.get("history_mode") or "").casefold() != "paginated":
            raise ValueError("Paginated history source rollout uses a different history mode.")
        current_identity = _rollout_identity_from_path(current_path)
        if current_identity is None:
            raise ValueError("Paginated rollout filename does not contain a rollout UUID.")
        current_thread_id, current_rollout_id = current_identity
        metadata_thread_id = metadata.get("id")
        if (
            not isinstance(metadata_thread_id, str)
            or _UUID_PATTERN.fullmatch(metadata_thread_id) is None
            or metadata_thread_id.casefold() != current_thread_id
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
    budget = _JsonlBudget()
    if len(segments) == 1 and segments[0].start_ordinal == 0:
        for line_number, entry in iter_jsonl(path, diagnostics, _budget=budget):
            payload = entry.get("payload")
            if "ordinal" in entry or (
                entry.get("type") == "session_meta"
                and isinstance(payload, dict)
                and str(payload.get("history_mode") or "").casefold() == "paginated"
            ):
                raise ValueError(
                    "Paginated rollout has no valid paginated session metadata "
                    "at its leading record."
                )
            yield line_number, entry
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
            _budget=budget,
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


def _iter_search_lines(
    path: Path,
    *,
    end_byte_offset: int | None = None,
) -> Iterator[tuple[int, bytes]]:
    """Yield bounded complete lines that may contain searchable session metadata."""
    if end_byte_offset is not None:
        if isinstance(end_byte_offset, bool) or end_byte_offset < 0:
            raise ValueError("Invalid paginated history byte boundary.")
        if end_byte_offset > path.stat().st_size:
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
                if end_byte_offset is not None and handle.tell() > end_byte_offset:
                    raise ValueError("Paginated history byte boundary splits a JSONL record.")
                continue
            line_end = handle.tell()
            if end_byte_offset is not None and line_end > end_byte_offset:
                raise ValueError("Paginated history byte boundary splits a JSONL record.")
            if not encoded_line.endswith(b"\n"):
                if end_byte_offset is not None and line_end == end_byte_offset:
                    raise ValueError(
                        "Paginated history byte boundary omits the terminating newline."
                    )
                continue
            if any(marker in encoded_line for marker in _SEARCH_LINE_MARKERS):
                yield line_number, encoded_line


def _decode_search_entry(encoded_line: bytes) -> dict[str, Any] | None:
    try:
        value = strict_json_loads(encoded_line.decode("utf-8"))
    except (RecursionError, UnicodeDecodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def iter_session_search_jsonl(path: Path) -> Iterator[JsonEntry]:
    """Yield only records needed to build a lightweight searchable session index."""
    segments = rollout_lineage(path)
    if len(segments) == 1 and segments[0].start_ordinal == 0:
        for line_number, encoded_line in _iter_search_lines(path):
            entry = _decode_search_entry(encoded_line)
            if entry is not None:
                yield line_number, entry
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
        for line_number, encoded_line in _iter_search_lines(
            segment.path,
            end_byte_offset=segment.end_byte_offset,
        ):
            entry = _decode_search_entry(encoded_line)
            if entry is None:
                continue
            ordinal = entry.get("ordinal")
            if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
                continue
            if ordinal < segment.start_ordinal:
                continue
            if subagent_start is not None and ordinal < subagent_start:
                continue
            if (
                segment.end_ordinal_exclusive is not None
                and ordinal >= segment.end_ordinal_exclusive
            ):
                continue
            yield line_number, entry


def session_signature(path: Path) -> tuple[tuple[str, int, int, int], ...]:
    """Return a cache signature that includes every inherited rollout segment."""
    signature: list[tuple[str, int, int, int]] = []
    for segment in rollout_lineage(path):
        file_stat = _safe_file_stat(segment.path)
        signature.append(
            (
                str(_absolute_path(segment.path)),
                file_stat.st_mtime_ns,
                file_stat.st_size,
                file_stat.st_ctime_ns,
            )
        )
    return tuple(signature)


def read_jsonl(path: Path) -> tuple[list[JsonEntry], list[dict[str, Any]]]:
    """Read valid JSON objects and report malformed complete lines."""
    warnings: list[dict[str, Any]] = []
    return list(iter_jsonl(path, warnings)), warnings

"""Private, bounded search metadata index for local Codex sessions."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import stat
import threading
import time
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path
from typing import Any

from .filesystem import is_link_or_reparse_point, linked_state_path
from .json_support import strict_json_loads
from .sessions import codex_home

INDEX_VERSION = 1
MAX_INDEX_BYTES = 8 * 1024 * 1024
MAX_INDEX_ENTRIES = 10_000
MAX_INDEX_LINEAGE_SEGMENTS = 1_024
MAX_STAT_VALUE = 2**63 - 1
_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_INDEX_WRITE_LOCK = threading.Lock()

SessionSignature = tuple[tuple[str, int, int, int], ...]


def search_index_path() -> Path:
    """Return the private plugin-owned session search index path."""
    return codex_home() / "codex-trajectory" / "session-search-index-v1.json"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogateescape")).hexdigest()


def _path_key(path: Path) -> str:
    return _digest(str(path.resolve(strict=True)))


def _stored_signature(signature: SessionSignature) -> list[list[str | int]]:
    return [
        [_digest(segment_path), modified, size, changed]
        for segment_path, modified, size, changed in signature
    ]


def _valid_stat(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and 0 <= value <= MAX_STAT_VALUE


def _valid_signature(value: Any) -> bool:
    return (
        isinstance(value, list)
        and 1 <= len(value) <= MAX_INDEX_LINEAGE_SEGMENTS
        and all(
            isinstance(segment, list)
            and len(segment) == 4
            and isinstance(segment[0], str)
            and _HASH_PATTERN.fullmatch(segment[0]) is not None
            and all(_valid_stat(item) for item in segment[1:])
            for segment in value
        )
    )


def _bounded_text(value: Any, maximum: int, *, nullable: bool = False) -> bool:
    return (nullable and value is None) or (isinstance(value, str) and len(value) <= maximum)


def _valid_entry(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) == {"signature", "id", "title", "cwd", "model"}
        and _valid_signature(value.get("signature"))
        and isinstance(value.get("id"), str)
        and 1 <= len(value["id"]) <= 240
        and _bounded_text(value.get("title"), 100)
        and _bounded_text(value.get("cwd"), 500, nullable=True)
        and _bounded_text(value.get("model"), 200, nullable=True)
    )


def _read_bounded_regular(path: Path) -> bytes | None:
    """Read one single-link regular index file without following links."""
    if linked_state_path(path.parent):
        return None
    try:
        expected = path.lstat()
    except OSError:
        return None
    if (
        not stat.S_ISREG(expected.st_mode)
        or is_link_or_reparse_point(expected)
        or expected.st_nlink != 1
        or expected.st_size > MAX_INDEX_BYTES
    ):
        return None
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_dev != expected.st_dev
            or opened.st_ino != expected.st_ino
        ):
            return None
        chunks: list[bytes] = []
        remaining = MAX_INDEX_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    return raw if len(raw) <= MAX_INDEX_BYTES else None


def _read_entries(path: Path) -> OrderedDict[str, dict[str, Any]]:
    raw = _read_bounded_regular(path)
    if raw is None:
        return OrderedDict()
    try:
        value = strict_json_loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return OrderedDict()
    if (
        not isinstance(value, dict)
        or set(value) != {"schemaVersion", "entries"}
        or value.get("schemaVersion") != INDEX_VERSION
        or not isinstance(value.get("entries"), dict)
        or len(value["entries"]) > MAX_INDEX_ENTRIES
    ):
        return OrderedDict()
    entries: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for key, entry in value["entries"].items():
        if (
            not isinstance(key, str)
            or _HASH_PATTERN.fullmatch(key) is None
            or not _valid_entry(entry)
        ):
            return OrderedDict()
        entries[key] = entry
    return entries


def _atomic_write(path: Path, entries: OrderedDict[str, dict[str, Any]]) -> None:
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if linked_state_path(directory) or (
        existing is not None
        and (
            is_link_or_reparse_point(existing)
            or not stat.S_ISREG(existing.st_mode)
            or existing.st_nlink != 1
        )
    ):
        raise OSError("Refusing to replace a linked or non-regular session index.")
    while True:
        value = {"schemaVersion": INDEX_VERSION, "entries": entries}
        encoded = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        if len(encoded) + 1 <= MAX_INDEX_BYTES:
            break
        if not entries:
            raise OSError("Session index exceeds its size limit.")
        entries.popitem(last=False)
    temporary = directory / f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        with suppress(OSError):
            path.chmod(0o600)
    finally:
        with suppress(OSError):
            temporary.unlink()


class SessionSearchIndex:
    """Load and update bounded search fields without persisting rollout paths."""

    def __init__(self) -> None:
        self._path = search_index_path()
        self._entries = _read_entries(self._path)
        self._dirty = False
        self._touched: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def lookup(self, path: Path, signature: SessionSignature) -> dict[str, Any] | None:
        """Return fields only when the complete lineage signature still matches."""
        key = _path_key(path)
        entry = self._entries.get(key)
        if entry is None or entry.get("signature") != _stored_signature(signature):
            return None
        self._entries.move_to_end(key)
        self._dirty = True
        self._touched[key] = entry
        self._touched.move_to_end(key)
        return {
            "id": entry["id"],
            "title": entry["title"],
            "cwd": entry["cwd"],
            "model": entry["model"],
        }

    def store(
        self,
        path: Path,
        signature: SessionSignature,
        fields: dict[str, Any],
    ) -> None:
        """Store one validated search record and retain the most recently used bound."""
        entry = {"signature": _stored_signature(signature), **fields}
        if not _valid_entry(entry):
            raise ValueError("Invalid session search metadata.")
        key = _path_key(path)
        if self._entries.get(key) != entry:
            self._entries[key] = entry
            self._dirty = True
        self._entries.move_to_end(key)
        self._touched[key] = entry
        self._touched.move_to_end(key)
        self._dirty = True
        while len(self._entries) > MAX_INDEX_ENTRIES:
            self._entries.popitem(last=False)
            self._dirty = True

    def flush(self) -> None:
        """Best-effort persistence; index failures never block session reads."""
        if not self._dirty:
            return
        try:
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            lock_path = self._path.with_suffix(".lock")
            if linked_state_path(lock_path.parent) or linked_state_path(lock_path):
                return
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            with _INDEX_WRITE_LOCK, os.fdopen(os.open(lock_path, flags, 0o600), "r+b") as lock:
                state = os.fstat(lock.fileno())
                linked = lock_path.lstat()
                if (
                    not stat.S_ISREG(state.st_mode)
                    or state.st_nlink != 1
                    or is_link_or_reparse_point(linked)
                    or (state.st_dev, state.st_ino) != (linked.st_dev, linked.st_ino)
                ):
                    return
                if os.name == "nt":
                    module = importlib.import_module("msvcrt")
                    if state.st_size == 0:
                        lock.write(b"0")
                        lock.flush()
                    lock.seek(0)
                    module.locking(lock.fileno(), module.LK_NBLCK, 1)
                else:
                    module = importlib.import_module("fcntl")
                    module.flock(lock.fileno(), module.LOCK_EX | module.LOCK_NB)
                # Merge only touched entries, not a stale whole-file snapshot.
                merged = _read_entries(self._path)
                for key, entry in self._touched.items():
                    current = merged.get(key)
                    if (
                        current is None
                        or current["signature"] == entry["signature"]
                        or max(part[1] for part in current["signature"])
                        <= max(part[1] for part in entry["signature"])
                    ):
                        merged[key] = entry
                        merged.move_to_end(key)
                while len(merged) > MAX_INDEX_ENTRIES:
                    merged.popitem(last=False)
                _atomic_write(self._path, merged)
                self._entries = merged
        except (OSError, ValueError, TypeError):
            return
        self._dirty = False
        self._touched.clear()

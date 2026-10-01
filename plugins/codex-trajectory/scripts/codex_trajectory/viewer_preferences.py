"""Persist the viewer's explicit full-detail opt-in without storing task data."""

from __future__ import annotations

import json
import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path

from .filesystem import is_link_or_reparse_point, linked_state_path
from .json_support import strict_json_loads
from .sessions import codex_home

MAX_PREFERENCE_BYTES = 512


def preferences_path() -> Path:
    """Return a plugin-owned setting shared by panels using the same Codex home."""
    return codex_home() / "codex-trajectory" / "viewer-preferences-v1.json"


def _regular_unlinked(value: os.stat_result) -> bool:
    return (
        stat.S_ISREG(value.st_mode) and not is_link_or_reparse_point(value) and value.st_nlink == 1
    )


def default_full_details() -> bool:
    """Missing, malformed, unreadable, or linked settings retain safe summaries."""
    path = preferences_path()
    if linked_state_path(path.parent):
        return False
    try:
        expected = path.lstat()
        if not _regular_unlinked(expected) or expected.st_size > MAX_PREFERENCE_BYTES:
            return False
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not _regular_unlinked(opened) or (opened.st_dev, opened.st_ino) != (
                expected.st_dev,
                expected.st_ino,
            ):
                return False
            raw = stream.read(MAX_PREFERENCE_BYTES + 1)
            current = path.lstat()
            if not _regular_unlinked(current) or (current.st_dev, current.st_ino) != (
                opened.st_dev,
                opened.st_ino,
            ):
                return False
        if len(raw) > MAX_PREFERENCE_BYTES:
            return False
        value = strict_json_loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    return (
        isinstance(value, dict)
        and set(value) == {"schemaVersion", "defaultFullDetails"}
        and type(value["schemaVersion"]) is int
        and value["schemaVersion"] == 1
        and value["defaultFullDetails"] is True
    )


def set_default_full_details(enabled: bool) -> None:
    """Atomically save only the opt-in flag, with private file permissions."""
    if not isinstance(enabled, bool):
        raise ValueError("defaultFullDetails must be a boolean.")
    path = preferences_path()
    directory = path.parent
    if linked_state_path(directory):
        raise OSError("Refusing a linked viewer preferences directory.")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if linked_state_path(directory):
        raise OSError("Refusing a linked viewer preferences directory.")
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    if existing is not None and not _regular_unlinked(existing):
        raise OSError("Refusing linked or non-regular viewer preferences.")
    encoded = json.dumps({"schemaVersion": 1, "defaultFullDetails": enabled}) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=directory)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with suppress(OSError):
            temporary.unlink()

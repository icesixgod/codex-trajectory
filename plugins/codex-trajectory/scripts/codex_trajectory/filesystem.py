"""Shared filesystem checks for local logs and plugin-owned cache files."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def is_link_or_reparse_point(file_stat: os.stat_result) -> bool:
    """Treat Windows junctions and other reparse points as links."""
    return stat.S_ISLNK(file_stat.st_mode) or bool(
        getattr(file_stat, "st_file_attributes", 0) & 0x400
    )


def linked_state_path(path: Path) -> bool:
    """Reject links and unreadable state paths while allowing absent files."""
    try:
        return is_link_or_reparse_point(path.lstat())
    except FileNotFoundError:
        return False
    except OSError:
        return True

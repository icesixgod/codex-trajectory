"""Shared link checks remain safe after retiring the optional integration."""

from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from codex_trajectory.filesystem import is_link_or_reparse_point, linked_state_path


def test_state_links_and_regular_files(tmp_path: Path) -> None:
    absent = tmp_path / "missing"
    assert not linked_state_path(absent)
    regular = tmp_path / "regular"
    regular.write_text("cache")
    assert not linked_state_path(regular)
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(regular)
    except OSError:
        pytest.skip("Symlink creation requires platform privileges")
    assert linked_state_path(linked)


def test_windows_reparse_points_and_unreadable_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    reparse: Any = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
    assert is_link_or_reparse_point(reparse)

    def fail(_path: Path) -> None:
        raise OSError("unreadable")

    monkeypatch.setattr(Path, "lstat", fail)
    assert linked_state_path(Path("unreadable"))

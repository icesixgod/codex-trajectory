"""Session-root safety tests."""

from __future__ import annotations

from pathlib import Path

from codex_trajectory.sessions import codex_home as resolve_codex_home
from codex_trajectory.sessions import iter_jsonl, read_jsonl, session_files, session_roots
from conftest import write_rollout


def test_discovery_excludes_symlinks_and_outside_files(codex_home: Path, tmp_path: Path) -> None:
    outside = write_rollout(tmp_path.parent / "outside-rollout.jsonl")
    link = codex_home / "sessions" / "linked.jsonl"
    try:
        link.symlink_to(outside)
    except OSError:
        return

    discovered = session_files(True)
    assert link not in discovered
    assert outside not in discovered
    assert {path.name for path in discovered} == {"rollout-alpha.jsonl", "rollout-archive.jsonl"}


def test_non_object_jsonl_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "values.jsonl"
    path.write_text("[]\n{}\n", encoding="utf-8")
    entries, warnings = read_jsonl(path)
    assert entries == [(2, {})]
    assert warnings[0]["code"] == "non_object_jsonl"


def test_jsonl_iteration_is_streaming(tmp_path: Path) -> None:
    path = tmp_path / "stream.jsonl"
    path.write_text('{}\n{"value":1}\n', encoding="utf-8")
    entries = iter_jsonl(path)

    assert iter(entries) is entries
    assert list(entries) == [(1, {}), (2, {"value": 1})]


def test_default_home_and_missing_roots(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert resolve_codex_home().name == ".codex"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    assert session_roots(False) == [tmp_path / "missing" / "sessions"]
    assert session_files(False) == []

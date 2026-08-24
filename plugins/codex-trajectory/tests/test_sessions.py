"""Session-root safety tests."""

from __future__ import annotations

import importlib
import io
import json
import os
import tempfile
from pathlib import Path

import pytest
from codex_trajectory import sessions
from codex_trajectory.json_support import MAX_JSON_NESTING_DEPTH
from codex_trajectory.sessions import (
    _history_position,
    _JsonlReadState,
    first_session_metadata,
    is_archived_session,
    is_safe_session_file,
    iter_jsonl,
    iter_session_jsonl,
    read_jsonl,
    rollout_id_from_path,
    rollout_lineage,
    session_files,
    session_roots,
)
from codex_trajectory.sessions import codex_home as resolve_codex_home
from conftest import write_rollout


def _zstd_compress(value: bytes) -> bytes:
    try:
        zstd = importlib.import_module("compression.zstd")
    except ModuleNotFoundError:
        zstd = importlib.import_module("zstandard")
        return zstd.ZstdCompressor().compress(value)  # type: ignore[no-any-return]
    return zstd.compress(value)  # type: ignore[no-any-return]


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


def test_discovery_excludes_hardlinks_to_outside_files(codex_home: Path, tmp_path: Path) -> None:
    outside = write_rollout(tmp_path.parent / "outside-hardlink-rollout.jsonl")
    link = codex_home / "sessions" / "hardlinked.jsonl"
    try:
        os.link(outside, link)
    except OSError:
        pytest.skip("hardlinks are unavailable on this filesystem")

    assert link not in session_files(True)


def test_discovered_file_swap_to_symlink_is_rejected(codex_home: Path, tmp_path: Path) -> None:
    discovered = next(path for path in session_files(False) if path.name == "rollout-alpha.jsonl")
    outside = write_rollout(tmp_path / "outside-after-discovery.jsonl")
    discovered.unlink()
    try:
        discovered.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable on this filesystem")

    with pytest.raises(OSError):
        list(iter_jsonl(discovered))


def test_discovered_ancestor_swap_to_outside_symlink_is_rejected(
    codex_home: Path, tmp_path: Path
) -> None:
    discovered = next(path for path in session_files(False) if path.name == "rollout-alpha.jsonl")
    original_parent = discovered.parent
    saved_parent = original_parent.with_name("saved-2026")
    outside_parent = tmp_path / "outside-directory"
    write_rollout(outside_parent / discovered.name, [{"source": "outside"}])
    original_parent.rename(saved_parent)
    try:
        original_parent.symlink_to(outside_parent, target_is_directory=True)
    except OSError:
        saved_parent.rename(original_parent)
        pytest.skip("directory symlinks are unavailable on this filesystem")

    with pytest.raises(OSError):
        list(iter_jsonl(discovered))


def test_session_file_safety_rejects_paths_outside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    outside = write_rollout(tmp_path / "outside.jsonl")

    assert is_safe_session_file(outside, root) is False
    assert rollout_id_from_path(tmp_path / "not-a-rollout-id.jsonl") is None


def test_safe_open_accepts_the_macos_var_filesystem_alias() -> None:
    if not Path("/var").is_symlink():
        pytest.skip("/var is not a filesystem alias on this platform")
    with tempfile.TemporaryDirectory(dir="/var/tmp") as directory:
        path = Path(directory) / "rollout.jsonl"
        path.write_text("{}\n", encoding="utf-8")

        assert list(iter_jsonl(path)) == [(1, {})]


def test_safe_open_fallback_anchors_direct_paths_to_their_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "rollout.jsonl"
    path.write_text("{}\n", encoding="utf-8")
    opened: list[tuple[Path, Path | None]] = []

    def fallback(candidate: Path, trusted_root: Path | None) -> io.BytesIO:
        opened.append((candidate, trusted_root))
        return io.BytesIO(b"{}\n")

    monkeypatch.setattr(sessions.os, "supports_dir_fd", set())
    monkeypatch.setattr(sessions, "_open_regular_binary_fallback", fallback)

    with sessions._open_regular_binary(path) as handle:
        assert handle.read() == b"{}\n"
    assert opened == [(path.resolve(), path.parent.resolve())]


def test_compressed_rollouts_are_discovered_read_and_prefer_plain_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    rollout_id = "10101010-1010-4010-8010-101010101010"
    compressed = tmp_path / "sessions" / f"rollout-2026-08-24T00-00-00-{rollout_id}.jsonl.zst"
    compressed.parent.mkdir()
    compressed.write_bytes(_zstd_compress(b"{}\n"))

    assert session_files(False) == [compressed]
    assert rollout_id_from_path(compressed) == rollout_id
    assert list(iter_jsonl(compressed)) == [(1, {})]

    compressed.write_bytes(_zstd_compress(b" " * 1024 + b"\n"))
    monkeypatch.setattr(sessions, "MAX_JSONL_TOTAL_BYTES", 100)
    with pytest.raises(ValueError, match="total JSONL byte limit"):
        list(iter_jsonl(compressed))
    monkeypatch.setattr(sessions, "MAX_JSONL_TOTAL_BYTES", 512 * 1024 * 1024)

    plain = compressed.with_suffix("")
    plain.write_text("{}\n", encoding="utf-8")
    assert session_files(False) == [plain]


def test_compressed_rollouts_support_multiple_frames_and_reject_bad_tails(
    tmp_path: Path,
) -> None:
    compressed = tmp_path / "rollout.jsonl.zst"
    first_frame = _zstd_compress(b"{}\n")
    second_frame = _zstd_compress(b'{"value":1}\n')
    compressed.write_bytes(first_frame + second_frame)

    assert list(iter_jsonl(compressed)) == [(1, {}), (2, {"value": 1})]

    compressed.write_bytes(first_frame[:-1])
    with pytest.raises(ValueError):
        list(iter_jsonl(compressed))

    compressed.write_bytes(first_frame + b"trailing garbage")
    with pytest.raises(ValueError):
        list(iter_jsonl(compressed))

    compressed.write_bytes(b"not a zstd frame")
    with pytest.raises(ValueError):
        list(iter_jsonl(compressed))


@pytest.mark.parametrize(
    ("budget_name", "expected"),
    [
        ("MAX_DISCOVERED_SESSION_FILES", "rollout file limit"),
        ("MAX_DISCOVERY_ENTRIES", "entry limit"),
        ("MAX_SESSION_DIRECTORIES", "directory limit"),
        ("MAX_SESSION_DEPTH", "depth limit"),
    ],
)
def test_session_discovery_enforces_work_budgets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    budget_name: str,
    expected: str,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    root = tmp_path / "sessions"
    root.mkdir()
    if budget_name == "MAX_DISCOVERED_SESSION_FILES":
        for name in ("a.jsonl", "b.jsonl", "c.jsonl"):
            (root / name).write_text("{}\n", encoding="utf-8")
        limit = 2
    elif budget_name == "MAX_DISCOVERY_ENTRIES":
        for name in ("a.txt", "b.txt", "c.txt"):
            (root / name).write_text("ignored", encoding="utf-8")
        limit = 2
    elif budget_name == "MAX_SESSION_DIRECTORIES":
        (root / "a").mkdir()
        (root / "b").mkdir()
        limit = 2
    else:
        (root / "a" / "b").mkdir(parents=True)
        limit = 1
    monkeypatch.setattr(sessions, budget_name, limit)

    with pytest.raises(ValueError, match=expected):
        session_files(False)


def test_discovery_has_a_deterministic_active_first_tiebreak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    active_z = write_rollout(tmp_path / "sessions" / "z.jsonl")
    active_a = write_rollout(tmp_path / "sessions" / "a.jsonl")
    archived = write_rollout(tmp_path / "archived_sessions" / "a.jsonl")
    for path in (active_z, active_a, archived):
        os.utime(path, ns=(1, 1))

    assert session_files(True) == [active_a, active_z, archived]


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


def test_complete_blank_jsonl_lines_are_ignored_without_warnings(tmp_path: Path) -> None:
    path = tmp_path / "blank-lines.jsonl"
    path.write_text("\n \t\n{}\n", encoding="utf-8")

    entries, warnings = read_jsonl(path)

    assert entries == [(3, {})]
    assert warnings == []


def test_incomplete_utf8_tail_is_deferred(tmp_path: Path) -> None:
    path = tmp_path / "active.jsonl"
    path.write_bytes(b'{}\n{"value":"\xe4\xb8')

    entries, warnings = read_jsonl(path)

    assert entries == [(1, {})]
    assert warnings == []


def test_complete_json_without_terminating_newline_is_deferred(tmp_path: Path) -> None:
    path = tmp_path / "active-valid-tail.jsonl"
    path.write_bytes(b'{}\n{"value":1}')

    entries, warnings = read_jsonl(path)

    assert entries == [(1, {})]
    assert warnings == []


def test_oversized_and_nonstandard_json_lines_are_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "MAX_JSONL_LINE_BYTES", 32)
    path = tmp_path / "hostile.jsonl"
    path.write_bytes(
        b'{"value":"' + b"x" * 40 + b'"}\n{"value":NaN}\n{"duplicate":1,"duplicate":2}\n{}\n'
    )

    entries, warnings = read_jsonl(path)

    assert entries == [(4, {})]
    assert [warning["code"] for warning in warnings] == [
        "oversized_jsonl",
        "malformed_jsonl",
        "malformed_jsonl",
    ]


def test_rejected_paginated_lines_contribute_bounded_gap_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "MAX_JSONL_LINE_BYTES", 32)
    path = tmp_path / "rejected-lines.jsonl"
    path.write_bytes(b"x" * 40 + b"\n\xff\n[]\n{bad}\n")
    warnings: list[dict[str, object]] = []
    state = _JsonlReadState()

    assert list(iter_jsonl(path, warnings, _state=state)) == []
    assert state.pending_rejected_lines == 4
    assert [warning["code"] for warning in warnings] == [
        "oversized_jsonl",
        "malformed_utf8",
        "non_object_jsonl",
        "malformed_jsonl",
    ]


def test_jsonl_rejects_invalid_and_oversized_partial_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "boundary.jsonl"
    path.write_bytes(b"x" * 20 + b"\n")
    monkeypatch.setattr(sessions, "MAX_JSONL_LINE_BYTES", 8)

    with pytest.raises(ValueError, match="Invalid paginated"):
        list(iter_jsonl(path, end_byte_offset=True))
    with pytest.raises(ValueError, match="Invalid paginated"):
        list(iter_jsonl(path, end_byte_offset=-1))
    with pytest.raises(ValueError, match="splits"):
        list(iter_jsonl(path, end_byte_offset=10))
    with pytest.raises(ValueError, match="terminating newline"):
        list(iter_jsonl(path, end_byte_offset=9))
    with pytest.raises(OSError):
        list(iter_jsonl(tmp_path / "missing.jsonl", end_byte_offset=1))


@pytest.mark.parametrize(
    "value",
    [
        None,
        {"thread_id": "not-a-uuid", "end_ordinal_exclusive": 1, "end_byte_offset": 1},
        {
            "thread_id": "11111111-1111-4111-8111-111111111111",
            "end_ordinal_exclusive": True,
            "end_byte_offset": 1,
        },
        {
            "thread_id": "11111111-1111-4111-8111-111111111111",
            "end_ordinal_exclusive": 1,
            "end_byte_offset": 0,
        },
    ],
)
def test_paginated_history_position_rejects_malformed_fields(value: object) -> None:
    with pytest.raises(ValueError):
        _history_position(value)


def test_jsonl_rejects_resource_intensive_numeric_literals(tmp_path: Path) -> None:
    path = tmp_path / "numeric-limits.jsonl"
    path.write_text(
        '{"value":' + "9" * 257 + '}\n{"value":1e400}\n{}\n',
        encoding="utf-8",
    )

    entries, warnings = read_jsonl(path)

    assert entries == [(3, {})]
    assert [warning["code"] for warning in warnings] == [
        "malformed_jsonl",
        "malformed_jsonl",
    ]


def test_jsonl_rejects_excessive_nesting_and_continues(tmp_path: Path) -> None:
    path = tmp_path / "nested.jsonl"
    path.write_text(
        "[" * (MAX_JSON_NESTING_DEPTH + 1) + "0" + "]" * (MAX_JSON_NESTING_DEPTH + 1) + "\n{}\n",
        encoding="utf-8",
    )

    entries, warnings = read_jsonl(path)

    assert entries == [(2, {})]
    assert [warning["code"] for warning in warnings] == ["malformed_jsonl"]


@pytest.mark.parametrize(
    ("budget_name", "limit", "expected"),
    [
        ("MAX_JSONL_LINES", 2, "total JSONL line limit"),
        ("MAX_JSONL_TOTAL_BYTES", 5, "total JSONL byte limit"),
    ],
)
def test_jsonl_iteration_enforces_total_file_budgets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    budget_name: str,
    limit: int,
    expected: str,
) -> None:
    path = tmp_path / "bounded-total.jsonl"
    path.write_bytes(b"{}\n{}\n{}\n" if budget_name == "MAX_JSONL_LINES" else b"{}\n{}\n")
    monkeypatch.setattr(sessions, budget_name, limit)

    with pytest.raises(ValueError, match=expected):
        list(iter_jsonl(path))


def test_complete_invalid_utf8_line_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "invalid-utf8.jsonl"
    path.write_bytes(b"\xff\n{}\n")

    entries, warnings = read_jsonl(path)

    assert entries == [(2, {})]
    assert warnings == [
        {
            "code": "malformed_utf8",
            "line": 1,
            "message": "Skipped malformed UTF-8 JSONL line 1.",
        }
    ]


def test_default_home_and_missing_roots(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    assert resolve_codex_home().name == ".codex"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    assert session_roots(False) == [tmp_path / "missing" / "sessions"]
    assert session_files(False) == []


def test_archive_classification_uses_the_configured_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configured_home = tmp_path / "archived_sessions" / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(configured_home))
    active = write_rollout(configured_home / "sessions" / "active.jsonl")
    archived = write_rollout(configured_home / "archived_sessions" / "archived.jsonl")

    assert is_archived_session(active) is False
    assert is_archived_session(archived) is True


def test_jsonl_byte_boundaries_must_end_after_a_complete_line(tmp_path: Path) -> None:
    path = tmp_path / "bounded.jsonl"
    first = b'{"ordinal":0}\n'
    second = b'{"ordinal":1}\n'
    path.write_bytes(first + second)

    assert list(iter_jsonl(path, end_byte_offset=len(first))) == [(1, {"ordinal": 0})]
    with pytest.raises(ValueError, match="splits"):
        list(iter_jsonl(path, end_byte_offset=len(first) + 1))
    with pytest.raises(ValueError, match="past"):
        list(iter_jsonl(path, end_byte_offset=path.stat().st_size + 1))

    incomplete = tmp_path / "incomplete.jsonl"
    incomplete.write_bytes(b'{"ordinal":0}')
    with pytest.raises(ValueError, match="newline"):
        list(iter_jsonl(incomplete, end_byte_offset=incomplete.stat().st_size))


def _paginated_line(ordinal: int, entry_type: str, payload: dict[str, object]) -> str:
    return (
        json.dumps(
            {
                "timestamp": f"2026-08-16T00:00:{ordinal:02d}Z",
                "ordinal": ordinal,
                "type": entry_type,
                "payload": payload,
            }
        )
        + "\n"
    )


def test_reverted_paginated_lineage_separates_thread_and_rollout_identities(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    thread_id = "11111111-1111-4111-8111-111111111111"
    reverted_rollout_id = "22222222-2222-4222-8222-222222222222"
    child_id = "33333333-3333-4333-8333-333333333333"
    directory = tmp_path / "sessions"
    directory.mkdir()

    root = directory / f"rollout-2026-08-24T00-00-00-{thread_id}.jsonl"
    root_lines = [
        _paginated_line(0, "session_meta", {"id": thread_id, "history_mode": "paginated"}),
        _paginated_line(1, "event_msg", {"type": "turn_started", "turn_id": "root"}),
        _paginated_line(2, "event_msg", {"type": "turn_complete", "turn_id": "root"}),
    ]
    root.write_text("".join(root_lines), encoding="utf-8")

    reverted = directory / (f"rollout-2026-08-24T00-01-00-{thread_id}_{reverted_rollout_id}.jsonl")
    reverted_lines = [
        _paginated_line(
            3,
            "session_meta",
            {
                "id": thread_id,
                "history_mode": "paginated",
                "history_base": {
                    "thread_id": thread_id,
                    "end_ordinal_exclusive": 3,
                    "end_byte_offset": root.stat().st_size,
                },
            },
        ),
        _paginated_line(4, "event_msg", {"type": "turn_started", "turn_id": "revert"}),
    ]
    reverted.write_text("".join(reverted_lines), encoding="utf-8")

    child = directory / f"rollout-2026-08-24T00-02-00-{child_id}.jsonl"
    child.write_text(
        _paginated_line(
            5,
            "session_meta",
            {
                "id": child_id,
                "history_mode": "paginated",
                "history_base": {
                    "thread_id": reverted_rollout_id,
                    "end_ordinal_exclusive": 5,
                    "end_byte_offset": reverted.stat().st_size,
                },
            },
        )
        + _paginated_line(6, "event_msg", {"type": "turn_started", "turn_id": "child"}),
        encoding="utf-8",
    )

    assert rollout_id_from_path(reverted) == reverted_rollout_id
    assert first_session_metadata(reverted)["id"] == thread_id
    assert [segment.path for segment in rollout_lineage(child)] == [root, reverted, child]
    assert [entry["ordinal"] for _, entry in iter_session_jsonl(child)] == [1, 2, 4, 6]


def test_paginated_session_metadata_must_be_the_head_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    rollout_id = "34343434-3434-4434-8434-343434343434"
    path = tmp_path / "sessions" / f"rollout-late-metadata-{rollout_id}.jsonl"
    path.parent.mkdir()
    path.write_text(
        _paginated_line(0, "event_msg", {"type": "turn_started"})
        + _paginated_line(
            1,
            "session_meta",
            {"id": rollout_id, "history_mode": "paginated"},
        ),
        encoding="utf-8",
    )

    assert first_session_metadata(path) == {}
    with pytest.raises(ValueError, match="leading record"):
        rollout_lineage(path)


def test_paginated_shape_probe_checks_past_the_first_valid_object(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    rollout_id = "35353535-3535-4535-8535-353535353535"
    path = tmp_path / "sessions" / f"rollout-damaged-{rollout_id}.jsonl"
    path.parent.mkdir()
    path.write_text(
        "{}\n" + _paginated_line(1, "event_msg", {"type": "turn_started"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no valid paginated session metadata"):
        rollout_lineage(path)


@pytest.mark.parametrize(
    ("budget_name", "limit", "prefix"),
    [
        ("MAX_SESSION_HEADER_LINES", 2, "{}\n{}\n"),
        ("MAX_SESSION_HEADER_BYTES", 3, "{}\n"),
    ],
)
def test_paginated_probe_budget_exhaustion_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    budget_name: str,
    limit: int,
    prefix: str,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(sessions, budget_name, limit)
    rollout_id = "36363636-3636-4636-8636-363636363636"
    path = tmp_path / "sessions" / f"rollout-probe-budget-{rollout_id}.jsonl"
    path.parent.mkdir()
    path.write_text(
        prefix + _paginated_line(2, "event_msg", {"type": "turn_started"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no valid paginated session metadata"):
        list(iter_session_jsonl(path))


def test_paginated_metadata_beyond_probe_budget_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sessions, "MAX_SESSION_HEADER_LINES", 2)
    path = tmp_path / "late-paginated-metadata.jsonl"
    path.write_text(
        "{}\n{}\n"
        + json.dumps(
            {
                "type": "session_meta",
                "payload": {"history_mode": "paginated"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no valid paginated session metadata"):
        list(iter_session_jsonl(path))


def test_paginated_lineage_uses_declared_ordinal_and_byte_prefixes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    root_id = "11111111-1111-4111-8111-111111111111"
    child_id = "22222222-2222-4222-8222-222222222222"
    directory = tmp_path / "sessions" / "2026" / "08"
    directory.mkdir(parents=True)
    root_path = directory / f"rollout-2026-08-16T00-00-00-{root_id}.jsonl"
    root_lines = [
        _paginated_line(
            0,
            "session_meta",
            {
                "id": root_id,
                "session_id": "shared-session",
                "history_mode": "paginated",
            },
        ),
        _paginated_line(1, "event_msg", {"type": "task_started", "turn_id": "root"}),
        _paginated_line(
            2,
            "event_msg",
            {
                "type": "item_completed",
                "turn_id": "root",
                "completed_at_ms": 1_786_665_602_000,
                "item": {"type": "UserMessage", "id": "root-user", "content": []},
            },
        ),
        _paginated_line(3, "event_msg", {"type": "task_complete", "turn_id": "root"}),
        _paginated_line(4, "event_msg", {"type": "task_started", "turn_id": "excluded"}),
    ]
    root_path.write_bytes("".join(root_lines).encode("utf-8"))
    root_boundary = len("".join(root_lines[:4]).encode())

    child_path = directory / f"rollout-2026-08-16T00-01-00-{child_id}.jsonl"
    child_lines = [
        _paginated_line(
            4,
            "session_meta",
            {
                "id": child_id,
                "session_id": "shared-session",
                "history_mode": "paginated",
                "history_base": {
                    "thread_id": root_id,
                    "end_ordinal_exclusive": 4,
                    "end_byte_offset": root_boundary,
                },
            },
        ),
        _paginated_line(5, "event_msg", {"type": "task_started", "turn_id": "child"}),
        _paginated_line(6, "event_msg", {"type": "task_complete", "turn_id": "child"}),
    ]
    child_path.write_text("".join(child_lines), encoding="utf-8")

    assert first_session_metadata(child_path)["id"] == child_id
    assert [(segment.path, segment.start_ordinal) for segment in rollout_lineage(child_path)] == [
        (root_path, 1),
        (child_path, 5),
    ]
    entries = [entry for _, entry in iter_session_jsonl(child_path)]
    assert [entry["ordinal"] for entry in entries] == [1, 2, 3, 5, 6]
    assert all(entry.get("payload", {}).get("turn_id") != "excluded" for entry in entries)


def test_compressed_history_source_uses_decompressed_logical_byte_offsets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    root_id = "39393939-3939-4939-8939-393939393939"
    child_id = "40404040-4040-4040-8040-404040404040"
    directory = tmp_path / "sessions"
    directory.mkdir()
    root = directory / f"rollout-compressed-root-{root_id}.jsonl.zst"
    root_jsonl = (
        _paginated_line(0, "session_meta", {"id": root_id, "history_mode": "paginated"})
        + _paginated_line(1, "event_msg", {"type": "turn_started"})
    ).encode()
    root.write_bytes(_zstd_compress(root_jsonl))
    child = directory / f"rollout-compressed-child-{child_id}.jsonl"
    child.write_text(
        _paginated_line(
            2,
            "session_meta",
            {
                "id": child_id,
                "history_mode": "paginated",
                "history_base": {
                    "thread_id": root_id,
                    "end_ordinal_exclusive": 2,
                    "end_byte_offset": len(root_jsonl),
                },
            },
        )
        + _paginated_line(3, "event_msg", {"type": "turn_started"}),
        encoding="utf-8",
    )

    assert [segment.path for segment in rollout_lineage(child)] == [root, child]
    assert [entry["ordinal"] for _, entry in iter_session_jsonl(child)] == [1, 3]


def test_paginated_lineage_shares_full_parse_budget_across_segments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    root_id = "37373737-3737-4737-8737-373737373737"
    child_id = "38383838-3838-4838-8838-383838383838"
    directory = tmp_path / "sessions"
    directory.mkdir()
    root = directory / f"rollout-budget-root-{root_id}.jsonl"
    root.write_text(
        _paginated_line(0, "session_meta", {"id": root_id, "history_mode": "paginated"})
        + _paginated_line(1, "event_msg", {"type": "turn_started"}),
        encoding="utf-8",
    )
    child = directory / f"rollout-budget-child-{child_id}.jsonl"
    child.write_text(
        _paginated_line(
            2,
            "session_meta",
            {
                "id": child_id,
                "history_mode": "paginated",
                "history_base": {
                    "thread_id": root_id,
                    "end_ordinal_exclusive": 2,
                    "end_byte_offset": root.stat().st_size,
                },
            },
        )
        + _paginated_line(3, "event_msg", {"type": "turn_started"}),
        encoding="utf-8",
    )

    monkeypatch.setattr(sessions, "MAX_JSONL_LINES", 3)
    with pytest.raises(ValueError, match="total JSONL line limit"):
        list(iter_session_jsonl(child))

    monkeypatch.setattr(sessions, "MAX_JSONL_LINES", 1_000_000)
    monkeypatch.setattr(
        sessions,
        "MAX_JSONL_TOTAL_BYTES",
        max(root.stat().st_size, child.stat().st_size) + 1,
    )
    with pytest.raises(ValueError, match="total JSONL byte limit"):
        list(iter_session_jsonl(child))


def test_paginated_lineage_rejects_missing_cycle_and_inconsistent_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    directory = tmp_path / "sessions"
    directory.mkdir()
    first_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    second_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    missing_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"

    missing = directory / f"rollout-2026-08-16T00-00-00-{first_id}.jsonl"
    missing.write_text(
        _paginated_line(
            1,
            "session_meta",
            {
                "id": first_id,
                "history_mode": "paginated",
                "history_base": {
                    "thread_id": missing_id,
                    "end_ordinal_exclusive": 1,
                    "end_byte_offset": 1,
                },
            },
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not found"):
        rollout_lineage(missing)
    missing.unlink()

    first = directory / f"rollout-2026-08-16T00-01-00-{first_id}.jsonl"
    second = directory / f"rollout-2026-08-16T00-02-00-{second_id}.jsonl"
    first.write_text(
        _paginated_line(
            1,
            "session_meta",
            {
                "id": first_id,
                "history_mode": "paginated",
                "history_base": {
                    "thread_id": second_id,
                    "end_ordinal_exclusive": 1,
                    "end_byte_offset": 1,
                },
            },
        ),
        encoding="utf-8",
    )
    second.write_text(
        _paginated_line(
            1,
            "session_meta",
            {
                "id": second_id,
                "history_mode": "paginated",
                "history_base": {
                    "thread_id": first_id,
                    "end_ordinal_exclusive": 1,
                    "end_byte_offset": 1,
                },
            },
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="cycle"):
        rollout_lineage(first)

    root_id = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
    child_id = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
    root = directory / f"rollout-2026-08-16T00-03-00-{root_id}.jsonl"
    root_line = _paginated_line(0, "session_meta", {"id": root_id, "history_mode": "paginated"})
    root.write_text(root_line, encoding="utf-8")
    child = directory / f"rollout-2026-08-16T00-04-00-{child_id}.jsonl"
    child.write_text(
        _paginated_line(
            1,
            "session_meta",
            {
                "id": child_id,
                "history_mode": "paginated",
                "history_base": {
                    "thread_id": root_id,
                    "end_ordinal_exclusive": 1,
                    "end_byte_offset": len(root_line.encode()) - 1,
                },
            },
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="splits"):
        list(iter_session_jsonl(child))


def test_paginated_lineage_has_a_bounded_segment_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(sessions, "MAX_LINEAGE_SEGMENTS", 2)
    directory = tmp_path / "sessions"
    directory.mkdir()
    root_id = "61616161-6161-4161-8161-616161616161"
    middle_id = "62626262-6262-4262-8262-626262626262"
    child_id = "63636363-6363-4363-8363-636363636363"
    root = directory / f"rollout-2026-08-16T00-00-00-{root_id}.jsonl"
    root.write_text(
        _paginated_line(0, "session_meta", {"id": root_id, "history_mode": "paginated"}),
        encoding="utf-8",
    )
    middle = directory / f"rollout-2026-08-16T00-01-00-{middle_id}.jsonl"
    middle.write_text(
        _paginated_line(
            1,
            "session_meta",
            {
                "id": middle_id,
                "history_mode": "paginated",
                "history_base": {
                    "thread_id": root_id,
                    "end_ordinal_exclusive": 1,
                    "end_byte_offset": root.stat().st_size,
                },
            },
        ),
        encoding="utf-8",
    )
    child = directory / f"rollout-2026-08-16T00-02-00-{child_id}.jsonl"
    child.write_text(
        _paginated_line(
            2,
            "session_meta",
            {
                "id": child_id,
                "history_mode": "paginated",
                "history_base": {
                    "thread_id": middle_id,
                    "end_ordinal_exclusive": 2,
                    "end_byte_offset": middle.stat().st_size,
                },
            },
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="segment limit"):
        rollout_lineage(child)


def test_paginated_lineage_rejects_ambiguous_source_and_invalid_source_shapes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    directory = tmp_path / "sessions"
    archive = tmp_path / "archived_sessions"
    directory.mkdir()
    archive.mkdir()
    source_id = "71717171-7171-4171-8171-717171717171"
    child_id = "72727272-7272-4272-8272-727272727272"
    source_line = _paginated_line(0, "session_meta", {"id": source_id, "history_mode": "paginated"})
    (directory / f"rollout-source-a-{source_id}.jsonl").write_text(source_line, encoding="utf-8")
    (archive / f"rollout-source-b-{source_id}.jsonl").write_text(source_line, encoding="utf-8")
    (directory / "legacy-without-physical-id.jsonl").write_text("{}\n", encoding="utf-8")
    child = directory / f"rollout-child-{child_id}.jsonl"
    child.write_text(
        _paginated_line(
            1,
            "session_meta",
            {
                "id": child_id,
                "history_mode": "paginated",
                "history_base": {
                    "thread_id": source_id,
                    "end_ordinal_exclusive": 1,
                    "end_byte_offset": len(source_line.encode()),
                },
            },
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="ambiguous"):
        rollout_lineage(child)

    for duplicate in (
        directory / f"rollout-source-a-{source_id}.jsonl",
        archive / f"rollout-source-b-{source_id}.jsonl",
    ):
        duplicate.unlink()
    legacy_source = directory / f"rollout-source-{source_id}.jsonl"
    legacy_source.write_text(
        _paginated_line(0, "session_meta", {"id": source_id, "history_mode": "legacy"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="different history mode"):
        rollout_lineage(child)

    no_filename_id = directory / "paginated-without-id-in-name.jsonl"
    no_filename_id.write_text(
        _paginated_line(0, "session_meta", {"id": source_id, "history_mode": "paginated"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="filename does not contain"):
        rollout_lineage(no_filename_id)


def test_paginated_iteration_rejects_invalid_ordinals_and_boundary_disagreement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    directory = tmp_path / "sessions"
    directory.mkdir()
    invalid_id = "73737373-7373-4373-8373-737373737373"
    invalid = directory / f"rollout-invalid-{invalid_id}.jsonl"
    invalid.write_text(
        _paginated_line(0, "session_meta", {"id": invalid_id, "history_mode": "paginated"})
        + json.dumps(
            {
                "timestamp": "2026-08-16T00:00:01Z",
                "ordinal": True,
                "type": "event_msg",
                "payload": {"type": "turn_started"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="no valid ordinal"):
        list(iter_session_jsonl(invalid))

    source_id = "74747474-7474-4474-8474-747474747474"
    child_id = "75757575-7575-4575-8575-757575757575"
    source = directory / f"rollout-source-{source_id}.jsonl"
    source_lines = [
        _paginated_line(0, "session_meta", {"id": source_id, "history_mode": "paginated"}),
        _paginated_line(1, "event_msg", {"type": "turn_started"}),
    ]
    source.write_bytes("".join(source_lines).encode("utf-8"))
    child = directory / f"rollout-child-{child_id}.jsonl"

    def write_child(end_ordinal: int) -> None:
        child.write_text(
            _paginated_line(
                end_ordinal,
                "session_meta",
                {
                    "id": child_id,
                    "history_mode": "paginated",
                    "history_base": {
                        "thread_id": source_id,
                        "end_ordinal_exclusive": end_ordinal,
                        "end_byte_offset": source.stat().st_size,
                    },
                },
            ),
            encoding="utf-8",
        )

    write_child(1)
    with pytest.raises(ValueError, match="ordinal and byte boundaries disagree"):
        list(iter_session_jsonl(child))
    source.write_text(source_lines[0], encoding="utf-8")
    write_child(2)
    with pytest.raises(ValueError, match="does not end"):
        list(iter_session_jsonl(child))


def test_empty_legacy_rollout_has_a_single_physical_segment(tmp_path: Path) -> None:
    path = tmp_path / "empty.jsonl"
    path.write_bytes(b"")

    assert rollout_lineage(path) == [sessions.RolloutSegment(path=path, start_ordinal=0)]


def test_paginated_rollout_rejects_noncontiguous_ordinals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    rollout_id = "ffffffff-ffff-4fff-8fff-ffffffffffff"
    path = tmp_path / "sessions" / f"rollout-2026-08-16T00-00-00-{rollout_id}.jsonl"
    path.parent.mkdir()
    path.write_text(
        _paginated_line(
            0,
            "session_meta",
            {"id": rollout_id, "history_mode": "paginated"},
        )
        + _paginated_line(2, "event_msg", {"type": "turn_started", "turn_id": "turn"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="not contiguous"):
        list(iter_session_jsonl(path))


def test_paginated_rollout_does_not_fall_back_to_legacy_when_metadata_is_damaged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    rollout_id = "64646464-6464-4464-8464-646464646464"
    path = tmp_path / "sessions" / f"rollout-2026-08-16T00-00-00-{rollout_id}.jsonl"
    path.parent.mkdir()
    path.write_text(
        "{damaged session metadata}\n"
        + _paginated_line(1, "event_msg", {"type": "turn_started", "turn_id": "copied"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="no valid paginated session metadata"):
        list(iter_session_jsonl(path))


def test_paginated_rollout_allows_rejected_line_to_cover_one_ordinal_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    rollout_id = "67676767-6767-4767-8767-676767676767"
    path = tmp_path / "sessions" / f"rollout-2026-08-16T00-00-00-{rollout_id}.jsonl"
    path.parent.mkdir()
    path.write_text(
        _paginated_line(
            0,
            "session_meta",
            {"id": rollout_id, "history_mode": "paginated"},
        )
        + "{malformed complete record}\n"
        + _paginated_line(2, "event_msg", {"type": "turn_started", "turn_id": "turn"}),
        encoding="utf-8",
    )
    warnings: list[dict[str, object]] = []

    entries = [entry for _, entry in iter_session_jsonl(path, warnings)]

    assert [entry["ordinal"] for entry in entries] == [2]
    assert [warning["code"] for warning in warnings] == ["malformed_jsonl"]


def test_paginated_rollout_rejects_wrong_initial_ordinal_and_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    rollout_id = "abababab-abab-4bab-8bab-abababababab"
    path = tmp_path / "sessions" / f"rollout-2026-08-16T00-00-00-{rollout_id}.jsonl"
    path.parent.mkdir()
    path.write_text(
        _paginated_line(
            1,
            "session_meta",
            {"id": rollout_id, "history_mode": "paginated"},
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unexpected ordinal"):
        list(iter_session_jsonl(path))

    path.write_text(
        _paginated_line(
            0,
            "session_meta",
            {
                "id": "cdcdcdcd-cdcd-4dcd-8dcd-cdcdcdcdcdcd",
                "history_mode": "paginated",
            },
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="filename identity"):
        rollout_lineage(path)


def test_paginated_subagent_excludes_inherited_model_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    rollout_id = "12121212-1212-4212-8212-121212121212"
    path = tmp_path / "sessions" / f"rollout-2026-08-16T00-00-00-{rollout_id}.jsonl"
    path.parent.mkdir()
    path.write_text(
        _paginated_line(
            0,
            "session_meta",
            {
                "id": rollout_id,
                "history_mode": "paginated",
                "subagent_history_start_ordinal": 4,
            },
        )
        + _paginated_line(1, "event_msg", {"type": "turn_started", "turn_id": "parent"})
        + _paginated_line(
            2,
            "event_msg",
            {
                "type": "item_completed",
                "turn_id": "parent",
                "item": {"type": "UserMessage", "id": "parent-user", "content": []},
            },
        )
        + _paginated_line(3, "event_msg", {"type": "turn_complete", "turn_id": "parent"})
        + _paginated_line(4, "event_msg", {"type": "turn_started", "turn_id": "child"})
        + _paginated_line(
            5,
            "event_msg",
            {
                "type": "item_completed",
                "turn_id": "child",
                "item": {"type": "UserMessage", "id": "child-user", "content": []},
            },
        ),
        encoding="utf-8",
    )

    entries = [entry for _, entry in iter_session_jsonl(path)]

    assert [entry["ordinal"] for entry in entries] == [4, 5]


def test_paginated_lineage_applies_each_inherited_subagent_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    source_id = "89898989-8989-4989-8989-898989898989"
    child_id = "90909090-9090-4090-8090-909090909090"
    directory = tmp_path / "sessions"
    directory.mkdir()
    source = directory / f"rollout-2026-08-16T00-00-00-{source_id}.jsonl"
    source_lines = [
        _paginated_line(
            0,
            "session_meta",
            {
                "id": source_id,
                "history_mode": "paginated",
                "subagent_history_start_ordinal": 4,
            },
        ),
        _paginated_line(1, "event_msg", {"type": "turn_started", "turn_id": "parent"}),
        _paginated_line(2, "event_msg", {"type": "turn_complete", "turn_id": "parent"}),
        _paginated_line(3, "event_msg", {"type": "turn_started", "turn_id": "copied"}),
        _paginated_line(4, "event_msg", {"type": "turn_started", "turn_id": "source"}),
        _paginated_line(5, "event_msg", {"type": "turn_complete", "turn_id": "source"}),
        _paginated_line(6, "event_msg", {"type": "shutdown_complete"}),
    ]
    source.write_bytes("".join(source_lines).encode("utf-8"))
    child = directory / f"rollout-2026-08-16T00-01-00-{child_id}.jsonl"
    child.write_text(
        _paginated_line(
            7,
            "session_meta",
            {
                "id": child_id,
                "history_mode": "paginated",
                "history_base": {
                    "thread_id": source_id,
                    "end_ordinal_exclusive": 7,
                    "end_byte_offset": len("".join(source_lines).encode()),
                },
            },
        )
        + _paginated_line(8, "event_msg", {"type": "turn_started", "turn_id": "child"}),
        encoding="utf-8",
    )

    entries = [entry for _, entry in iter_session_jsonl(child)]

    assert [entry["ordinal"] for entry in entries] == [4, 5, 6, 8]


@pytest.mark.parametrize("boundary", [True, 0, -1, "4"])
def test_paginated_subagent_rejects_invalid_history_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: object
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    rollout_id = "56565656-5656-4656-8656-565656565656"
    path = tmp_path / "sessions" / f"rollout-2026-08-16T00-00-00-{rollout_id}.jsonl"
    path.parent.mkdir()
    path.write_text(
        _paginated_line(
            0,
            "session_meta",
            {
                "id": rollout_id,
                "history_mode": "paginated",
                "subagent_history_start_ordinal": boundary,
            },
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="subagent history"):
        list(iter_session_jsonl(path))


def test_jsonl_diagnostics_are_bounded(tmp_path: Path) -> None:
    path = tmp_path / "many-errors.jsonl"
    path.write_text("{bad}\n" * 150, encoding="utf-8")

    _, warnings = read_jsonl(path)

    assert len(warnings) == 100

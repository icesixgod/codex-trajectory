"""Session-root safety tests."""

from __future__ import annotations

import json
import os
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


def test_session_file_safety_rejects_paths_outside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "sessions"
    root.mkdir()
    outside = write_rollout(tmp_path / "outside.jsonl")

    assert is_safe_session_file(outside, root) is False
    assert rollout_id_from_path(tmp_path / "not-a-rollout-id.jsonl") is None


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
    root_path.write_text("".join(root_lines), encoding="utf-8")
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
    source.write_text("".join(source_lines), encoding="utf-8")
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
    source.write_text("".join(source_lines), encoding="utf-8")
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

"""Coverage for the private session search metadata index."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from codex_trajectory import projection, session_index, sessions
from codex_trajectory.projection import list_session_overviews, session_search_fields
from codex_trajectory.session_index import SessionSearchIndex, search_index_path
from codex_trajectory.sessions import (
    RolloutSegment,
    _iter_search_lines,
    iter_session_search_jsonl,
    session_signature,
)
from conftest import rollout_events, write_rollout


def test_search_index_persists_only_bounded_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    events = rollout_events("indexed-session")
    events[2]["payload"]["message"] = "Indexed synthetic title"
    path = write_rollout(tmp_path / "sessions" / "rollout-indexed.jsonl", events)

    listed = list_session_overviews(
        limit=1,
        query="Indexed synthetic title",
        include_archived=False,
    )

    assert [item["id"] for item in listed] == ["indexed-session"]
    index_path = search_index_path()
    raw = index_path.read_text(encoding="utf-8")
    value = json.loads(raw)
    assert value["schemaVersion"] == 1
    assert len(value["entries"]) == 1
    assert "Indexed synthetic title" in raw
    assert "indexed-session" in raw
    assert str(tmp_path) not in raw
    assert "private system instructions" not in raw
    assert "secret-tool-input" not in raw
    assert "secret-tool-output" not in raw
    assert "opaque-secret-reasoning" not in raw
    if os.name != "nt":
        assert stat.S_IMODE(index_path.stat().st_mode) == 0o600
    assert path.exists()
    assert SessionSearchIndex().lookup(path, session_signature(path)) == session_search_fields(path)


def test_search_index_invalidates_after_rollout_rewrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    path = tmp_path / "sessions" / "rollout-changing.jsonl"
    first = rollout_events("changing-session")
    first[2]["payload"]["message"] = "Old indexed title"
    write_rollout(path, first)
    assert list_session_overviews(query="Old indexed title", include_archived=False)

    second = rollout_events("changing-session")
    second[2]["payload"]["message"] = "New and longer indexed title"
    write_rollout(path, second)
    os.utime(path, ns=(2_000_000_000, 2_000_000_000))
    projection._SESSION_OVERVIEW_CACHE.clear()

    assert list_session_overviews(query="Old indexed title", include_archived=False) == []
    refreshed = list_session_overviews(query="New and longer indexed title", include_archived=False)
    assert [item["id"] for item in refreshed] == ["changing-session"]


def test_search_index_ignores_invalid_and_linked_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    path = write_rollout(
        tmp_path / "sessions" / "rollout-safe.jsonl",
        rollout_events("safe-session"),
    )
    index_path = search_index_path()
    index_path.parent.mkdir(parents=True)
    index_path.write_text("not-json", encoding="utf-8")

    fields = session_search_fields(path)
    index = SessionSearchIndex()
    signature = session_signature(path)
    assert index.lookup(path, signature) is None
    index.store(path, signature, fields)
    index.flush()
    assert index.lookup(path, signature) == fields

    index_path.unlink()
    outside = tmp_path / "outside-index.json"
    outside.write_text("do not replace", encoding="utf-8")
    try:
        index_path.symlink_to(outside)
    except OSError as error:
        if os.name == "nt" and error.winerror == 1314:
            pytest.skip("Windows symlink privilege is unavailable")
        raise
    linked = SessionSearchIndex()
    linked.store(path, signature, fields)
    linked.flush()
    assert outside.read_text(encoding="utf-8") == "do not replace"
    assert index_path.is_symlink()


@pytest.mark.parametrize(
    "value",
    [
        [],
        {"schemaVersion": 2, "entries": {}},
        {"schemaVersion": 1, "entries": []},
        {"schemaVersion": 1, "entries": {"not-a-digest": {}}},
        {
            "schemaVersion": 1,
            "entries": {
                "0" * 64: {
                    "signature": [["0" * 64, 1, 1, 1]],
                    "id": "",
                    "title": "invalid",
                    "cwd": None,
                    "model": None,
                }
            },
        },
    ],
)
def test_search_index_rejects_invalid_document_shapes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    value: object,
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    path = write_rollout(
        tmp_path / "sessions" / "rollout-invalid-index.jsonl",
        rollout_events("invalid-index-session"),
    )
    index_path = search_index_path()
    index_path.parent.mkdir(parents=True)
    index_path.write_text(json.dumps(value), encoding="utf-8")

    assert SessionSearchIndex().lookup(path, session_signature(path)) is None


def test_search_index_evicts_old_entries_and_rejects_invalid_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(session_index, "MAX_INDEX_ENTRIES", 1)
    first = write_rollout(
        tmp_path / "sessions" / "rollout-first.jsonl",
        rollout_events("first-index-session"),
    )
    second = write_rollout(
        tmp_path / "sessions" / "rollout-second.jsonl",
        rollout_events("second-index-session"),
    )
    index = SessionSearchIndex()
    first_fields = session_search_fields(first)
    second_fields = session_search_fields(second)

    with pytest.raises(ValueError, match="Invalid session search metadata"):
        index.store(first, session_signature(first), {**first_fields, "id": ""})
    index.store(first, session_signature(first), first_fields)
    index.store(second, session_signature(second), second_fields)

    assert index.lookup(first, session_signature(first)) is None
    assert index.lookup(second, session_signature(second)) == second_fields
    index.flush()
    reloaded = SessionSearchIndex()
    assert reloaded.lookup(first, session_signature(first)) is None
    assert reloaded.lookup(second, session_signature(second)) == second_fields


def test_search_index_fails_closed_when_the_serialized_bound_is_impossible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(session_index, "MAX_INDEX_BYTES", 1)
    path = write_rollout(
        tmp_path / "sessions" / "rollout-too-large.jsonl",
        rollout_events("too-large-index-session"),
    )
    index = SessionSearchIndex()
    index.store(path, session_signature(path), session_search_fields(path))

    index.flush()

    assert not search_index_path().exists()


def test_search_index_refuses_a_hardlinked_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    path = write_rollout(
        tmp_path / "sessions" / "rollout-hardlinked-index.jsonl",
        rollout_events("hardlinked-index-session"),
    )
    index_path = search_index_path()
    index_path.parent.mkdir(parents=True)
    outside = tmp_path / "outside-hardlinked-index.json"
    outside.write_text("do not replace", encoding="utf-8")
    try:
        os.link(outside, index_path)
    except OSError:
        pytest.skip("hardlinks are unavailable on this filesystem")
    index = SessionSearchIndex()
    index.store(path, session_signature(path), session_search_fields(path))

    index.flush()

    assert outside.read_text(encoding="utf-8") == "do not replace"
    assert index_path.samefile(outside)


def test_search_index_refuses_a_linked_state_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    codex_home = tmp_path / "home"
    codex_home.mkdir()
    outside = tmp_path / "outside-state"
    outside.mkdir()
    try:
        (codex_home / "codex-trajectory").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable on this filesystem")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    path = write_rollout(
        codex_home / "sessions" / "rollout-linked-directory.jsonl",
        rollout_events("linked-directory-session"),
    )
    index = SessionSearchIndex()
    index.store(path, session_signature(path), session_search_fields(path))

    index.flush()

    assert list(outside.iterdir()) == []


def test_lightweight_search_decodes_only_metadata_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = rollout_events("lightweight-session")
    events[2]["payload"]["message"] = "Lightweight title"
    for index in range(200):
        events.append(
            {
                "timestamp": f"2026-08-14T00:01:{index % 60:02d}Z",
                "type": "future_unknown_event",
                "payload": {"private": "ignored"},
            }
        )
    path = write_rollout(tmp_path / "rollout-lightweight.jsonl", events)
    decoded = 0
    original = sessions.strict_json_loads

    def counted_loads(value: str) -> object:
        nonlocal decoded
        decoded += 1
        return original(value)

    monkeypatch.setattr(sessions, "strict_json_loads", counted_loads)

    fields = session_search_fields(path)

    assert fields["id"] == "lightweight-session"
    assert fields["title"] == "Lightweight title"
    assert fields["model"] == "gpt-test"
    assert decoded < 20


def test_lightweight_search_ignores_invalid_marker_records(tmp_path: Path) -> None:
    path = tmp_path / "rollout-invalid-markers.jsonl"
    path.write_bytes(
        b'{"type":"session_meta","payload":{"id":"invalid-markers"}}\n'
        b'{"type":"event_msg","payload":{"type":"user_message","message":"Valid title"}}\n'
        b'{"type":"user_message",invalid}\n'
        b'["user_message"]\n'
        b'\xff"user_message"\n'
        b'{"type":"user_message"'
    )

    entries = list(iter_session_search_jsonl(path))

    assert [entry[0] for entry in entries] == [2]


def test_lightweight_search_enforces_byte_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bounded-search.jsonl"
    path.write_bytes(b'{"type":"turn_context"}\n')

    assert list(_iter_search_lines(path, end_byte_offset=0)) == []
    with pytest.raises(ValueError, match="Invalid paginated"):
        list(_iter_search_lines(path, end_byte_offset=-1))
    with pytest.raises(ValueError, match="past the source"):
        list(_iter_search_lines(path, end_byte_offset=path.stat().st_size + 1))
    with pytest.raises(ValueError, match="splits a JSONL record"):
        list(_iter_search_lines(path, end_byte_offset=path.stat().st_size - 1))

    incomplete = tmp_path / "incomplete-search.jsonl"
    incomplete.write_bytes(b'{"type":"turn_context"}')
    with pytest.raises(ValueError, match="terminating newline"):
        list(_iter_search_lines(incomplete, end_byte_offset=incomplete.stat().st_size))

    monkeypatch.setattr(sessions, "MAX_JSONL_LINE_BYTES", 16)
    oversized = tmp_path / "oversized-search.jsonl"
    oversized.write_bytes(b'{"type":"turn_context","padding":"xxxxxxxx"}\n')
    assert list(_iter_search_lines(oversized)) == []
    with pytest.raises(ValueError, match="splits a JSONL record"):
        list(_iter_search_lines(oversized, end_byte_offset=20))


def test_paginated_lightweight_search_applies_logical_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "paginated-search.jsonl"
    lines = [
        b'{"ordinal":1,"type":"turn_context","payload":{}}\n',
        b'{"ordinal":3,"type":"turn_context","payload":{}}\n',
        b'{"ordinal":4,"type":"turn_context","payload":{"model":"selected"}}\n',
        b'{"ordinal":6,"type":"turn_context","payload":{}}\n',
        b'{"ordinal":true,"type":"turn_context","payload":{}}\n',
        b'{"ordinal":5,"type":"turn_context",invalid}\n',
    ]
    path.write_bytes(b"".join(lines))
    segment = RolloutSegment(
        path=path,
        start_ordinal=2,
        end_ordinal_exclusive=6,
        end_byte_offset=path.stat().st_size,
    )
    monkeypatch.setattr(sessions, "rollout_lineage", lambda _path: [segment])
    monkeypatch.setattr(
        sessions,
        "first_session_metadata",
        lambda _path: {"subagent_history_start_ordinal": 4},
    )

    entries = list(iter_session_search_jsonl(path))

    assert [(line, entry["ordinal"]) for line, entry in entries] == [(3, 4)]

    monkeypatch.setattr(
        sessions,
        "first_session_metadata",
        lambda _path: {"subagent_history_start_ordinal": True},
    )
    with pytest.raises(ValueError, match="invalid ordinal boundary"):
        list(iter_session_search_jsonl(path))

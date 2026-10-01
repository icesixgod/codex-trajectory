"""Ephemeral safe-summary pages and validated source offsets for on-demand details."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import weakref
from collections.abc import Iterator
from copy import deepcopy
from pathlib import Path
from typing import Any, BinaryIO

from . import sessions
from .json_support import strict_json_loads

MAX_INDEX_BYTES = 128 * 1024 * 1024


class PageIndex:
    """A private temporary index; never persist raw tool bodies or instructions."""

    def __init__(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="codex-trajectory-pages-")
        self.path = Path(self.directory.name) / "pages.sqlite"
        try:
            self.db = sqlite3.connect(self.path, check_same_thread=False)
            self.db.executescript(
                "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;"
                "CREATE TABLE records (id INTEGER PRIMARY KEY, turn INTEGER, data TEXT);"
                "CREATE TABLE turns (id INTEGER PRIMARY KEY, data TEXT);"
                "CREATE TABLE sources (event INTEGER PRIMARY KEY, line INTEGER, path TEXT, "
                "offset INTEGER, length INTEGER);"
                "CREATE TABLE refs (record INTEGER, event INTEGER, PRIMARY KEY(record,event));"
                "CREATE TABLE turn_errors (id INTEGER PRIMARY KEY, event INTEGER, kind TEXT);"
                "CREATE TABLE identifiers (kind TEXT, value TEXT, PRIMARY KEY(kind,value));"
            )
            self.path.chmod(0o600)
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self.directory.cleanup()
            raise
        # Register after TemporaryDirectory's finalizer so SQLite closes first
        # during weakref shutdown, when Windows still locks the database file.
        self._finalizer = weakref.finalize(self, self._cleanup, self.db, self.directory)
        self.current_event = 0
        self.pending: dict[int, dict[str, Any]] = {}
        self.template: dict[str, Any] = {}
        self.enabled = True
        self.details_ambiguous = False

    @property
    def size(self) -> int:
        return self.path.stat().st_size

    @staticmethod
    def _cleanup(db: sqlite3.Connection, directory: tempfile.TemporaryDirectory[str]) -> None:
        try:
            db.close()
        finally:
            directory.cleanup()

    def close(self) -> None:
        self._finalizer()

    def enter_event(self, event: int, line: int) -> None:
        if not self.enabled:
            return
        self.current_event = event
        position = sessions.current_jsonl_position()
        if position is None:
            self.enabled = False
            return
        path, offset, length = position
        self.db.execute(
            "INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?)",
            (event, line, str(path), offset, length),
        )

    def track(self, record: dict[str, Any], *, inherit: int | None = None) -> None:
        if not self.enabled:
            return
        index = int(record["index"])
        self.pending[index] = record
        self.db.execute("INSERT OR IGNORE INTO refs VALUES (?,?)", (index, self.current_event))
        if inherit is not None:
            self.db.execute(
                "INSERT OR IGNORE INTO refs SELECT ?,event FROM refs WHERE record=?",
                (index, inherit),
            )

    def track_identifiers(self, record_id: str, call_id: str | None) -> None:
        """Detect replay ambiguity across the whole log, before page-local ID salting."""
        if not self.enabled or self.details_ambiguous:
            return
        for kind, value in (("record", record_id), ("call", call_id)):
            if (
                value is not None
                and self.db.execute(
                    "INSERT OR IGNORE INTO identifiers VALUES (?,?)", (kind, value)
                ).rowcount
                == 0
            ):
                self.details_ambiguous = True
                return

    def flush(self) -> None:
        if not self.enabled:
            self.pending.clear()
            return
        for index, record in self.pending.items():
            safe = {key: value for key, value in record.items() if not key.startswith("_")}
            self.db.execute(
                "INSERT OR REPLACE INTO records VALUES (?,?,?)",
                (index, record["turn"], json.dumps(safe, ensure_ascii=True)),
            )
        self.pending.clear()
        if self.current_event % 256 == 0 and self.size > MAX_INDEX_BYTES:
            self.enabled = False

    def track_turn_error(self, turn: dict[str, Any], kind: str) -> None:
        if not self.enabled:
            return
        self.db.execute(
            "INSERT OR REPLACE INTO turn_errors VALUES (?,?,?)",
            (turn["index"], self.current_event, kind),
        )

    def hydrate_turn_errors(self, turns: list[dict[str, Any]]) -> None:
        from .privacy import safe_text

        sources: dict[str, list[tuple[dict[str, Any], int, int, str]]] = {}
        for turn in turns:
            if turn["error"] is None:
                continue
            row = self.db.execute(
                "SELECT event,kind FROM turn_errors WHERE id=?", (turn["index"],)
            ).fetchone()
            if row is None:
                continue
            event, kind = row
            if kind == "overlapping_turn_start":
                turn["error"] = "Turn was superseded by another persisted start event."
            elif kind == "mismatched_item_turn":
                turn["error"] = "Turn was superseded by an item from another persisted turn."
            elif kind == "mismatched_turn_completion":
                turn["error"] = "Turn was superseded by a completion for another persisted turn."
            else:
                source = self.db.execute(
                    "SELECT path,offset,length FROM sources WHERE event=?", (event,)
                ).fetchone()
                if source is None:
                    continue
                path, offset, length = source
                sources.setdefault(path, []).append((turn, offset, length, kind))
        total = 0
        for path, reads in sources.items():
            with sessions._open_rollout_binary(Path(path)) as handle:
                for turn, offset, length, kind in reads:
                    handle.seek(offset)
                    raw = handle.read(length)
                    total += len(raw)
                    sessions._enforce_read_limit(total, at_least=True)
                    if len(raw) != length or not raw.endswith(b"\n"):
                        raise ValueError("Indexed rollout source changed during detail read.")
                    entry = strict_json_loads(raw.decode("utf-8"))
                    payload = entry["payload"]
                    value = payload.get("reason" if kind == "turn_aborted" else "error")
                    text = value.get("message") if isinstance(value, dict) else value
                    turn["error"] = safe_text(text, 1000) or turn["error"]

    def save_turn(self, turn: dict[str, Any]) -> None:
        if not self.enabled:
            return
        safe = {key: value for key, value in turn.items() if not key.startswith("_")}
        self.db.execute(
            "INSERT OR REPLACE INTO turns VALUES (?,?)",
            (turn["index"], json.dumps(safe, ensure_ascii=True)),
        )

    def finish(self, trajectory: dict[str, Any]) -> None:
        self.flush()
        self.db.commit()
        self.template = deepcopy(trajectory)
        self.template["records"] = []
        self.template["turns"] = []
        self.enabled = self.enabled and self.size <= MAX_INDEX_BYTES

    def page(self, max_records: int, before_record: int | None, page_bytes: int) -> dict[str, Any]:
        result = deepcopy(self.template)
        count = int(result["stats"]["records"])
        end = count + 1 if before_record is None else before_record
        rows = self.db.execute(
            "SELECT data FROM records WHERE id < ? ORDER BY id DESC LIMIT ?", (end, max_records)
        )
        records: list[dict[str, Any]] = []
        total_bytes = 0
        trimmed = False
        for (data,) in rows:
            if total_bytes + len(data.encode("ascii")) > page_bytes:
                trimmed = True
                break
            total_bytes += len(data.encode("ascii"))
            records.append(json.loads(data))
        records.reverse()
        first = records[0]["index"] if records else None
        last = records[-1]["index"] if records else None
        earlier = first - 1 if first is not None else 0
        later = count - last if last is not None else count
        result["records"] = records
        result["pagination"] = {
            "firstRecord": first,
            "lastRecord": last,
            "earlierRecords": earlier,
            "laterRecords": later,
            "hasEarlier": earlier > 0,
            "hasLater": later > 0,
            "nextBeforeRecord": first if earlier > 0 else None,
        }
        indexes = {record["turn"] for record in records}
        # Match projection's bounded turn list, including the newest turn summaries.
        indexes.update(
            row[0] for row in self.db.execute("SELECT id FROM turns ORDER BY id DESC LIMIT 1000")
        )
        selected = sorted(indexes)
        if len(selected) > 1000:
            owners = {record["turn"] for record in records}
            selected = sorted(
                owners | set(sorted(indexes - owners, reverse=True)[: 1000 - len(owners)])
            )
        result["turns"] = [
            json.loads(row[0])
            for index in selected
            if (row := self.db.execute("SELECT data FROM turns WHERE id=?", (index,)).fetchone())
        ]
        stats = result["stats"]
        stats.update(visibleRecords=len(records), omittedRecords=earlier + later)
        stats.pop("visibleTurns", None)
        stats.pop("omittedTurns", None)
        if stats["turns"] > len(result["turns"]):
            stats.update(
                visibleTurns=len(result["turns"]),
                omittedTurns=stats["turns"] - len(result["turns"]),
            )
        if trimmed and not any(w["code"] == "page_byte_limit" for w in result["warnings"]):
            result["warnings"].append(
                {
                    "code": "page_byte_limit",
                    "line": 1,
                    "message": "Page size was reduced; load earlier records to continue.",
                }
            )
        return result

    def detail_entries(
        self, records: list[dict[str, Any]]
    ) -> Iterator[tuple[int, int, dict[str, Any]]]:
        """Seek only sources associated with the selected records, in logical order."""
        if not records:
            return
        placeholders = ",".join("?" for _ in records)
        rows = self.db.execute(
            "SELECT DISTINCT s.event,s.line,s.path,s.offset,s.length FROM sources s "
            f"JOIN refs r ON r.event=s.event WHERE r.record IN ({placeholders}) ORDER BY s.event",
            [record["index"] for record in records],
        )
        handle: BinaryIO | None = None
        opened_path: str | None = None
        total = 0
        try:
            for event, line, path, offset, length in rows:
                if path != opened_path:
                    if handle is not None:
                        handle.close()
                    handle = sessions._open_rollout_binary(Path(path))
                    opened_path = path
                assert handle is not None
                handle.seek(offset)
                raw = handle.read(length)
                total += len(raw)
                sessions._enforce_read_limit(total, at_least=True)
                if len(raw) != length or not raw.endswith(b"\n"):
                    raise ValueError("Indexed rollout source changed during detail read.")
                value = strict_json_loads(raw.decode("utf-8"))
                if not isinstance(value, dict):
                    raise ValueError("Indexed rollout source is not an object.")
                yield event, line, value
        finally:
            if handle is not None:
                handle.close()

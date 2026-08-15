"""Safe discovery and reading of local Codex rollout logs."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

JsonEntry = tuple[int, dict[str, Any]]


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


def is_safe_session_file(path: Path, root: Path) -> bool:
    """Check that a regular non-symlink file remains under its session root."""
    try:
        return (
            path.is_file()
            and not path.is_symlink()
            and path.resolve(strict=True).is_relative_to(root.resolve(strict=True))
        )
    except (OSError, RuntimeError):
        return False


def session_files(include_archived: bool) -> list[Path]:
    """Discover authorized rollout logs ordered by last modification."""
    paths: list[Path] = []
    for root in session_roots(include_archived):
        if not root.is_dir():
            continue
        paths.extend(path for path in root.rglob("*.jsonl") if is_safe_session_file(path, root))

    def modified(path: Path) -> int:
        try:
            return path.stat().st_mtime_ns
        except OSError:
            return 0

    return sorted(paths, key=modified, reverse=True)


def iter_jsonl(path: Path, warnings: list[dict[str, Any]] | None = None) -> Iterator[JsonEntry]:
    """Yield valid JSON objects while optionally collecting line diagnostics."""
    diagnostics = warnings if warnings is not None else []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as error:
                if line.endswith("\n"):
                    diagnostics.append(
                        {
                            "code": "malformed_jsonl",
                            "line": line_number,
                            "message": (
                                f"Skipped malformed JSONL line {line_number}: {error.msg}."
                            ),
                        }
                    )
                continue
            if isinstance(value, dict):
                yield line_number, value
            else:
                diagnostics.append(
                    {
                        "code": "non_object_jsonl",
                        "line": line_number,
                        "message": f"Skipped non-object JSONL line {line_number}.",
                    }
                )


def read_jsonl(path: Path) -> tuple[list[JsonEntry], list[dict[str, Any]]]:
    """Read valid JSON objects and report malformed complete lines."""
    warnings: list[dict[str, Any]] = []
    return list(iter_jsonl(path, warnings)), warnings

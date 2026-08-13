#!/usr/bin/env python3
"""Check release archives for matching, safe repository contents."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from pathlib import PurePosixPath

REQUIRED = {
    "LICENSE",
    "LICENSES/DeepSeek-Harness.txt",
    "NOTICE",
    "README.md",
    "README.zh-CN.md",
    "plugins/codex-trajectory/.codex-plugin/plugin.json",
    "plugins/codex-trajectory/.mcp.json",
    "plugins/codex-trajectory/scripts/codex_trajectory_mcp.py",
    "plugins/codex-trajectory/assets/trajectory.html",
}
FORBIDDEN_PARTS = {".git", ".venv", "__pycache__", "dist"}


def normalized(names: list[str]) -> set[str]:
    """Strip the archive root and reject generated or private residue."""
    result: set[str] = set()
    roots: set[str] = set()
    for name in names:
        path = PurePosixPath(name)
        if not path.parts:
            continue
        roots.add(path.parts[0])
        if len(path.parts) == 1 or name.endswith("/"):
            continue
        relative = PurePosixPath(*path.parts[1:])
        if FORBIDDEN_PARTS.intersection(relative.parts) or relative.suffix == ".pyc":
            raise ValueError(f"forbidden release member: {relative}")
        result.add(str(relative))
    if len(roots) != 1:
        raise ValueError("release members must share one top-level directory")
    missing = REQUIRED - result
    if missing:
        raise ValueError(f"required release member is missing: {sorted(missing)[0]}")
    return result


def main() -> None:
    """Validate one ZIP and tar.gz pair."""
    parser = argparse.ArgumentParser()
    parser.add_argument("zip_archive")
    parser.add_argument("tar_archive")
    args = parser.parse_args()
    with zipfile.ZipFile(args.zip_archive) as archive:
        zip_members = normalized(
            [member.filename for member in archive.infolist() if not member.is_dir()]
        )
    with tarfile.open(args.tar_archive, "r:gz") as archive:
        tar_members = normalized(
            [member.name for member in archive.getmembers() if member.isfile()]
        )
    if zip_members != tar_members:
        raise ValueError("ZIP and tar.gz release contents differ")
    print(f"Release archives contain {len(zip_members)} matching files.")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Check release archives for byte-identical, safe repository contents."""

from __future__ import annotations

import argparse
import hashlib
import re
import stat
import tarfile
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import IO

REQUIRED = {
    ".agents/plugins/marketplace.json",
    "CHANGELOG.md",
    "CODE_OF_CONDUCT.md",
    "CONTRIBUTING.md",
    "LICENSE",
    "LICENSES/DeepSeek-Harness.txt",
    "NOTICE",
    "PRIVACY.md",
    "README.md",
    "README.zh-CN.md",
    "SECURITY.md",
    "docs/interface.md",
    "plugins/codex-trajectory/.codex-plugin/plugin.json",
    "plugins/codex-trajectory/.mcp.json",
    "plugins/codex-trajectory/README.md",
    "plugins/codex-trajectory/assets/icon.png",
    "plugins/codex-trajectory/assets/brand/icon.svg",
    "plugins/codex-trajectory/assets/brand/logo-dark.svg",
    "plugins/codex-trajectory/assets/brand/logo-light.svg",
    "plugins/codex-trajectory/assets/logo-dark.png",
    "plugins/codex-trajectory/assets/logo.png",
    "plugins/codex-trajectory/assets/screenshots/desktop-en.png",
    "plugins/codex-trajectory/assets/screenshots/mobile-zh.png",
    "plugins/codex-trajectory/assets/trajectory.html",
    "plugins/codex-trajectory/assets/whale-girl-mining-32f.png",
    "plugins/codex-trajectory/scripts/codex_trajectory/__init__.py",
    "plugins/codex-trajectory/scripts/codex_trajectory/json_support.py",
    "plugins/codex-trajectory/scripts/codex_trajectory/privacy.py",
    "plugins/codex-trajectory/scripts/codex_trajectory/projection.py",
    "plugins/codex-trajectory/scripts/codex_trajectory/protocol.py",
    "plugins/codex-trajectory/scripts/codex_trajectory/sessions.py",
    "plugins/codex-trajectory/scripts/codex_trajectory_mcp.py",
    "plugins/codex-trajectory/skills/inspect-codex-trajectory/SKILL.md",
    "pyproject.toml",
    "schemas/trajectory-v1.schema.json",
    "scripts/check_archives.py",
    "scripts/smoke_mcp.py",
    "scripts/validate_release.py",
    "uv.lock",
}
FORBIDDEN_PARTS = {
    ".codex",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    ".workspace-ledger",
    "__pycache__",
    "dist",
}
FORBIDDEN_NAMES = {
    "credentials",
    "credentials.json",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
}
FORBIDDEN_SUFFIXES = {".jks", ".key", ".p12", ".pem", ".pfx", ".pyc", ".pyo"}
MAX_MEMBERS = 10_000
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
WINDOWS_RESERVED = {
    "aux",
    "con",
    "nul",
    "prn",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
RELEASE_ROOT_PATTERN = re.compile(
    r"codex-trajectory-(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
)


@dataclass(frozen=True)
class ReleaseMember:
    """Security-relevant identity for one regular release file."""

    size: int
    sha256: str
    executable: bool


def executable_mode(mode: int, name: str) -> bool:
    """Normalize Git archive modes while rejecting unsafe permission bits."""
    permissions = mode & 0o7777
    if permissions and (permissions & 0o7000 or permissions & 0o002):
        raise ValueError(f"unsafe release member permissions: {name}")
    return bool(permissions & 0o111)


def relative_member(name: str) -> tuple[str, str | None]:
    """Validate an archive member name and remove its single root directory."""
    if (
        not name
        or len(name.encode("utf-8")) > 4_096
        or "\x00" in name
        or "\\" in name
        or name.startswith("/")
    ):
        raise ValueError(f"unsafe release member name: {name!r}")
    is_directory = name.endswith("/")
    raw = name[:-1] if is_directory else name
    parts = raw.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"unsafe release member name: {name!r}")
    for part in parts:
        if len(part.encode("utf-8")) > 255:
            raise ValueError(f"non-portable release member name: {name!r}")
        reserved_stem = part.split(".", 1)[0].rstrip(" .").casefold()
        if (
            any(ord(character) < 32 or character in '<>:"|?*' for character in part)
            or part.endswith((" ", "."))
            or reserved_stem in WINDOWS_RESERVED
        ):
            raise ValueError(f"non-portable release member name: {name!r}")
    root = parts[0]
    if len(parts) == 1:
        if not is_directory:
            raise ValueError("release files must live below one top-level directory")
        return root, None
    relative = PurePosixPath(*parts[1:])
    lowered_parts = {part.casefold() for part in relative.parts}
    filename = relative.name.casefold()
    if FORBIDDEN_PARTS.intersection(lowered_parts):
        raise ValueError(f"forbidden release member: {relative}")
    if (
        filename in FORBIDDEN_NAMES
        or str(relative).casefold() == "agents.md"
        or filename == ".env"
        or filename.startswith(".env.")
        or relative.suffix.casefold() in FORBIDDEN_SUFFIXES
    ):
        raise ValueError(f"private release member: {relative}")
    return root, str(relative)


def digest_stream(stream: IO[bytes], expected_size: int) -> str:
    """Hash a bounded stream and verify the archive's declared size."""
    digest = hashlib.sha256()
    consumed = 0
    while chunk := stream.read(1024 * 1024):
        consumed += len(chunk)
        if consumed > MAX_FILE_BYTES:
            raise ValueError("release member exceeds the maximum allowed size")
        digest.update(chunk)
    if consumed != expected_size:
        raise ValueError("release member size does not match its archive metadata")
    return digest.hexdigest()


def _record_member(
    files: dict[str, ReleaseMember],
    canonical_names: dict[str, str],
    directories: dict[str, str],
    relative: str,
    member: ReleaseMember,
) -> None:
    if relative in files:
        raise ValueError(f"duplicate release member: {relative}")
    canonical = unicodedata.normalize("NFC", relative).casefold()
    if canonical in canonical_names:
        raise ValueError(
            f"release members collide across supported filesystems: "
            f"{canonical_names[canonical]} and {relative}"
        )
    if canonical in directories:
        raise ValueError(f"release file collides with a directory: {relative}")
    raw_parts = relative.split("/")
    canonical_parts = canonical.split("/")
    for index in range(1, len(canonical_parts)):
        ancestor = "/".join(canonical_parts[:index])
        raw_ancestor = "/".join(raw_parts[:index])
        if ancestor in canonical_names:
            raise ValueError(f"release file has a file ancestor: {canonical_names[ancestor]}")
        existing_directory = directories.get(ancestor)
        if existing_directory is not None and existing_directory != raw_ancestor:
            raise ValueError(
                "release directories collide across supported filesystems: "
                f"{existing_directory} and {raw_ancestor}"
            )
        directories.setdefault(ancestor, raw_ancestor)
    canonical_names[canonical] = relative
    files[relative] = member


def _record_directory(
    canonical_names: dict[str, str],
    directories: dict[str, str],
    explicit_directories: set[str],
    relative: str,
) -> None:
    canonical = unicodedata.normalize("NFC", relative).casefold()
    raw_parts = relative.split("/")
    canonical_parts = canonical.split("/")
    for index in range(1, len(canonical_parts) + 1):
        ancestor = "/".join(canonical_parts[:index])
        raw_ancestor = "/".join(raw_parts[:index])
        if ancestor in canonical_names:
            raise ValueError(f"release directory has a file ancestor: {canonical_names[ancestor]}")
        existing_directory = directories.get(ancestor)
        if existing_directory is not None and existing_directory != raw_ancestor:
            raise ValueError(
                "release directories collide across supported filesystems: "
                f"{existing_directory} and {raw_ancestor}"
            )
        directories.setdefault(ancestor, raw_ancestor)
    if canonical in explicit_directories:
        raise ValueError(f"duplicate release directory: {relative}")
    explicit_directories.add(canonical)


def _validate_inventory(
    roots: set[str], files: dict[str, ReleaseMember], total_size: int
) -> tuple[str, dict[str, ReleaseMember]]:
    if len(roots) != 1:
        raise ValueError("release members must share one top-level directory")
    root = next(iter(roots))
    if RELEASE_ROOT_PATTERN.fullmatch(root) is None:
        raise ValueError("release top-level directory is not versioned canonically")
    if total_size > MAX_TOTAL_BYTES:
        raise ValueError("release archive exceeds the maximum total size")
    missing = REQUIRED - files.keys()
    if missing:
        raise ValueError(f"required release member is missing: {sorted(missing)[0]}")
    return root, files


def inspect_zip(path: str) -> tuple[str, dict[str, ReleaseMember]]:
    """Inspect a ZIP without extracting or following archive links."""
    roots: set[str] = set()
    files: dict[str, ReleaseMember] = {}
    canonical_names: dict[str, str] = {}
    directories: dict[str, str] = {}
    explicit_directories: set[str] = set()
    total_size = 0
    with zipfile.ZipFile(path) as archive:
        members = archive.infolist()
        if len(members) > MAX_MEMBERS:
            raise ValueError("release archive contains too many members")
        for info in members:
            root, relative = relative_member(info.filename)
            roots.add(root)
            unix_mode = info.external_attr >> 16
            file_type = stat.S_IFMT(unix_mode)
            if info.is_dir():
                if file_type not in {0, stat.S_IFDIR}:
                    raise ValueError(f"unsafe ZIP directory type: {info.filename}")
                executable_mode(unix_mode, info.filename)
                if relative is not None:
                    _record_directory(canonical_names, directories, explicit_directories, relative)
                continue
            if file_type not in {0, stat.S_IFREG}:
                raise ValueError(f"non-regular ZIP member: {info.filename}")
            if info.flag_bits & 1:
                raise ValueError(f"encrypted ZIP member: {info.filename}")
            if relative is None or info.file_size > MAX_FILE_BYTES:
                raise ValueError(f"invalid ZIP release member: {info.filename}")
            total_size += info.file_size
            if total_size > MAX_TOTAL_BYTES:
                raise ValueError("release archive exceeds the maximum total size")
            with archive.open(info, "r") as stream:
                digest = digest_stream(stream, info.file_size)
            _record_member(
                files,
                canonical_names,
                directories,
                relative,
                ReleaseMember(info.file_size, digest, executable_mode(unix_mode, info.filename)),
            )
    return _validate_inventory(roots, files, total_size)


def inspect_tar(path: str) -> tuple[str, dict[str, ReleaseMember]]:
    """Inspect a tar.gz without extracting or following archive links."""
    roots: set[str] = set()
    files: dict[str, ReleaseMember] = {}
    canonical_names: dict[str, str] = {}
    directories: dict[str, str] = {}
    explicit_directories: set[str] = set()
    total_size = 0
    with tarfile.open(path, "r:gz") as archive:
        for member_number, info in enumerate(archive, 1):
            if member_number > MAX_MEMBERS:
                raise ValueError("release archive contains too many members")
            name = f"{info.name}/" if info.isdir() and not info.name.endswith("/") else info.name
            root, relative = relative_member(name)
            roots.add(root)
            if info.isdir():
                executable_mode(info.mode, info.name)
                if relative is not None:
                    _record_directory(canonical_names, directories, explicit_directories, relative)
                continue
            if not info.isreg():
                raise ValueError(f"non-regular tar member: {info.name}")
            if relative is None or info.size > MAX_FILE_BYTES:
                raise ValueError(f"invalid tar release member: {info.name}")
            stream = archive.extractfile(info)
            if stream is None:
                raise ValueError(f"unreadable tar release member: {info.name}")
            total_size += info.size
            if total_size > MAX_TOTAL_BYTES:
                raise ValueError("release archive exceeds the maximum total size")
            with stream:
                digest = digest_stream(stream, info.size)
            _record_member(
                files,
                canonical_names,
                directories,
                relative,
                ReleaseMember(info.size, digest, executable_mode(info.mode, info.name)),
            )
    return _validate_inventory(roots, files, total_size)


def main() -> None:
    """Validate one ZIP and tar.gz pair."""
    parser = argparse.ArgumentParser()
    parser.add_argument("zip_archive")
    parser.add_argument("tar_archive")
    args = parser.parse_args()
    zip_root, zip_members = inspect_zip(args.zip_archive)
    tar_root, tar_members = inspect_tar(args.tar_archive)
    if zip_root != tar_root:
        raise ValueError("ZIP and tar.gz release roots differ")
    if zip_members != tar_members:
        raise ValueError("ZIP and tar.gz release contents or modes differ")
    print(f"Release archives contain {len(zip_members)} byte-identical safe files.")


if __name__ == "__main__":
    main()

"""Release archive and metadata guard tests."""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from scripts.check_archives import REQUIRED, inspect_tar, inspect_zip, relative_member
from scripts.validate_release import MAX_JSON_NESTING_DEPTH, MAX_RELEASE_JSON_BYTES, load_json


def release_files(value: bytes = b"release-data") -> dict[str, bytes]:
    """Return a minimal complete release inventory."""
    return {name: value for name in REQUIRED}


def write_zip(path: Path, files: dict[str, bytes], root: str = "codex-trajectory-0.2.0") -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, value in files.items():
            archive.writestr(f"{root}/{name}", value)
    return path


def write_tar(path: Path, files: dict[str, bytes], root: str = "codex-trajectory-0.2.0") -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for name, value in files.items():
            info = tarfile.TarInfo(f"{root}/{name}")
            info.size = len(value)
            info.mode = 0o644
            archive.addfile(info, io.BytesIO(value))
    return path


def test_release_archives_compare_file_bytes_and_modes(tmp_path: Path) -> None:
    files = release_files()
    zip_root, zip_members = inspect_zip(str(write_zip(tmp_path / "release.zip", files)))
    tar_root, tar_members = inspect_tar(str(write_tar(tmp_path / "release.tar.gz", files)))

    assert zip_root == tar_root == "codex-trajectory-0.2.0"
    assert zip_members == tar_members
    assert set(zip_members) == REQUIRED

    changed = release_files()
    changed["README.md"] = b"different"
    _, changed_members = inspect_tar(str(write_tar(tmp_path / "changed.tar.gz", changed)))
    assert zip_members != changed_members


@pytest.mark.parametrize(
    "name",
    [
        "/absolute/file",
        "root/../escape",
        "root\\windows\\escape",
        "root/.workspace-ledger/project.toml",
        "root/.env.production",
        "root/AGENTS.md",
        "root/private.key",
        "root/docs/CON.txt",
        "root/docs/CON .txt",
        "root/docs/trailing. ",
        "root/docs/alternate:stream",
        "root/private.pem",
        "root/docs/" + "x" * 256,
    ],
)
def test_release_member_rejects_unsafe_or_private_paths(name: str) -> None:
    with pytest.raises(ValueError):
        relative_member(name)


def test_nested_agents_document_is_not_mistaken_for_workspace_instructions() -> None:
    assert relative_member("root/docs/AGENTS.md") == ("root", "docs/AGENTS.md")


def test_release_archive_rejects_links_and_duplicate_members(tmp_path: Path) -> None:
    files = release_files()
    linked_tar = tmp_path / "linked.tar.gz"
    with tarfile.open(linked_tar, "w:gz") as archive:
        for name, value in files.items():
            info = tarfile.TarInfo(f"codex-trajectory-0.2.0/{name}")
            info.size = len(value)
            archive.addfile(info, io.BytesIO(value))
        link = tarfile.TarInfo("codex-trajectory-0.2.0/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "README.md"
        archive.addfile(link)
    with pytest.raises(ValueError, match="non-regular"):
        inspect_tar(str(linked_tar))

    zip_path = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for name, value in files.items():
            archive.writestr(f"codex-trajectory-0.2.0/{name}", value)
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("codex-trajectory-0.2.0/README.md", b"duplicate")
    with pytest.raises(ValueError, match="duplicate"):
        inspect_zip(str(zip_path))


def test_release_archive_rejects_case_insensitive_name_collisions(tmp_path: Path) -> None:
    files = release_files()
    files["docs/Name.txt"] = b"first"
    files["docs/name.TXT"] = b"second"

    with pytest.raises(ValueError, match="collide"):
        inspect_zip(str(write_zip(tmp_path / "case-collision.zip", files)))


def test_release_archive_rejects_case_inconsistent_directories(tmp_path: Path) -> None:
    files = release_files()
    files["DOCS/extra.txt"] = b"different directory spelling"

    with pytest.raises(ValueError, match="directories collide"):
        inspect_zip(str(write_zip(tmp_path / "directory-case-collision.zip", files)))


def test_release_archive_requires_a_canonical_versioned_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="top-level directory"):
        inspect_zip(str(write_zip(tmp_path / "unsafe-root.zip", release_files(), root=".git")))


def test_release_archive_rejects_file_directory_prefix_collisions(tmp_path: Path) -> None:
    files = release_files()
    descendant = tmp_path / "file-ancestor.zip"
    with zipfile.ZipFile(descendant, "w") as archive:
        for name, value in files.items():
            archive.writestr(f"codex-trajectory-0.2.0/{name}", value)
        archive.writestr("codex-trajectory-0.2.0/collision", b"file")
        archive.writestr("codex-trajectory-0.2.0/collision/child", b"child")
    with pytest.raises(ValueError, match="file ancestor"):
        inspect_zip(str(descendant))

    same_path = tmp_path / "file-directory.zip"
    with zipfile.ZipFile(same_path, "w") as archive:
        for name, value in files.items():
            archive.writestr(f"codex-trajectory-0.2.0/{name}", value)
        archive.writestr("codex-trajectory-0.2.0/collision/", b"")
        archive.writestr("codex-trajectory-0.2.0/collision", b"file")
    with pytest.raises(ValueError, match="collides with a directory"):
        inspect_zip(str(same_path))

    duplicate_directory = tmp_path / "duplicate-directory.zip"
    with zipfile.ZipFile(duplicate_directory, "w") as archive:
        for name, value in files.items():
            archive.writestr(f"codex-trajectory-0.2.0/{name}", value)
        archive.writestr("codex-trajectory-0.2.0/empty/", b"")
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("codex-trajectory-0.2.0/empty/", b"")
    with pytest.raises(ValueError, match="duplicate release directory"):
        inspect_zip(str(duplicate_directory))


def test_release_archive_rejects_unsafe_permission_bits(tmp_path: Path) -> None:
    files = release_files()
    unsafe_tar = tmp_path / "unsafe-world-writable.tar.gz"
    with tarfile.open(unsafe_tar, "w:gz") as archive:
        for name, value in files.items():
            info = tarfile.TarInfo(f"codex-trajectory-0.2.0/{name}")
            info.size = len(value)
            info.mode = 0o666 if name == "README.md" else 0o644
            archive.addfile(info, io.BytesIO(value))
    with pytest.raises(ValueError, match="permissions"):
        inspect_tar(str(unsafe_tar))

    unsafe_zip = tmp_path / "unsafe-setuid.zip"
    with zipfile.ZipFile(unsafe_zip, "w") as archive:
        for name, value in files.items():
            info = zipfile.ZipInfo(f"codex-trajectory-0.2.0/{name}")
            info.create_system = 3
            mode = 0o104755 if name == "README.md" else 0o100644
            info.external_attr = mode << 16
            archive.writestr(info, value)
    with pytest.raises(ValueError, match="permissions"):
        inspect_zip(str(unsafe_zip))


@pytest.mark.parametrize(
    "payload, message",
    [
        ('{"field": 1, "field": 2}', "duplicate"),
        ('{"field": NaN}', "constant"),
        ('{"field": 1e400}', "finite"),
        ('{"field": ' + "9" * 257 + "}", "maximum supported size"),
    ],
)
def test_release_json_parser_rejects_ambiguous_or_unbounded_numbers(
    tmp_path: Path, payload: str, message: str
) -> None:
    path = tmp_path / "metadata.json"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_json(path)


def test_release_json_parser_rejects_oversized_input(tmp_path: Path) -> None:
    path = tmp_path / "metadata.json"
    path.write_bytes(b" " * (MAX_RELEASE_JSON_BYTES + 1))

    with pytest.raises(ValueError, match="exceeds"):
        load_json(path)


def test_release_json_parser_rejects_excessive_nesting(tmp_path: Path) -> None:
    path = tmp_path / "metadata.json"
    path.write_text(
        '{"value":' + "[" * MAX_JSON_NESTING_DEPTH + "0" + "]" * MAX_JSON_NESTING_DEPTH + "}",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="depth"):
        load_json(path)

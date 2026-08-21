#!/usr/bin/env python3
"""Validate the marketplace, plugin, skill, schema, and release contents."""

from __future__ import annotations

import argparse
import json
import math
import re
import struct
import zlib
from pathlib import Path
from typing import Any, cast

from jsonschema import Draft202012Validator  # type: ignore[import-untyped]

ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "plugins" / "codex-trajectory"
VERSION_PATTERN = re.compile(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")
MAX_RELEASE_JSON_BYTES = 1024 * 1024
MAX_JSON_INTEGER_DIGITS = 256
MAX_JSON_NESTING_DEPTH = 256


def _validate_json_nesting(value: str) -> None:
    """Reject excessive container nesting independently of Python's recursion limit."""
    depth = 0
    in_string = False
    escaped = False
    for character in value:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character in "[{":
            depth += 1
            if depth > MAX_JSON_NESTING_DEPTH:
                raise ValueError("JSON nesting exceeds the maximum supported depth")
        elif character in "]}":
            depth -= 1


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant {value}")


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _bounded_json_integer(value: str) -> int:
    digits = value.removeprefix("-")
    if len(digits) > MAX_JSON_INTEGER_DIGITS:
        raise ValueError("JSON integer exceeds the maximum supported size")
    return int(value)


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("JSON number must be finite")
    return parsed


def _read_bounded(path: Path, maximum: int, message: str) -> bytes:
    with path.open("rb") as handle:
        value = handle.read(maximum + 1)
    if len(value) > maximum:
        raise ValueError(message)
    return value


def load_json(path: Path) -> dict[str, Any]:
    """Load a JSON object or fail with a useful path."""
    raw = _read_bounded(
        path,
        MAX_RELEASE_JSON_BYTES,
        f"release JSON exceeds {MAX_RELEASE_JSON_BYTES} bytes: {path.name}",
    )
    text = raw.decode("utf-8")
    _validate_json_nesting(text)
    value: Any = json.loads(
        text,
        parse_constant=_reject_json_constant,
        parse_float=_finite_json_float,
        parse_int=_bounded_json_integer,
        object_pairs_hook=_unique_json_object,
    )
    if not isinstance(value, dict):
        raise ValueError(f"{path.relative_to(ROOT)} must contain a JSON object")
    return value


def require(condition: bool, message: str) -> None:
    """Raise one release validation error when a condition is false."""
    if not condition:
        raise ValueError(message)


def plugin_path(value: str) -> Path:
    """Resolve and contain one plugin-relative manifest path."""
    require(value.startswith("./"), f"manifest path must be plugin-relative: {value}")
    resolved = (PLUGIN / value).resolve()
    require(resolved.is_relative_to(PLUGIN.resolve()), f"manifest path escapes plugin: {value}")
    require(resolved.is_file(), f"manifest path is missing: {value}")
    return resolved


def png_dimensions(path: Path) -> tuple[int, int]:
    """Validate PNG framing, checksums, compressed data, and IHDR dimensions."""
    label = path.relative_to(ROOT)
    data = _read_bounded(path, 20 * 1024 * 1024, f"PNG is unexpectedly large: {label}")
    require(data.startswith(b"\x89PNG\r\n\x1a\n"), f"not a PNG: {label}")
    offset = 8
    dimensions: tuple[int, int] | None = None
    row_bytes: int | None = None
    compressed = bytearray()
    saw_end = False
    while offset < len(data):
        require(offset + 12 <= len(data), f"truncated PNG chunk: {label}")
        length = struct.unpack(">I", data[offset : offset + 4])[0]
        chunk_type = data[offset + 4 : offset + 8]
        chunk_end = offset + 12 + length
        require(chunk_end <= len(data), f"truncated PNG payload: {label}")
        payload = data[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack(">I", data[offset + 8 + length : chunk_end])[0]
        actual_crc = zlib.crc32(chunk_type + payload) & 0xFFFFFFFF
        require(actual_crc == expected_crc, f"PNG checksum mismatch: {label}")
        if chunk_type == b"IHDR":
            require(offset == 8 and length == 13, f"invalid PNG IHDR: {label}")
            width, height = struct.unpack(">II", payload[:8])
            require(width > 0 and height > 0, f"invalid PNG dimensions: {label}")
            bit_depth, color_type, compression, filter_method, interlace = payload[8:13]
            valid_depths = {
                0: {1, 2, 4, 8, 16},
                2: {8, 16},
                3: {1, 2, 4, 8},
                4: {8, 16},
                6: {8, 16},
            }
            require(
                color_type in valid_depths and bit_depth in valid_depths[color_type],
                f"invalid PNG color format: {label}",
            )
            require(
                compression == 0 and filter_method == 0,
                f"unsupported PNG compression or filter method: {label}",
            )
            require(interlace == 0, f"release PNG must be non-interlaced: {label}")
            channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
            row_bytes = (width * channels * bit_depth + 7) // 8
            dimensions = (width, height)
        elif chunk_type == b"IDAT":
            compressed.extend(payload)
        elif chunk_type == b"IEND":
            require(length == 0, f"invalid PNG IEND: {label}")
            saw_end = True
            offset = chunk_end
            break
        offset = chunk_end
    require(
        dimensions is not None and row_bytes is not None and bool(compressed),
        f"PNG image data is incomplete: {label}",
    )
    require(saw_end and offset == len(data), f"PNG has no clean IEND: {label}")
    inflater = zlib.decompressobj()
    try:
        decoded = inflater.decompress(bytes(compressed), 20 * 1024 * 1024 + 1)
    except zlib.error as error:
        raise ValueError(f"invalid PNG compressed data: {label}") from error
    require(
        len(decoded) <= 20 * 1024 * 1024
        and inflater.eof
        and not inflater.unused_data
        and not inflater.unconsumed_tail,
        f"invalid PNG compressed data: {label}",
    )
    dimensions = cast(tuple[int, int], dimensions)
    row_bytes = cast(int, row_bytes)
    expected_size = (row_bytes + 1) * dimensions[1]
    require(len(decoded) == expected_size, f"PNG scanline size mismatch: {label}")
    require(
        all(decoded[offset] <= 4 for offset in range(0, expected_size, row_bytes + 1)),
        f"PNG has an invalid scanline filter: {label}",
    )
    return dimensions


def validate_manifest(expected_version: str | None) -> str:
    """Validate plugin metadata and every referenced interface asset."""
    manifest = load_json(PLUGIN / ".codex-plugin" / "plugin.json")
    require(
        set(manifest)
        == {
            "name",
            "version",
            "description",
            "author",
            "homepage",
            "repository",
            "license",
            "keywords",
            "skills",
            "mcpServers",
            "interface",
        },
        "unexpected plugin manifest field",
    )
    require(manifest.get("name") == "codex-trajectory", "unexpected plugin name")
    version = manifest.get("version")
    require(
        isinstance(version, str) and VERSION_PATTERN.fullmatch(version) is not None,
        "invalid version",
    )
    version = cast(str, version)
    if expected_version is not None:
        require(version == expected_version, f"manifest version {version} != {expected_version}")
    require(manifest.get("license") == "MIT", "plugin license must be MIT")
    description = manifest.get("description")
    require(
        isinstance(description, str) and 20 <= len(description) <= 200,
        "plugin description length is invalid",
    )
    require(
        manifest.get("homepage") == "https://github.com/icesixgod/codex-trajectory#readme",
        "homepage URL is not canonical",
    )
    require(
        manifest.get("repository") == "https://github.com/icesixgod/codex-trajectory",
        "repository URL is not canonical",
    )
    require(
        manifest.get("author") == {"name": "icesixgod", "url": "https://github.com/icesixgod"},
        "author metadata changed",
    )
    require(manifest.get("skills") == "./skills/", "skill directory must be declared")
    require(manifest.get("mcpServers") == "./.mcp.json", "MCP configuration must be declared")
    require((PLUGIN / "skills").is_dir(), "skill directory is missing")
    keywords = manifest.get("keywords")
    require(
        keywords == ["codex", "trajectory", "session", "timeline", "debugging"],
        "plugin keywords changed",
    )

    interface = manifest.get("interface")
    require(isinstance(interface, dict), "plugin interface is required")
    interface = cast(dict[str, Any], interface)
    require(
        set(interface)
        == {
            "displayName",
            "shortDescription",
            "longDescription",
            "developerName",
            "category",
            "capabilities",
            "websiteURL",
            "privacyPolicyURL",
            "defaultPrompt",
            "brandColor",
            "composerIcon",
            "logo",
            "logoDark",
            "screenshots",
        },
        "unexpected plugin interface field",
    )
    require(interface.get("displayName") == "Codex Trajectory", "display name changed")
    require(interface.get("developerName") == "icesixgod", "developer name changed")
    require(interface.get("category") == "Developer Tools", "plugin category changed")
    require(interface.get("capabilities") == ["Interactive", "Read"], "capabilities changed")
    require(
        interface.get("websiteURL") == "https://github.com/icesixgod/codex-trajectory",
        "website URL is not canonical",
    )
    require(
        interface.get("privacyPolicyURL")
        == "https://github.com/icesixgod/codex-trajectory/blob/main/PRIVACY.md",
        "privacy URL is not canonical",
    )
    require(interface.get("brandColor") == "#5B6CFF", "brand color changed")
    for field in ("shortDescription", "longDescription"):
        value = interface.get(field)
        require(
            isinstance(value, str) and 20 <= len(value) <= 500,
            f"interface.{field} length is invalid",
        )
    prompts = interface.get("defaultPrompt")
    require(
        isinstance(prompts, list)
        and len(prompts) == 2
        and all(isinstance(value, str) and 10 <= len(value) <= 200 for value in prompts),
        "default prompts are invalid",
    )
    expected_assets = {
        "composerIcon": "./assets/icon.png",
        "logo": "./assets/logo.png",
        "logoDark": "./assets/logo-dark.png",
    }
    for field, value in expected_assets.items():
        require(interface.get(field) == value, f"interface.{field} changed")
        plugin_path(value)
    screenshots = interface.get("screenshots")
    require(
        screenshots
        == [
            "./assets/screenshots/desktop-en.png",
            "./assets/screenshots/mobile-zh.png",
        ],
        "screenshot declarations changed",
    )
    screenshots = cast(list[Any], screenshots)
    for value in screenshots:
        require(isinstance(value, str), "screenshot paths must be strings")
        value = cast(str, value)
        plugin_path(value)

    expected_sizes = {
        "./assets/icon.png": (128, 128),
        "./assets/logo.png": (1200, 360),
        "./assets/logo-dark.png": (1200, 360),
        "./assets/screenshots/desktop-en.png": (1280, 900),
        "./assets/screenshots/mobile-zh.png": (600, 900),
        "./assets/whale-girl-mining-32f.png": (768, 384),
    }
    for value, dimensions in expected_sizes.items():
        require(png_dimensions(plugin_path(value)) == dimensions, f"unexpected dimensions: {value}")
    return version


def declared_version(path: Path, pattern: str, label: str) -> str:
    """Read one semantic version declaration from a release source file."""
    match = re.search(pattern, path.read_text(encoding="utf-8"), flags=re.MULTILINE)
    require(match is not None, f"{label} version declaration is missing")
    match = cast(re.Match[str], match)
    version = match.group(1)
    require(VERSION_PATTERN.fullmatch(version) is not None, f"invalid {label} version")
    return version


def validate_versions(manifest_version: str) -> None:
    """Require every independently packaged version source to agree."""
    project_version = declared_version(
        ROOT / "pyproject.toml",
        r'^version\s*=\s*"([^"]+)"',
        "project",
    )
    package_version = declared_version(
        PLUGIN / "scripts" / "codex_trajectory" / "__init__.py",
        r'^__version__\s*=\s*"([^"]+)"',
        "runtime package",
    )
    require(
        project_version == manifest_version,
        f"project version {project_version} != manifest version {manifest_version}",
    )
    require(
        package_version == manifest_version,
        f"runtime version {package_version} != manifest version {manifest_version}",
    )


def validate_marketplace() -> None:
    """Validate the personal marketplace entry and local source mapping."""
    marketplace = load_json(ROOT / ".agents" / "plugins" / "marketplace.json")
    require(
        set(marketplace) == {"name", "interface", "plugins"},
        "unexpected marketplace field",
    )
    require(marketplace.get("name") == "icesixgod", "marketplace name must be icesixgod")
    require(
        marketplace.get("interface") == {"displayName": "icesixgod"},
        "marketplace interface metadata changed",
    )
    plugins = marketplace.get("plugins")
    require(isinstance(plugins, list) and len(plugins) == 1, "marketplace must have one plugin")
    plugins = cast(list[Any], plugins)
    plugin = plugins[0]
    require(
        isinstance(plugin, dict) and plugin.get("name") == "codex-trajectory",
        "marketplace plugin name changed",
    )
    plugin = cast(dict[str, Any], plugin)
    require(
        set(plugin) == {"name", "source", "policy", "category"},
        "unexpected marketplace plugin field",
    )
    require(
        plugin.get("source") == {"source": "local", "path": "./plugins/codex-trajectory"},
        "marketplace source changed",
    )
    require(
        plugin.get("policy") == {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "marketplace policy changed",
    )
    require(plugin.get("category") == "Developer Tools", "marketplace category changed")


def validate_skill() -> None:
    """Validate the Skill frontmatter and privacy guidance."""
    skill = (PLUGIN / "skills" / "inspect-codex-trajectory" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    require(skill.startswith("---\n"), "Skill frontmatter is missing")
    frontmatter, body = skill[4:].split("\n---\n", 1)
    fields = {
        key.strip(): value.strip()
        for key, value in (line.split(":", 1) for line in frontmatter.splitlines())
    }
    require(fields.get("name") == "inspect-codex-trajectory", "Skill name changed")
    description = fields.get("description", "").strip()
    require(20 <= len(description) <= 1024, "Skill description length is invalid")
    require("detailLevel: summary" in body, "Skill must default to safe summaries")
    require(
        "only after the user explicitly asks" in body, "Skill full-detail consent rule is missing"
    )


def validate_mcp() -> None:
    """Validate the cross-platform uv script command."""
    config = load_json(PLUGIN / ".mcp.json")
    require(
        set(config) == {"mcpServers"},
        "MCP config must contain only the mcpServers object",
    )
    servers = config.get("mcpServers")
    require(isinstance(servers, dict), "MCP mcpServers value must be an object")
    servers = cast(dict[str, Any], servers)
    require(set(servers) == {"codex-trajectory"}, "unexpected MCP server declaration")
    server = servers.get("codex-trajectory")
    require(isinstance(server, dict), "codex-trajectory MCP server is missing")
    server = cast(dict[str, Any], server)
    require(
        set(server) == {"command", "args", "cwd", "env_vars"},
        "unexpected MCP server field",
    )
    require(server.get("command") == "uv", "MCP runtime must be uv")
    require(
        server.get("args") == ["run", "--script", "./scripts/codex_trajectory_mcp.py"],
        "MCP uv command changed",
    )
    require(server.get("cwd") == ".", "MCP cwd must be the plugin root")
    require(server.get("env_vars") == ["CODEX_HOME"], "MCP environment allowlist changed")


def validate_schema() -> None:
    """Validate the complete versioned trajectory schema."""
    schema = load_json(ROOT / "schemas" / "trajectory-v1.schema.json")
    Draft202012Validator.check_schema(schema)
    require(
        schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema",
        "unexpected JSON Schema draft",
    )
    properties = schema.get("properties")
    require(isinstance(properties, dict), "schema properties are missing")
    properties = cast(dict[str, Any], properties)
    schema_version = properties.get("schemaVersion")
    require(
        isinstance(schema_version, dict) and schema_version.get("const") == 1,
        "schemaVersion must be 1",
    )
    require(schema.get("additionalProperties") is False, "schema root must be closed")
    definitions = schema.get("$defs")
    require(isinstance(definitions, dict), "schema definitions are missing")
    definitions = cast(dict[str, Any], definitions)
    require(
        {"session", "stats", "turn", "record", "warning", "sessionOverview"} <= definitions.keys(),
        "schema definitions are incomplete",
    )


def validate_attribution() -> None:
    """Validate third-party license texts and attribution statements."""
    dsh_license = (ROOT / "LICENSES" / "DeepSeek-Harness.txt").read_text(encoding="utf-8")
    require(dsh_license.startswith("MIT License\n"), "DeepSeek Harness MIT title is missing")
    require(
        "Copyright (c) 2026 DeepSeek" in dsh_license,
        "DeepSeek Harness copyright notice is missing",
    )
    require(
        "The above copyright notice and this permission notice shall be included" in dsh_license,
        "DeepSeek Harness MIT permission notice is incomplete",
    )
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    normalized_notice = " ".join(notice.split())
    require(
        "@deepseek-ai/dsh-client-ui-trajectory" in notice,
        "NOTICE must identify the adapted DeepSeek Harness component",
    )
    require(
        "LICENSES/DeepSeek-Harness.txt" in notice,
        "NOTICE must link the complete DeepSeek Harness license",
    )
    require(
        "not affiliated with or endorsed by DeepSeek" in normalized_notice,
        "NOTICE must disclaim DeepSeek affiliation and endorsement",
    )
    code_of_conduct = (ROOT / "CODE_OF_CONDUCT.md").read_text(encoding="utf-8")
    require(
        "https://creativecommons.org/licenses/by/4.0/" in code_of_conduct,
        "Contributor Covenant CC BY 4.0 attribution is missing",
    )


def validate_repository_contents() -> None:
    """Reject private residue and require the public release documents."""
    required = [
        "LICENSE",
        "LICENSES/DeepSeek-Harness.txt",
        "NOTICE",
        "PRIVACY.md",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "CODE_OF_CONDUCT.md",
        "CHANGELOG.md",
        "README.md",
        "README.zh-CN.md",
        "uv.lock",
    ]
    for value in required:
        require((ROOT / value).is_file(), f"required release file is missing: {value}")
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for value in ("__pycache__/", "*.py[cod]", ".venv/", "dist/", ".DS_Store"):
        require(value in ignore, f"release residue ignore is missing: {value}")


def validate_release_notes(expected_version: str | None) -> None:
    """Require versioned changelog and release notes for a tagged build."""
    if expected_version is None:
        return
    require(
        VERSION_PATTERN.fullmatch(expected_version) is not None,
        f"invalid expected version: {expected_version}",
    )
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased_marker = "## [Unreleased]"
    release_marker = f"## [{expected_version}]"
    require(
        release_marker in changelog,
        f"CHANGELOG.md has no {expected_version} release section",
    )
    require(unreleased_marker in changelog, "CHANGELOG.md has no Unreleased section")
    unreleased_start = changelog.index(unreleased_marker) + len(unreleased_marker)
    release_start = changelog.index(release_marker)
    require(
        unreleased_start < release_start,
        f"CHANGELOG.md places {expected_version} before Unreleased",
    )
    require(
        not changelog[unreleased_start:release_start].strip(),
        f"CHANGELOG.md still contains Unreleased changes in the {expected_version} build",
    )
    notes = ROOT / ".github" / "release-notes" / f"v{expected_version}.md"
    require(notes.is_file(), f"release notes are missing: {notes.relative_to(ROOT)}")
    require(
        notes.read_text(encoding="utf-8").startswith(f"# Codex Trajectory v{expected_version}\n"),
        f"release notes title does not match v{expected_version}",
    )


def main() -> None:
    """Run all release validations."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", help="Expected semantic version without a v prefix")
    args = parser.parse_args()
    version = validate_manifest(args.version)
    validate_versions(version)
    validate_marketplace()
    validate_skill()
    validate_mcp()
    validate_schema()
    validate_attribution()
    validate_repository_contents()
    validate_release_notes(args.version)
    print(f"Codex Trajectory {version} release metadata is valid.")


if __name__ == "__main__":
    main()

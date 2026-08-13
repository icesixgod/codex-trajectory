#!/usr/bin/env python3
"""Validate the marketplace, plugin, skill, schema, and release contents."""

from __future__ import annotations

import argparse
import json
import re
import struct
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "plugins" / "codex-trajectory"
VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")


def load_json(path: Path) -> dict[str, Any]:
    """Load a JSON object or fail with a useful path."""
    value: Any = json.loads(path.read_text(encoding="utf-8"))
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
    """Read dimensions from a PNG IHDR header."""
    header = path.read_bytes()[:24]
    require(header.startswith(b"\x89PNG\r\n\x1a\n"), f"not a PNG: {path.relative_to(ROOT)}")
    return struct.unpack(">II", header[16:24])


def validate_manifest(expected_version: str | None) -> str:
    """Validate plugin metadata and every referenced interface asset."""
    manifest = load_json(PLUGIN / ".codex-plugin" / "plugin.json")
    require(manifest.get("name") == "codex-trajectory", "unexpected plugin name")
    version = manifest.get("version")
    require(
        isinstance(version, str) and VERSION_PATTERN.fullmatch(version) is not None,
        "invalid version",
    )
    if expected_version is not None:
        require(version == expected_version, f"manifest version {version} != {expected_version}")
    require(manifest.get("license") == "MIT", "plugin license must be MIT")
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

    interface = manifest.get("interface")
    require(isinstance(interface, dict), "plugin interface is required")
    require(
        interface.get("privacyPolicyURL")
        == "https://github.com/icesixgod/codex-trajectory/blob/main/PRIVACY.md",
        "privacy URL is not canonical",
    )
    for field in ("composerIcon", "logo", "logoDark"):
        value = interface.get(field)
        require(isinstance(value, str), f"interface.{field} is required")
        plugin_path(value)
    screenshots = interface.get("screenshots")
    require(isinstance(screenshots, list) and len(screenshots) == 2, "two screenshots are required")
    for value in screenshots:
        require(isinstance(value, str), "screenshot paths must be strings")
        plugin_path(value)

    expected_sizes = {
        "./assets/icon.png": (128, 128),
        "./assets/logo.png": (1200, 360),
        "./assets/logo-dark.png": (1200, 360),
        "./assets/screenshots/desktop-en.png": (1280, 900),
        "./assets/screenshots/mobile-zh.png": (600, 900),
    }
    for value, dimensions in expected_sizes.items():
        require(png_dimensions(plugin_path(value)) == dimensions, f"unexpected dimensions: {value}")
    return version


def validate_marketplace() -> None:
    """Validate the personal marketplace entry and local source mapping."""
    marketplace = load_json(ROOT / ".agents" / "plugins" / "marketplace.json")
    require(marketplace.get("name") == "icesixgod", "marketplace name must be icesixgod")
    plugins = marketplace.get("plugins")
    require(isinstance(plugins, list) and len(plugins) == 1, "marketplace must have one plugin")
    plugin = plugins[0]
    require(
        isinstance(plugin, dict) and plugin.get("name") == "codex-trajectory",
        "marketplace plugin name changed",
    )
    require(
        plugin.get("source") == {"source": "local", "path": "./plugins/codex-trajectory"},
        "marketplace source changed",
    )
    require(
        plugin.get("policy") == {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "marketplace policy changed",
    )


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
    servers = config.get("mcpServers")
    require(isinstance(servers, dict), "MCP server map is missing")
    server = servers.get("codex-trajectory")
    require(isinstance(server, dict), "codex-trajectory MCP server is missing")
    require(server.get("command") == "uv", "MCP runtime must be uv")
    require(
        server.get("args") == ["run", "--script", "./scripts/codex_trajectory_mcp.py"],
        "MCP uv command changed",
    )
    require(server.get("cwd") == ".", "MCP cwd must be the plugin root")
    require(server.get("env_vars") == ["CODEX_HOME"], "MCP environment allowlist changed")


def validate_schema() -> None:
    """Validate the versioned trajectory schema marker."""
    schema = load_json(ROOT / "schemas" / "trajectory-v1.schema.json")
    require(
        schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema",
        "unexpected JSON Schema draft",
    )
    properties = schema.get("properties")
    require(isinstance(properties, dict), "schema properties are missing")
    schema_version = properties.get("schemaVersion")
    require(
        isinstance(schema_version, dict) and schema_version.get("const") == 1,
        "schemaVersion must be 1",
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


def main() -> None:
    """Run all release validations."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", help="Expected semantic version without a v prefix")
    args = parser.parse_args()
    version = validate_manifest(args.version)
    validate_marketplace()
    validate_skill()
    validate_mcp()
    validate_schema()
    validate_attribution()
    validate_repository_contents()
    print(f"Codex Trajectory {version} release metadata is valid.")


if __name__ == "__main__":
    main()

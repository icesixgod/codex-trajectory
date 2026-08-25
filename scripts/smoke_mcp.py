#!/usr/bin/env python3
"""Exercise the packaged MCP server through its real stdio entry point."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import shutil
import struct

# This smoke test launches the exact entry point declared by the packaged plugin.
import subprocess  # nosec B404
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "plugins" / "codex-trajectory"
WINDOWS_LAUNCHER_SHA256 = "BF3CF1118AD6D5FD1CF91A671D9CCBA6CD3AF7DFB7DABEEEBD37F6C6442EA67F"


def require(condition: bool, message: str) -> None:
    """Fail the smoke test explicitly, including under ``python -O``."""
    if not condition:
        raise RuntimeError(message)


def request(identifier: int, method: str, params: dict[str, Any] | None = None) -> str:
    """Encode one JSON-RPC request line."""
    value: dict[str, Any] = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        value["params"] = params
    return json.dumps(value, ensure_ascii=False)


def compress_zstd(value: bytes) -> bytes:
    """Create the compressed rollout shape used by current Codex stores."""
    try:
        zstd = importlib.import_module("compression.zstd")
    except ModuleNotFoundError:
        zstd = importlib.import_module("zstandard")
        return bytes(zstd.ZstdCompressor().compress(value))
    return bytes(zstd.compress(value))


def declared_mcp_command(plugin: Path = PLUGIN) -> tuple[list[str], Path]:
    """Resolve the exact stdio command and working directory from ``.mcp.json``."""
    config = json.loads((plugin / ".mcp.json").read_text(encoding="utf-8"))
    servers = config.get("mcpServers") if isinstance(config, dict) else None
    server = servers.get("codex-trajectory") if isinstance(servers, dict) else None
    if not isinstance(server, dict):
        raise RuntimeError("Packaged MCP configuration is missing codex-trajectory.")
    command = server.get("command")
    arguments = server.get("args")
    working_directory = server.get("cwd")
    if (
        not isinstance(command, str)
        or not command
        or not isinstance(arguments, list)
        or not all(isinstance(argument, str) for argument in arguments)
        or not isinstance(working_directory, str)
        or not working_directory
    ):
        raise RuntimeError("Packaged MCP command is invalid.")
    plugin_root = plugin.resolve()
    cwd = (plugin_root / working_directory).resolve()
    command_path = Path(command)
    if command_path.parent != Path("."):
        launcher = (cwd / command_path).resolve()
        try:
            launcher.relative_to(plugin_root)
        except ValueError as error:
            raise RuntimeError("Packaged MCP launcher escapes the plugin root.") from error
        # Python 3.10 returns the extensionless Unix shim before consulting PATHEXT.
        resolved_launcher = (
            launcher.with_suffix(".exe") if os.name == "nt" and not launcher.suffix else launcher
        )
        executable = shutil.which(str(resolved_launcher))
    else:
        executable = shutil.which(command)
    if executable is None:
        raise RuntimeError(f"The packaged MCP launcher {command!r} is unavailable.")
    try:
        cwd.relative_to(plugin_root)
    except ValueError as error:
        raise RuntimeError("Packaged MCP cwd escapes the plugin root.") from error
    if not cwd.is_dir():
        raise RuntimeError("Packaged MCP cwd is unavailable.")
    return [executable, *arguments], cwd


def windows_pe_subsystem(executable: Path) -> int:
    """Read the PE subsystem without launching a potentially console-bound binary."""
    with executable.open("rb") as stream:
        dos_header = stream.read(64)
        if len(dos_header) != 64 or dos_header[:2] != b"MZ":
            raise RuntimeError("Windows MCP launcher is not a PE executable.")
        pe_offset = struct.unpack_from("<I", dos_header, 60)[0]
        if pe_offset < 64 or pe_offset > 16 * 1024 * 1024:
            raise RuntimeError("Windows MCP launcher has an invalid PE header offset.")
        stream.seek(pe_offset)
        pe_header = stream.read(24)
        if len(pe_header) != 24 or pe_header[:4] != b"PE\0\0":
            raise RuntimeError("Windows MCP launcher has an invalid PE header.")
        optional_header_size = struct.unpack_from("<H", pe_header, 20)[0]
        optional_header = stream.read(optional_header_size)
    if len(optional_header) < 70 or struct.unpack_from("<H", optional_header, 0)[0] not in {
        0x10B,
        0x20B,
    }:
        raise RuntimeError("Windows MCP launcher has an invalid optional header.")
    return int(struct.unpack_from("<H", optional_header, 68)[0])


def main() -> None:
    """Start the runtime and validate MCP discovery, UI, and Unicode output."""
    manifest = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    expected_version = manifest.get("version")
    if not isinstance(expected_version, str):
        raise RuntimeError("Plugin manifest version is missing.")
    command, command_cwd = declared_mcp_command()
    if os.name == "nt":
        launcher = Path(command[0])
        require(
            hashlib.sha256(launcher.read_bytes()).hexdigest().upper() == WINDOWS_LAUNCHER_SHA256,
            "Windows MCP launcher does not match its reviewed source build.",
        )
        require(
            windows_pe_subsystem(launcher) == 2,
            "Windows MCP launcher must use the GUI subsystem to avoid a console window.",
        )
    with tempfile.TemporaryDirectory() as temporary:
        codex_home = Path(temporary)
        session = codex_home / "sessions" / "2026" / "rollout-smoke.jsonl.zst"
        session.parent.mkdir(parents=True)
        events = [
            {
                "timestamp": "2026-08-14T00:00:00Z",
                "type": "session_meta",
                "payload": {"id": "smoke-session", "cwd": str(codex_home / "project")},
            },
            {
                "timestamp": "2026-08-14T00:00:01Z",
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "检查 Unicode 轨迹"},
            },
        ]
        session.write_bytes(
            compress_zstd(
                "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events).encode(
                    "utf-8"
                )
            )
        )
        messages = [
            request(
                1,
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "codex-trajectory-smoke", "version": "1.0.0"},
                },
            ),
            request(2, "tools/list"),
            request(3, "resources/list"),
            request(4, "resources/read", {"uri": "ui://codex-trajectory/trajectory-v2.html"}),
            request(
                5,
                "tools/call",
                {"name": "get_codex_trajectory", "arguments": {"detailLevel": "summary"}},
            ),
            request(
                6,
                "tools/call",
                {"name": "get_codex_trajectory_update", "arguments": {}},
            ),
        ]
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(codex_home)
        completed = subprocess.run(  # nosec B603
            command,
            cwd=command_cwd,
            env=environment,
            input="\n".join(messages) + "\n",
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=60,
            check=False,
        )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or f"MCP exited with {completed.returncode}")
    responses = [json.loads(line) for line in completed.stdout.splitlines()]
    by_id = {response["id"]: response for response in responses if "id" in response}
    require(
        by_id[1]["result"]["serverInfo"]["version"] == expected_version,
        "MCP server version does not match the manifest.",
    )
    tools = {tool["name"]: tool for tool in by_id[2]["result"]["tools"]}
    require(
        set(tools)
        == {
            "list_codex_sessions",
            "get_codex_trajectory",
            "show_codex_trajectory",
            "get_codex_trajectory_update",
            "get_codex_toolbar_injection_status",
            "set_codex_toolbar_injection",
            "request_codex_task_stop",
        },
        "MCP tool discovery is incomplete.",
    )
    live_tool = tools["get_codex_trajectory_update"]
    require(
        live_tool.get("_meta", {}).get("ui", {}).get("visibility") == ["app"]
        and live_tool.get("_meta", {}).get("openai/visibility") == "private",
        "Live update tool is not app-only.",
    )
    toolbar_status_tool = tools["get_codex_toolbar_injection_status"]
    toolbar_setting_tool = tools["set_codex_toolbar_injection"]
    direct_stop_tool = tools["request_codex_task_stop"]
    require(
        toolbar_status_tool.get("_meta", {}).get("ui", {}).get("visibility") == ["app"]
        and toolbar_setting_tool.get("_meta", {}).get("openai/visibility") == "private"
        and toolbar_setting_tool.get("annotations", {}).get("readOnlyHint") is False,
        "CDP toolbar tools are not scoped to the app resource.",
    )
    require(
        direct_stop_tool.get("_meta", {}).get("ui", {}).get("visibility") == ["app"]
        and direct_stop_tool.get("_meta", {}).get("openai/visibility") == "private"
        and direct_stop_tool.get("annotations", {}).get("readOnlyHint") is False
        and direct_stop_tool.get("annotations", {}).get("destructiveHint") is True
        and direct_stop_tool.get("annotations", {}).get("idempotentHint") is False,
        "Direct stop tool is not scoped to the app resource.",
    )
    require(
        by_id[3]["result"]["resources"][0]["uri"].startswith("ui://"),
        "MCP UI resource is missing.",
    )
    require(
        "Codex Trajectory" in by_id[4]["result"]["contents"][0]["text"],
        "MCP UI resource content is invalid.",
    )
    structured = by_id[5]["result"]["structuredContent"]
    require(structured["schemaVersion"] == 2, "Unexpected trajectory schema version.")
    require(structured["detailLevel"] == "summary", "Smoke test did not use summary mode.")
    require(
        "Unicode" in json.dumps(structured, ensure_ascii=False),
        "Unicode content did not survive the MCP round trip.",
    )
    live_update = by_id[6]["result"]["structuredContent"]
    require(live_update["schemaVersion"] == 2, "Unexpected live-update schema version.")
    require(live_update["unchanged"] is False, "Initial live update was not returned.")
    require(
        live_update["trajectory"]["detailLevel"] == "summary",
        "Live update did not use summary mode.",
    )
    print("MCP stdio smoke passed.")


if __name__ == "__main__":
    main()

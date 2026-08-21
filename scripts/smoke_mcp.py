#!/usr/bin/env python3
"""Exercise the packaged MCP server through its real stdio entry point."""

from __future__ import annotations

import json
import os
import shutil

# This smoke test intentionally launches the packaged MCP entry point with fixed arguments.
import subprocess  # nosec B404
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "plugins" / "codex-trajectory"


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


def main() -> None:
    """Start the runtime and validate MCP discovery, UI, and Unicode output."""
    manifest = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    expected_version = manifest.get("version")
    if not isinstance(expected_version, str):
        raise RuntimeError("Plugin manifest version is missing.")
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required for the MCP smoke test.")
    with tempfile.TemporaryDirectory() as temporary:
        codex_home = Path(temporary)
        session = codex_home / "sessions" / "2026" / "rollout-smoke.jsonl"
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
        session.write_text(
            "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events),
            encoding="utf-8",
        )
        messages = [
            request(1, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}),
            request(2, "tools/list"),
            request(3, "resources/list"),
            request(4, "resources/read", {"uri": "ui://codex-trajectory/trajectory-v1.html"}),
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
            [uv, "run", "--script", "./scripts/codex_trajectory_mcp.py"],
            cwd=PLUGIN,
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
    require(
        toolbar_status_tool.get("_meta", {}).get("ui", {}).get("visibility") == ["app"]
        and toolbar_setting_tool.get("_meta", {}).get("openai/visibility") == "private"
        and toolbar_setting_tool.get("annotations", {}).get("readOnlyHint") is False,
        "CDP toolbar tools are not scoped to the app resource.",
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
    require(structured["schemaVersion"] == 1, "Unexpected trajectory schema version.")
    require(structured["detailLevel"] == "summary", "Smoke test did not use summary mode.")
    require(
        "Unicode" in json.dumps(structured, ensure_ascii=False),
        "Unicode content did not survive the MCP round trip.",
    )
    live_update = by_id[6]["result"]["structuredContent"]
    require(live_update["schemaVersion"] == 1, "Unexpected live-update schema version.")
    require(live_update["unchanged"] is False, "Initial live update was not returned.")
    require(
        live_update["trajectory"]["detailLevel"] == "summary",
        "Live update did not use summary mode.",
    )
    print("MCP stdio smoke passed.")


if __name__ == "__main__":
    main()

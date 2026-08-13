#!/usr/bin/env python3
"""Exercise the packaged MCP server through its real stdio entry point."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "plugins" / "codex-trajectory"


def request(identifier: int, method: str, params: dict[str, Any] | None = None) -> str:
    """Encode one JSON-RPC request line."""
    value: dict[str, Any] = {"jsonrpc": "2.0", "id": identifier, "method": method}
    if params is not None:
        value["params"] = params
    return json.dumps(value, ensure_ascii=False)


def main() -> None:
    """Start the runtime and validate MCP discovery, UI, and Unicode output."""
    with tempfile.TemporaryDirectory() as temporary:
        codex_home = Path(temporary)
        session = codex_home / "sessions" / "2026" / "rollout-smoke.jsonl"
        session.parent.mkdir(parents=True)
        events = [
            {
                "timestamp": "2026-08-14T00:00:00Z",
                "type": "session_meta",
                "payload": {"session_id": "smoke-session", "cwd": str(codex_home / "project")},
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
        ]
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(codex_home)
        completed = subprocess.run(
            ["uv", "run", "--script", "./scripts/codex_trajectory_mcp.py"],
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
    assert by_id[1]["result"]["serverInfo"]["version"] == "0.1.0"
    assert {tool["name"] for tool in by_id[2]["result"]["tools"]} == {
        "list_codex_sessions",
        "get_codex_trajectory",
        "show_codex_trajectory",
    }
    assert by_id[3]["result"]["resources"][0]["uri"].startswith("ui://")
    assert "Codex Trajectory" in by_id[4]["result"]["contents"][0]["text"]
    structured = by_id[5]["result"]["structuredContent"]
    assert structured["schemaVersion"] == 1
    assert structured["detailLevel"] == "summary"
    assert "Unicode" in json.dumps(structured, ensure_ascii=False)
    print("MCP stdio smoke passed.")


if __name__ == "__main__":
    main()

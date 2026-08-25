#!/usr/bin/env python3
"""Verify an installed Codex Trajectory shortcut in a real Windows Codex task."""

from __future__ import annotations

import json
import ntpath
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
PLUGIN_SCRIPTS = ROOT / "plugins" / "codex-trajectory" / "scripts"
sys.path.insert(0, str(PLUGIN_SCRIPTS))

import codex_trajectory_cdp as cdp  # noqa: E402
from codex_trajectory.cdp_peer import discover_host_identity  # noqa: E402
from codex_trajectory.cdp_settings import public_status  # noqa: E402


def require(condition: bool, message: str) -> None:
    """Fail the canary with a stable, user-actionable explanation."""
    if not condition:
        raise RuntimeError(message)


def _button_state(port: int, identity: Any) -> list[dict[str, Any]]:
    expression = """(() => {
      const element = document.getElementById("codex-trajectory-toolbar-entry");
      return {
        exists: Boolean(element),
        visible: Boolean(element)
          && getComputedStyle(element).display !== "none"
          && element.getClientRects().length > 0,
        sessionReady: element?.dataset?.sessionReady === "true",
      };
    })()"""
    states: list[dict[str, Any]] = []
    for target in cdp._targets(port):
        if not cdp._is_codex_shell_target(target):
            continue
        websocket = target.get("webSocketDebuggerUrl")
        if not isinstance(websocket, str):
            continue
        with cdp.WebSocketConnection(
            websocket,
            expected_port=port,
            host_identity=identity,
        ) as connection:
            value = cdp._evaluate(connection, expression)
        if isinstance(value, dict):
            states.append(value)
    return states


def main() -> None:
    """Check the authenticated host, watcher heartbeat, and live shortcut DOM."""
    require(os.name == "nt", "The installed Codex canary is Windows-only.")
    require(
        os.environ.get("RUN_WINDOWS_CODEX_CANARY") == "1",
        "Set RUN_WINDOWS_CODEX_CANARY=1 after opening this repository in Codex.",
    )
    identity = discover_host_identity()
    if identity is None or ntpath.basename(identity.executable).casefold() != "chatgpt.exe":
        raise RuntimeError(
            "Run the canary from a Codex task whose outer desktop host is ChatGPT.exe."
        )
    status = public_status()
    for field in (
        "enabled",
        "browserShortcutAvailable",
        "cdpAvailable",
        "daemonRunning",
        "connected",
        "injected",
        "viewerServing",
    ):
        require(status.get(field) is True, f"Installed canary requires {field}=true.")
    require(status.get("lastError") is None, "Installed watcher reports an error.")
    port = status.get("port")
    if isinstance(port, bool) or not isinstance(port, int):
        raise RuntimeError("Installed watcher did not report a valid CDP port.")
    states = _button_state(port, identity)
    require(bool(states), "No authenticated Codex shell target was available.")
    require(
        any(
            state.get("exists") is True
            and state.get("visible") is True
            and state.get("sessionReady") is True
            for state in states
        ),
        "The trajectory shortcut is not visible and ready in the active Codex task.",
    )
    print(
        json.dumps(
            {
                "host": ntpath.basename(identity.executable),
                "port": port,
                "status": {
                    key: status[key]
                    for key in (
                        "daemonRunning",
                        "connected",
                        "injected",
                        "viewerServing",
                    )
                },
                "button": states,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

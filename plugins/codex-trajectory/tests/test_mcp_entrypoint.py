"""Tests for the console-free MCP startup path."""

from __future__ import annotations

import json
from pathlib import Path

import codex_trajectory_mcp
import pytest


def test_mcp_config_uses_the_portable_windowless_launcher() -> None:
    plugin_root = Path(__file__).resolve().parents[1]
    config = json.loads((plugin_root / ".mcp.json").read_text(encoding="utf-8"))
    server = config["mcpServers"]["codex-trajectory"]

    assert server["command"] == "./scripts/codex_trajectory_launcher"
    assert server["args"] == [
        "run",
        "--script",
        "./scripts/codex_trajectory_mcp.py",
    ]
    launcher = plugin_root / server["command"]
    assert launcher.is_file()
    assert launcher.with_suffix(".c").is_file()
    assert launcher.with_suffix(".exe").is_file()


def test_mcp_reconciles_the_watcher_before_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        codex_trajectory_mcp,
        "reconcile_daemon",
        lambda: calls.append("reconcile"),
    )
    monkeypatch.setattr(
        codex_trajectory_mcp,
        "protocol_main",
        lambda: calls.append("serve"),
    )

    codex_trajectory_mcp.main()

    assert calls == ["reconcile", "serve"]


def test_mcp_still_serves_when_watcher_recovery_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fail_reconcile() -> None:
        raise OSError("temporary watcher race")

    monkeypatch.setattr(codex_trajectory_mcp, "reconcile_daemon", fail_reconcile)
    monkeypatch.setattr(
        codex_trajectory_mcp,
        "protocol_main",
        lambda: calls.append("serve"),
    )

    codex_trajectory_mcp.main()

    assert calls == ["serve"]

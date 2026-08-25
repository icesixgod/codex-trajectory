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


def test_mcp_starts_optional_initialization_before_serving(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        codex_trajectory_mcp,
        "_start_background_initialization",
        lambda: calls.append("initialize"),
    )
    monkeypatch.setattr(
        codex_trajectory_mcp,
        "protocol_main",
        lambda: calls.append("serve"),
    )

    codex_trajectory_mcp.main()

    assert calls == ["initialize", "serve"]
    assert float(codex_trajectory_mcp.os.environ["CODEX_TRAJECTORY_MCP_STARTED_AT"]) > 0


def test_background_initialization_reconciles_then_prewarms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        codex_trajectory_mcp,
        "reconcile_daemon",
        lambda: calls.append("reconcile"),
    )
    monkeypatch.setattr(codex_trajectory_mcp, "prewarm_caches", lambda: calls.append("prewarm"))

    codex_trajectory_mcp._initialize_in_background()

    assert calls == ["reconcile", "prewarm"]


def test_background_prewarm_continues_when_watcher_recovery_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fail_reconcile() -> None:
        raise OSError("temporary watcher race")

    monkeypatch.setattr(codex_trajectory_mcp, "reconcile_daemon", fail_reconcile)
    monkeypatch.setattr(codex_trajectory_mcp, "prewarm_caches", lambda: calls.append("prewarm"))

    codex_trajectory_mcp._initialize_in_background()

    assert calls == ["prewarm"]

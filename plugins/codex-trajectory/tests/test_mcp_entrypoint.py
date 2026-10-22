"""Tests for the console-free MCP startup path."""

from __future__ import annotations

import json
from pathlib import Path

import codex_trajectory_mcp
import pytest
from codex_trajectory import projection, sessions


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


def test_mcp_serves_without_reading_or_indexing_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def unexpected(*args: object, **kwargs: object) -> None:
        calls.append("history")
        pytest.fail("MCP startup must not discover, decompress, or index task history")

    monkeypatch.setattr(sessions, "session_files", unexpected)
    monkeypatch.setattr(sessions, "_decompress_rollout", unexpected)
    monkeypatch.setattr(projection, "resolve_session", unexpected)
    monkeypatch.setattr(projection, "cached_trajectory", unexpected)
    monkeypatch.setattr(projection, "PageIndex", unexpected)
    monkeypatch.setattr(
        codex_trajectory_mcp,
        "protocol_main",
        lambda: calls.append("serve"),
    )

    codex_trajectory_mcp.main()

    assert calls == ["serve"]


def test_mcp_propagates_protocol_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail() -> None:
        raise RuntimeError("disconnected")

    monkeypatch.setattr(codex_trajectory_mcp, "protocol_main", fail)
    with pytest.raises(RuntimeError, match="disconnected"):
        codex_trajectory_mcp.main()

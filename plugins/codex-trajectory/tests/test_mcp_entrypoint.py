"""Tests for the console-free MCP startup path."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

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
        lambda: (
            calls.append("initialize"),
            SimpleNamespace(join=lambda: calls.append("join")),
        )[1],
    )
    monkeypatch.setattr(
        codex_trajectory_mcp,
        "protocol_main",
        lambda: calls.append("serve"),
    )

    codex_trajectory_mcp.main()

    assert calls == ["initialize", "serve", "join"]


def test_mcp_joins_initialization_when_protocol_exits_with_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    joined: list[bool] = []
    monkeypatch.setattr(
        codex_trajectory_mcp,
        "_start_background_initialization",
        lambda: SimpleNamespace(join=lambda: joined.append(True)),
    )

    def fail() -> None:
        raise RuntimeError("disconnected")

    monkeypatch.setattr(codex_trajectory_mcp, "protocol_main", fail)
    with pytest.raises(RuntimeError, match="disconnected"):
        codex_trajectory_mcp.main()
    assert joined == [True]


def test_background_initialization_only_prewarms(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(codex_trajectory_mcp, "prewarm_caches", lambda: calls.append("prewarm"))
    codex_trajectory_mcp._initialize_in_background()
    assert calls == ["prewarm"]


@pytest.mark.parametrize(
    "error", [OSError("unavailable"), RuntimeError("busy"), ValueError("no logs")]
)
def test_background_prewarm_failure_does_not_break_stdio(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def fail() -> None:
        raise error

    monkeypatch.setattr(codex_trajectory_mcp, "prewarm_caches", fail)
    codex_trajectory_mcp._initialize_in_background()

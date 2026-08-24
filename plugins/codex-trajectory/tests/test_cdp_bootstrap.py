"""Tests for the early SessionStart CDP watcher bootstrap."""

from __future__ import annotations

import codex_trajectory_bootstrap
import pytest
from codex_trajectory.cdp_peer import HostIdentity


def test_bootstrap_reconciles_only_with_an_authenticated_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(codex_trajectory_bootstrap, "discover_host_identity", lambda: object())
    monkeypatch.setattr(codex_trajectory_bootstrap, "IS_WINDOWS", False)
    monkeypatch.setattr(
        codex_trajectory_bootstrap,
        "reconcile_daemon",
        lambda: calls.append(True),
    )

    assert codex_trajectory_bootstrap.main() == 0
    assert calls == [True]

    calls.clear()
    monkeypatch.setattr(codex_trajectory_bootstrap, "discover_host_identity", lambda: None)
    assert codex_trajectory_bootstrap.main() == 0
    assert calls == []


def test_bootstrap_leaves_later_recovery_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(codex_trajectory_bootstrap, "discover_host_identity", lambda: object())
    monkeypatch.setattr(codex_trajectory_bootstrap, "IS_WINDOWS", False)

    def fail() -> None:
        raise OSError("temporary watcher race")

    monkeypatch.setattr(codex_trajectory_bootstrap, "reconcile_daemon", fail)

    assert codex_trajectory_bootstrap.main() == 0


def test_windows_bootstrap_watches_in_the_windowless_hook_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = HostIdentity("win32", 4321, "start", "codex.exe", "OpenAI.Codex_test")
    calls: list[HostIdentity] = []
    monkeypatch.setattr(codex_trajectory_bootstrap, "IS_WINDOWS", True)
    monkeypatch.setattr(codex_trajectory_bootstrap, "discover_host_identity", lambda: identity)
    monkeypatch.setattr(
        codex_trajectory_bootstrap,
        "read_settings",
        lambda: {"enabled": True, "port": 9222},
    )
    monkeypatch.setattr(codex_trajectory_bootstrap, "watch", lambda value: calls.append(value) or 0)
    monkeypatch.setattr(
        codex_trajectory_bootstrap,
        "reconcile_daemon",
        lambda: pytest.fail("Windows hook must not launch a nested watcher"),
    )

    assert codex_trajectory_bootstrap.main() == 0
    assert calls == [identity]


def test_windows_bootstrap_preserves_the_disabled_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(codex_trajectory_bootstrap, "IS_WINDOWS", True)
    monkeypatch.setattr(codex_trajectory_bootstrap, "discover_host_identity", lambda: object())
    monkeypatch.setattr(
        codex_trajectory_bootstrap,
        "read_settings",
        lambda: {"enabled": False, "port": 9222},
    )
    monkeypatch.setattr(
        codex_trajectory_bootstrap,
        "watch",
        lambda _identity: pytest.fail("disabled Windows hook must not watch"),
    )

    assert codex_trajectory_bootstrap.main() == 0

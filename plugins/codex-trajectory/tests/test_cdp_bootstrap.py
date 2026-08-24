"""Tests for the early SessionStart CDP watcher bootstrap."""

from __future__ import annotations

import codex_trajectory_bootstrap
import pytest


def test_bootstrap_reconciles_only_with_an_authenticated_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []
    monkeypatch.setattr(codex_trajectory_bootstrap, "discover_host_identity", lambda: object())
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

    def fail() -> None:
        raise OSError("temporary watcher race")

    monkeypatch.setattr(codex_trajectory_bootstrap, "reconcile_daemon", fail)

    assert codex_trajectory_bootstrap.main() == 0

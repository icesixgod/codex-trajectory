"""Opt-in acceptance coverage against a real installed Windows Codex desktop app."""

from __future__ import annotations

import os

import pytest

from scripts import smoke_windows_codex

pytestmark = [
    pytest.mark.windows_app,
    pytest.mark.skipif(os.name != "nt", reason="Windows Codex canary"),
    pytest.mark.skipif(
        os.environ.get("RUN_WINDOWS_CODEX_CANARY") != "1",
        reason="real installed Codex canary is opt-in",
    ),
]


def test_installed_windows_codex_shortcut() -> None:
    smoke_windows_codex.main()

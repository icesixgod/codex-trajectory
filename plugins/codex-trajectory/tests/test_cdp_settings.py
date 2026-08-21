"""Tests for the private CDP toolbar setting and daemon supervision."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from codex_trajectory import cdp_settings


@pytest.fixture
def isolated_cdp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


def test_settings_default_round_trip_and_validation(isolated_cdp_home: Path) -> None:
    assert cdp_settings.read_settings() == {
        "schemaVersion": 1,
        "enabled": False,
        "port": 9222,
    }

    saved = cdp_settings.write_settings(True, 9333)
    assert saved == {"schemaVersion": 1, "enabled": True, "port": 9333}
    assert cdp_settings.read_settings() == saved
    path = cdp_settings.settings_path()
    assert path.parent == isolated_cdp_home / "codex-trajectory"
    assert path.stat().st_mode & 0o777 == 0o600

    with pytest.raises(ValueError, match="enabled"):
        cdp_settings.write_settings(1, 9333)  # type: ignore[arg-type]
    for invalid in (True, "9222", 1023, 65536):
        with pytest.raises(ValueError, match="port"):
            cdp_settings.write_settings(False, invalid)  # type: ignore[arg-type]


def test_settings_fail_closed_for_corrupt_values_and_symlinks(
    isolated_cdp_home: Path,
) -> None:
    path = cdp_settings.settings_path()
    path.parent.mkdir(parents=True)
    path.write_text("not-json", encoding="utf-8")
    assert cdp_settings.read_settings()["enabled"] is False

    path.write_text(json.dumps({"enabled": True, "port": "bad"}), encoding="utf-8")
    assert cdp_settings.read_settings()["port"] == 9222

    path.unlink()
    target = isolated_cdp_home / "outside.json"
    target.write_text('{"enabled":true,"port":9444}', encoding="utf-8")
    path.symlink_to(target)
    assert cdp_settings.read_settings() == {
        "schemaVersion": 1,
        "enabled": False,
        "port": 9222,
    }
    with pytest.raises(OSError, match="symbolic link"):
        cdp_settings.write_settings(True, 9222)


def test_private_state_reads_reject_hardlinks_and_oversized_files(
    isolated_cdp_home: Path,
) -> None:
    path = cdp_settings.settings_path()
    path.parent.mkdir(parents=True)
    target = isolated_cdp_home / "linked-settings.json"
    target.write_text('{"enabled":true,"port":9555}', encoding="utf-8")
    os.link(target, path)

    assert cdp_settings.read_settings()["enabled"] is False

    path.unlink()
    path.write_bytes(b" " * (cdp_settings.MAX_STATE_FILE_BYTES + 1))
    assert cdp_settings.read_settings()["enabled"] is False

    lock = cdp_settings.lock_path()
    lock.write_bytes(b"1" * (cdp_settings.MAX_LOCK_FILE_BYTES + 1))
    assert cdp_settings._lock_owner_pid() is None


def test_public_status_uses_fresh_live_heartbeat(
    isolated_cdp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_settings.write_settings(True, 9444)
    monkeypatch.setattr(cdp_settings, "_probe_cdp", lambda port: port == 9444)
    monkeypatch.setattr(cdp_settings, "_pid_running", lambda pid: pid == 4321)
    cdp_settings.write_daemon_status(
        {
            "pid": 4321,
            "connected": True,
            "injected": True,
            "viewerServing": True,
            "lastError": "x" * 700,
        }
    )

    status = cdp_settings.public_status()
    assert status == {
        "schemaVersion": 1,
        "enabled": True,
        "port": 9444,
        "cdpAvailable": True,
        "daemonRunning": True,
        "connected": True,
        "injected": True,
        "viewerServing": True,
        "lastError": "x" * 500,
    }

    heartbeat = cdp_settings.status_path()
    stale = json.loads(heartbeat.read_text(encoding="utf-8"))
    stale["updatedAt"] = time.time() - 60
    heartbeat.write_text(json.dumps(stale), encoding="utf-8")
    stale_status = cdp_settings.public_status()
    assert stale_status["daemonRunning"] is False
    assert stale_status["lastError"] is None


def test_configure_starts_daemon_and_returns_current_status(
    isolated_cdp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[bool] = []
    monkeypatch.setattr(cdp_settings, "start_daemon", lambda: started.append(True))
    monkeypatch.setattr(cdp_settings, "_probe_cdp", lambda _port: False)

    status = cdp_settings.configure(True, 9555)

    assert started == [True]
    assert status["enabled"] is True
    assert status["port"] == 9555
    assert status["cdpAvailable"] is False


def test_reconcile_daemon_honors_the_persisted_opt_in(
    isolated_cdp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[bool] = []
    monkeypatch.setattr(cdp_settings, "start_daemon", lambda: started.append(True))

    cdp_settings.reconcile_daemon()
    assert started == []

    cdp_settings.write_settings(True, 9222)
    cdp_settings.reconcile_daemon()
    assert started == [True]


def test_start_daemon_is_idempotent_and_detached(
    isolated_cdp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cdp_settings,
        "_daemon_status",
        lambda: {
            "pid": 4321,
            "runtimeId": cdp_settings.daemon_runtime_id(),
            "daemonRunning": True,
            "connected": False,
            "injected": False,
            "viewerServing": False,
            "lastError": None,
        },
    )
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        cdp_settings.subprocess, "Popen", lambda *args, **kwargs: calls.append(kwargs)
    )
    cdp_settings.start_daemon()
    assert calls == []

    monkeypatch.setattr(
        cdp_settings,
        "_daemon_status",
        lambda: {
            "pid": None,
            "runtimeId": None,
            "daemonRunning": False,
            "connected": False,
            "injected": False,
            "viewerServing": False,
            "lastError": None,
        },
    )
    process_calls: list[tuple[list[str], dict[str, Any]]] = []

    def fake_popen(args: list[str], **kwargs: Any) -> SimpleNamespace:
        process_calls.append((args, kwargs))
        return SimpleNamespace()

    monkeypatch.setattr(cdp_settings.subprocess, "Popen", fake_popen)
    cdp_settings.start_daemon()
    assert process_calls[0][0][-1] == "--watch"
    assert process_calls[0][1]["stdin"] is cdp_settings.subprocess.DEVNULL
    assert process_calls[0][1]["close_fds"] is True

    missing = isolated_cdp_home / "missing.py"
    monkeypatch.setattr(cdp_settings, "_daemon_script", lambda: missing)
    with pytest.raises(OSError, match="unavailable"):
        cdp_settings.start_daemon()


def test_start_daemon_replaces_verified_outdated_runtime(
    isolated_cdp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = 4321
    lock = cdp_settings.lock_path()
    lock.parent.mkdir(parents=True)
    lock.write_text(str(pid), encoding="ascii")
    monkeypatch.setattr(
        cdp_settings,
        "_daemon_status",
        lambda: {
            "pid": pid,
            "runtimeId": "outdated-runtime",
            "daemonRunning": True,
            "connected": True,
            "injected": True,
            "viewerServing": True,
            "lastError": None,
        },
    )
    cdp_settings.write_settings(True, 9222)
    running = {"value": True}
    monkeypatch.setattr(cdp_settings, "_pid_running", lambda value: running["value"])
    setting_changes: list[tuple[bool, int]] = []
    real_write_settings = cdp_settings.write_settings

    def fake_write_settings(enabled: bool, port: int) -> dict[str, Any]:
        setting_changes.append((enabled, port))
        saved = real_write_settings(enabled, port)
        if not enabled:
            running["value"] = False
        return saved

    monkeypatch.setattr(cdp_settings, "write_settings", fake_write_settings)
    process_calls: list[list[str]] = []
    monkeypatch.setattr(
        cdp_settings.subprocess,
        "Popen",
        lambda args, **_kwargs: process_calls.append(args),
    )

    cdp_settings.start_daemon()

    assert setting_changes == [(False, 9222), (True, 9222)]
    assert process_calls and process_calls[0][-1] == "--watch"


def test_pid_probe_and_http_probe_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert cdp_settings._pid_running(False) is False
    assert cdp_settings._pid_running(-1) is False
    monkeypatch.setattr(os, "kill", lambda _pid, _signal: None)
    assert cdp_settings._pid_running(123) is True

    class Response:
        def __init__(self, body: bytes, status: int = 200) -> None:
            self.body = body
            self.status = status

        def read(self, _limit: int) -> bytes:
            return self.body

    class Connection:
        body = b'{"webSocketDebuggerUrl":"ws://127.0.0.1:9222/devtools/browser/x"}'
        status = 200

        def __init__(self, _host: str, _port: int, *, timeout: float) -> None:
            assert timeout == 0.25

        def request(self, method: str, route: str, *, headers: dict[str, str]) -> None:
            assert (method, route, headers) == (
                "GET",
                "/json/version",
                {"Accept": "application/json"},
            )

        def getresponse(self) -> Response:
            return Response(self.body, self.status)

        def close(self) -> None:
            return None

    monkeypatch.setattr(cdp_settings.http.client, "HTTPConnection", Connection)
    assert cdp_settings._probe_cdp(9222) is True

    Connection.body = b"{}"
    assert cdp_settings._probe_cdp(9222) is False
    Connection.body = b"not-json"
    assert cdp_settings._probe_cdp(9222) is False
    Connection.status = 302
    Connection.body = b'{"webSocketDebuggerUrl":"ws://127.0.0.1:9222/redirect"}'
    assert cdp_settings._probe_cdp(9222) is False

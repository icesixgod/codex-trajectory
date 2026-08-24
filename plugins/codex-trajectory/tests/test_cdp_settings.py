"""Tests for the private CDP toolbar setting and daemon supervision."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from codex_trajectory import cdp_settings
from codex_trajectory.cdp_peer import HostIdentity


@pytest.fixture
def isolated_cdp_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "codex-home"
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def no_desktop_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep daemon command tests independent from the test runner's ancestry."""
    monkeypatch.setattr(cdp_settings, "discover_host_identity", lambda: None)


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
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600

    with pytest.raises(ValueError, match="enabled"):
        cdp_settings.write_settings(1, 9333)  # type: ignore[arg-type]
    for invalid in (True, "9222", 1023, 65536):
        with pytest.raises(ValueError, match="port"):
            cdp_settings.write_settings(False, invalid)  # type: ignore[arg-type]


def test_settings_fail_closed_for_corrupt_values(isolated_cdp_home: Path) -> None:
    path = cdp_settings.settings_path()
    path.parent.mkdir(parents=True)
    path.write_text("not-json", encoding="utf-8")
    assert cdp_settings.read_settings()["enabled"] is False

    path.write_text(json.dumps({"enabled": True, "port": "bad"}), encoding="utf-8")
    assert cdp_settings.read_settings()["port"] == 9222


def test_settings_fail_closed_for_symlinks(isolated_cdp_home: Path) -> None:
    path = cdp_settings.settings_path()
    path.parent.mkdir(parents=True)

    target = isolated_cdp_home / "outside.json"
    target.write_text('{"enabled":true,"port":9444}', encoding="utf-8")
    try:
        path.symlink_to(target)
    except OSError as error:
        if os.name == "nt" and error.winerror == 1314:
            pytest.skip("Windows symlink privilege is unavailable")
        raise
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
    monkeypatch.setattr(cdp_settings, "BROWSER_SHORTCUT_AVAILABLE", True)
    cdp_settings.write_settings(True, 9444)
    monkeypatch.setattr(cdp_settings, "_probe_cdp", lambda port: port == 9444)
    monkeypatch.setattr(cdp_settings, "_pid_running", lambda pid: pid == 4321)
    cdp_settings.write_daemon_status(
        {
            "pid": 4321,
            "connected": True,
            "injected": True,
            "viewerServing": True,
            "lastError": "/Users/private/project/session.jsonl: connection failed",
        }
    )

    status = cdp_settings.public_status()
    assert status == {
        "schemaVersion": 1,
        "enabled": True,
        "browserShortcutAvailable": True,
        "port": 9444,
        "cdpAvailable": True,
        "daemonRunning": True,
        "connected": True,
        "injected": True,
        "viewerServing": True,
        "lastError": cdp_settings.PUBLIC_DAEMON_ERROR,
    }
    assert "/Users/private" not in cdp_settings.status_path().read_text(encoding="utf-8")

    heartbeat = cdp_settings.status_path()
    stale = json.loads(heartbeat.read_text(encoding="utf-8"))
    stale["updatedAt"] = time.time() - 60
    heartbeat.write_text(json.dumps(stale), encoding="utf-8")
    stale_status = cdp_settings.public_status()
    assert stale_status["daemonRunning"] is False
    assert stale_status["lastError"] is None


def test_public_status_does_not_probe_cdp_when_disabled(
    isolated_cdp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_settings.write_settings(False, 9444)

    def unexpected_probe(_port: int) -> bool:
        pytest.fail("disabled CDP settings must not probe a local port")

    monkeypatch.setattr(cdp_settings, "_probe_cdp", unexpected_probe)

    status = cdp_settings.public_status()

    assert status["enabled"] is False
    assert status["cdpAvailable"] is False


def test_configure_starts_daemon_and_returns_current_status(
    isolated_cdp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[bool] = []
    monkeypatch.setattr(
        cdp_settings,
        "_start_daemon_locked",
        lambda *, cooldown: started.append(cooldown),
    )
    monkeypatch.setattr(cdp_settings, "_probe_cdp", lambda _port: False)

    status = cdp_settings.configure(True, 9555)

    assert started == [False]
    assert status["enabled"] is True
    assert status["port"] == 9555
    assert status["cdpAvailable"] is False


def test_reconcile_daemon_honors_the_persisted_opt_in(
    isolated_cdp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[bool] = []
    monkeypatch.setattr(
        cdp_settings,
        "_start_daemon_locked",
        lambda *, cooldown: started.append(cooldown),
    )

    cdp_settings.reconcile_daemon()
    assert started == []

    cdp_settings.write_settings(True, 9222)
    cdp_settings.reconcile_daemon()
    assert started == [True]


def test_recover_daemon_is_compare_and_swap_and_rate_limited(
    isolated_cdp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stopped = {
        "pid": None,
        "runtimeId": None,
        "daemonRunning": False,
        "connected": False,
        "injected": False,
        "viewerServing": False,
        "lastError": None,
    }
    monkeypatch.setattr(cdp_settings, "_daemon_status", lambda: stopped)
    monkeypatch.setattr(cdp_settings, "_probe_cdp", lambda _port: True)
    now = {"value": 100.0}
    monkeypatch.setattr(cdp_settings.time, "monotonic", lambda: now["value"])
    starts: list[list[str]] = []
    monkeypatch.setattr(
        cdp_settings,
        "_start_watcher_process",
        lambda command, _script: starts.append(command),
    )

    original = cdp_settings.write_settings(True, 9444)
    first = cdp_settings.recover_daemon(9444)
    second = cdp_settings.recover_daemon(9444)

    assert first["enabled"] is True
    assert second["daemonRunning"] is False
    assert len(starts) == 1
    assert cdp_settings.read_settings() == original

    cdp_settings.write_settings(True, 9555)
    changed_port = cdp_settings.recover_daemon(9444)
    assert changed_port["port"] == 9555
    assert len(starts) == 1

    cdp_settings.write_settings(False, 9444)
    disabled = cdp_settings.recover_daemon(9444)
    assert disabled["enabled"] is False
    assert len(starts) == 1

    cdp_settings.write_settings(True, 9444)
    now["value"] += cdp_settings.DAEMON_START_COOLDOWN_SECONDS
    cdp_settings.recover_daemon(9444)
    assert len(starts) == 2


def test_recover_daemon_serializes_concurrent_start_attempts(
    isolated_cdp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    cdp_settings.write_settings(True, 9222)
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
    monkeypatch.setattr(cdp_settings, "_probe_cdp", lambda _port: False)
    monkeypatch.setattr(cdp_settings.time, "monotonic", lambda: 500.0)
    starts: list[bool] = []
    monkeypatch.setattr(
        cdp_settings,
        "_start_watcher_process",
        lambda _command, _script: starts.append(True),
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        statuses = list(executor.map(lambda _index: cdp_settings.recover_daemon(9222), range(8)))

    assert len(starts) == 1
    assert all(status["enabled"] is True for status in statuses)


def test_recover_daemon_retries_failed_start_after_cooldown(
    isolated_cdp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cdp_settings.write_settings(True, 9222)
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
    now = {"value": 800.0}
    monkeypatch.setattr(cdp_settings.time, "monotonic", lambda: now["value"])
    attempts: list[bool] = []

    def fail_start(_command: list[str], _script: Path) -> None:
        attempts.append(True)
        raise OSError("private failure")

    monkeypatch.setattr(cdp_settings, "_start_watcher_process", fail_start)

    with pytest.raises(OSError, match="private failure"):
        cdp_settings.recover_daemon(9222)
    with pytest.raises(OSError, match="previous CDP watcher"):
        cdp_settings.recover_daemon(9222)
    assert len(attempts) == 1

    now["value"] += cdp_settings.DAEMON_START_COOLDOWN_SECONDS
    with pytest.raises(OSError, match="private failure"):
        cdp_settings.recover_daemon(9222)
    assert len(attempts) == 2


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


def test_windows_watcher_uses_the_base_gui_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    active_python = tmp_path / "uv-environment" / "python.exe"
    base_python = tmp_path / "base" / "python.exe"
    active_python.parent.mkdir()
    base_python.parent.mkdir()
    active_python.write_bytes(b"")
    base_python.write_bytes(b"")
    pythonw = base_python.with_name("pythonw.exe")
    pythonw.write_bytes(b"")
    monkeypatch.setattr(cdp_settings, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(cdp_settings.sys, "executable", str(active_python))
    monkeypatch.setattr(
        cdp_settings.sys,
        "_base_executable",
        str(base_python),
        raising=False,
    )

    assert cdp_settings._watcher_executable() == pythonw

    pythonw.unlink()
    assert cdp_settings._watcher_executable() == base_python


def test_daemon_command_binds_the_authenticated_desktop_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = HostIdentity(
        "darwin",
        4321,
        "Mon Aug 24 18:01:41 2026",
        "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT",
    )
    monkeypatch.setattr(cdp_settings, "discover_host_identity", lambda: identity)
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
    commands: list[list[str]] = []
    monkeypatch.setattr(
        cdp_settings,
        "_start_watcher_process",
        lambda command, _script: commands.append(command),
    )

    cdp_settings.start_daemon()

    assert commands == [
        [
            str(cdp_settings._watcher_executable()),
            str(cdp_settings._daemon_script()),
            "--host-identity",
            identity.encode(),
            "--watch",
        ]
    ]


def test_runtime_revision_replaces_an_old_same_path_watcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    old_runtime = cdp_settings.daemon_runtime_id()
    monkeypatch.setattr(
        cdp_settings,
        "DAEMON_RUNTIME_REVISION",
        cdp_settings.DAEMON_RUNTIME_REVISION + 1,
    )

    assert cdp_settings.daemon_runtime_id() != old_runtime


def test_runtime_identity_does_not_depend_on_the_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = cdp_settings.daemon_runtime_id()
    monkeypatch.setattr(
        cdp_settings,
        "_watcher_executable",
        lambda: Path("different-launcher.exe"),
    )

    assert cdp_settings.daemon_runtime_id() == runtime


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


def test_outdated_replacement_preserves_later_configuration_across_processes(
    isolated_cdp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = 4321
    watcher_lock = cdp_settings.lock_path()
    watcher_lock.parent.mkdir(parents=True)
    watcher_lock.write_text(str(pid), encoding="ascii")
    cdp_settings.write_settings(True, 9222)
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
    monkeypatch.setattr(cdp_settings, "_start_watcher_process", lambda _command, _script: None)

    proceed = isolated_cdp_home / "configure-now"
    attempted = isolated_cdp_home / "configure-attempted"
    scripts_root = Path(cdp_settings.__file__).resolve().parents[1]
    child_source = """
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from codex_trajectory import cdp_settings

proceed = Path(sys.argv[2])
attempted = Path(sys.argv[3])
while not proceed.exists():
    time.sleep(0.01)
cdp_settings._start_daemon_locked = lambda *, cooldown: None
attempted.write_text("ready", encoding="ascii")
cdp_settings.configure(False, 9555)
"""
    process = subprocess.Popen(
        [sys.executable, "-c", child_source, str(scripts_root), str(proceed), str(attempted)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    first_probe = True

    def watcher_running(value: Any) -> bool:
        nonlocal first_probe
        assert value == pid
        if first_probe:
            first_probe = False
            proceed.write_text("go", encoding="ascii")
            deadline = time.monotonic() + 5.0
            while not attempted.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert attempted.exists()
            with pytest.raises(subprocess.TimeoutExpired):
                process.wait(timeout=0.2)
        return False

    monkeypatch.setattr(cdp_settings, "_pid_running", watcher_running)
    try:
        cdp_settings.start_daemon()
        stdout, stderr = process.communicate(timeout=5.0)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()

    assert process.returncode == 0, (stdout, stderr)
    assert cdp_settings.read_settings() == {
        "schemaVersion": 1,
        "enabled": False,
        "port": 9555,
    }


def test_outdated_replacement_does_not_restore_over_an_unlocked_newer_write(
    isolated_cdp_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = 4321
    watcher_lock = cdp_settings.lock_path()
    watcher_lock.parent.mkdir(parents=True)
    watcher_lock.write_text(str(pid), encoding="ascii")
    cdp_settings.write_settings(True, 9222)
    changed = False

    def watcher_running(value: Any) -> bool:
        nonlocal changed
        assert value == pid
        if not changed:
            changed = True
            cdp_settings.write_settings(False, 9222)
        return False

    monkeypatch.setattr(cdp_settings, "_pid_running", watcher_running)

    cdp_settings._stop_outdated_daemon({"pid": pid})

    assert cdp_settings.read_settings() == {
        "schemaVersion": 1,
        "enabled": False,
        "port": 9222,
    }


def test_pid_probe_and_http_probe_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert cdp_settings._pid_running(False) is False
    assert cdp_settings._pid_running(-1) is False
    if os.name != "nt":
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


def test_windows_pid_probe_uses_a_non_destructive_process_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Any, ...]] = []
    winapi = ModuleType("_winapi")
    winapi.SYNCHRONIZE = 0x100000  # type: ignore[attr-defined]
    winapi.WAIT_TIMEOUT = 258  # type: ignore[attr-defined]

    def open_process(access: int, inherited: bool, pid: int) -> int:
        calls.append(("open", access, inherited, pid))
        return 9876

    def wait_for_single_object(handle: int, timeout: int) -> int:
        calls.append(("wait", handle, timeout))
        return 258

    def close_handle(handle: int) -> None:
        calls.append(("close", handle))

    winapi.OpenProcess = open_process  # type: ignore[attr-defined]
    winapi.WaitForSingleObject = wait_for_single_object  # type: ignore[attr-defined]
    winapi.CloseHandle = close_handle  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "_winapi", winapi)

    def unexpected_kill(_pid: int, _signal: int) -> None:
        pytest.fail("Windows process liveness checks must not call os.kill")

    monkeypatch.setattr(cdp_settings, "os", SimpleNamespace(name="nt", kill=unexpected_kill))

    assert cdp_settings._pid_running(1234) is True
    assert calls == [
        ("open", 0x100000, False, 1234),
        ("wait", 9876, 0),
        ("close", 9876),
    ]


@pytest.mark.skipif(os.name != "nt", reason="Windows-only process handle integration")
def test_windows_pid_probe_leaves_the_process_running() -> None:
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert cdp_settings._pid_running(process.pid) is True
        assert process.poll() is None
    finally:
        process.terminate()
        process.wait(timeout=5.0)

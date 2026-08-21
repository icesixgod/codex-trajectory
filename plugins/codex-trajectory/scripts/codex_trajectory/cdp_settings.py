"""Persist and supervise the optional local CDP toolbar injector."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import stat
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from .json_support import strict_json_loads

SETTINGS_VERSION = 1
DEFAULT_CDP_PORT = 9222
MIN_CDP_PORT = 1024
MAX_CDP_PORT = 65535
MAX_LOCAL_RESPONSE_BYTES = 256 * 1024
MAX_STATE_FILE_BYTES = 64 * 1024
MAX_LOCK_FILE_BYTES = 64
STATUS_FRESH_SECONDS = 5.0
DAEMON_RESTART_TIMEOUT_SECONDS = 5.0


def _codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def _state_dir() -> Path:
    return _codex_home() / "codex-trajectory"


def settings_path() -> Path:
    """Return the private plugin-owned toolbar settings path."""
    return _state_dir() / "cdp-toolbar.json"


def status_path() -> Path:
    """Return the private injector heartbeat path."""
    return _state_dir() / "cdp-toolbar-status.json"


def lock_path() -> Path:
    """Return the process-lock path shared by installed plugin versions."""
    return _state_dir() / "cdp-toolbar.lock"


def daemon_runtime_id() -> str:
    """Identify the installed watcher runtime without exposing its local path."""
    identity = f"{sys.executable}\0{_daemon_script().resolve()}".encode(
        "utf-8", errors="surrogateescape"
    )
    return hashlib.sha256(identity).hexdigest()


def _validate_port(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("port must be an integer.")
    if not MIN_CDP_PORT <= value <= MAX_CDP_PORT:
        raise ValueError(f"port must be between {MIN_CDP_PORT} and {MAX_CDP_PORT}.")
    return int(value)


def _read_bounded_regular(path: Path, maximum: int) -> bytes | None:
    """Read one private single-link regular file without following links."""
    if maximum <= 0 or path.parent.is_symlink():
        return None
    try:
        expected = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(expected.st_mode) or expected.st_nlink != 1 or expected.st_size > maximum:
        return None
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_dev != expected.st_dev
            or opened.st_ino != expected.st_ino
        ):
            return None
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    return raw if len(raw) <= maximum else None


def _read_object(path: Path) -> dict[str, Any]:
    raw = _read_bounded_regular(path, MAX_STATE_FILE_BYTES)
    if raw is None:
        return {}
    try:
        value = strict_json_loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def read_settings() -> dict[str, Any]:
    """Read validated settings, falling back to the disabled safe default."""
    value = _read_object(settings_path())
    enabled = value.get("enabled") is True
    port_value = value.get("port", DEFAULT_CDP_PORT)
    try:
        port = _validate_port(port_value)
    except ValueError:
        port = DEFAULT_CDP_PORT
    return {"schemaVersion": SETTINGS_VERSION, "enabled": enabled, "port": port}


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
    directory = path.parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or (path.exists() and path.is_symlink()):
        raise OSError("Refusing to write CDP settings through a symbolic link.")
    temporary = directory / f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        with suppress(OSError):
            path.chmod(0o600)
    finally:
        with suppress(OSError):
            temporary.unlink()


def write_settings(enabled: bool, port: int) -> dict[str, Any]:
    """Validate and atomically persist the user-controlled injector setting."""
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a boolean.")
    validated_port = _validate_port(port)
    value = {
        "schemaVersion": SETTINGS_VERSION,
        "enabled": enabled,
        "port": validated_port,
    }
    _atomic_write(settings_path(), value)
    return value


def write_daemon_status(value: dict[str, Any]) -> None:
    """Publish a bounded local heartbeat for the viewer's status label."""
    allowed = {
        "pid": value.get("pid"),
        "connected": value.get("connected") is True,
        "injected": value.get("injected") is True,
        "viewerServing": value.get("viewerServing") is True,
        "lastError": str(value.get("lastError") or "")[:500] or None,
        "runtimeId": (
            str(value.get("runtimeId"))
            if isinstance(value.get("runtimeId"), str) and len(value["runtimeId"]) <= 128
            else None
        ),
        "updatedAt": time.time(),
    }
    _atomic_write(status_path(), allowed)


def _pid_running(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return False
    try:
        os.kill(value, 0)
    except (OSError, OverflowError, ValueError):
        return False
    return True


def _daemon_status() -> dict[str, Any]:
    value = _read_object(status_path())
    updated = value.get("updatedAt")
    fresh = (
        not isinstance(updated, bool)
        and isinstance(updated, (int, float))
        and 0 <= time.time() - float(updated) <= STATUS_FRESH_SECONDS
    )
    running = fresh and _pid_running(value.get("pid"))
    runtime_id = value.get("runtimeId")
    return {
        "pid": value.get("pid") if running else None,
        "runtimeId": runtime_id if fresh and isinstance(runtime_id, str) else None,
        "daemonRunning": running,
        "connected": running and value.get("connected") is True,
        "injected": running and value.get("injected") is True,
        "viewerServing": running and value.get("viewerServing") is True,
        "lastError": str(value.get("lastError") or "")[:500] or None if fresh else None,
    }


def _probe_cdp(port: int) -> bool:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=0.25)
    try:
        connection.request("GET", "/json/version", headers={"Accept": "application/json"})
        response = connection.getresponse()
        if response.status != 200:
            return False
        body = response.read(MAX_LOCAL_RESPONSE_BYTES + 1)
    except (http.client.HTTPException, OSError, TimeoutError, ValueError):
        return False
    finally:
        connection.close()
    if len(body) > MAX_LOCAL_RESPONSE_BYTES:
        return False
    try:
        value = strict_json_loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return False
    websocket = value.get("webSocketDebuggerUrl") if isinstance(value, dict) else None
    return isinstance(websocket, str) and websocket.startswith(
        (f"ws://127.0.0.1:{port}/", f"ws://localhost:{port}/")
    )


def public_status() -> dict[str, Any]:
    """Return non-sensitive settings and runtime state for the app resource."""
    settings = read_settings()
    runtime = _daemon_status()
    return {
        "schemaVersion": SETTINGS_VERSION,
        "enabled": settings["enabled"],
        "port": settings["port"],
        "cdpAvailable": _probe_cdp(settings["port"]),
        "daemonRunning": runtime["daemonRunning"],
        "connected": runtime["connected"],
        "injected": runtime["injected"],
        "viewerServing": runtime["viewerServing"],
        "lastError": runtime["lastError"],
    }


def _daemon_script() -> Path:
    return Path(__file__).resolve().parent.parent / "codex_trajectory_cdp.py"


def _lock_owner_pid() -> int | None:
    raw_bytes = _read_bounded_regular(lock_path(), MAX_LOCK_FILE_BYTES)
    if raw_bytes is None:
        return None
    try:
        raw = raw_bytes.decode("ascii").strip()
        value = int(raw)
    except (UnicodeDecodeError, ValueError):
        return None
    return value if value > 0 else None


def _stop_outdated_daemon(runtime: dict[str, Any]) -> None:
    pid = runtime.get("pid")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or pid == os.getpid()
        or _lock_owner_pid() != pid
    ):
        raise OSError("Could not verify the outdated CDP watcher process.")
    settings = read_settings()
    enabled = settings["enabled"] is True
    port = int(settings["port"])
    if enabled:
        write_settings(False, port)
    deadline = time.monotonic() + DAEMON_RESTART_TIMEOUT_SECONDS
    try:
        while _pid_running(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if _pid_running(pid):
            raise OSError("The outdated CDP watcher did not stop safely.")
    finally:
        if enabled:
            write_settings(True, port)


def _start_watcher_process(command: list[str], script: Path) -> None:
    """Launch the watcher outside the Codex host's console and process group."""
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "cwd": str(script.parent.parent),
    }
    if os.name != "nt":
        options["start_new_session"] = True
        subprocess.Popen(command, **options)
        return

    detached_flags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    breakaway_flag = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
    try:
        subprocess.Popen(
            command,
            creationflags=detached_flags | breakaway_flag,
            **options,
        )
    except OSError:
        # Some Windows hosts do not allow a child to break away from their Job
        # Object. The detached process-group fallback still prevents console
        # coupling, while the next MCP runtime can reconcile a missing watcher.
        if not breakaway_flag:
            raise
        subprocess.Popen(command, creationflags=detached_flags, **options)


def start_daemon() -> None:
    """Start the detached watcher; its cross-process lock removes duplicate instances."""
    runtime = _daemon_status()
    if runtime["daemonRunning"]:
        if runtime.get("runtimeId") == daemon_runtime_id():
            return
        _stop_outdated_daemon(runtime)
    script = _daemon_script()
    if not script.is_file():
        raise OSError("CDP injector script is unavailable.")
    _start_watcher_process([sys.executable, str(script), "--watch"], script)


def reconcile_daemon() -> None:
    """Honor the persisted opt-in and replace a missing or outdated watcher."""
    if read_settings()["enabled"] is True:
        start_daemon()


def configure(enabled: bool, port: int) -> dict[str, Any]:
    """Persist the choice and start a watcher that applies or removes the injection."""
    write_settings(enabled, port)
    start_daemon()
    return public_status()


__all__ = [
    "DEFAULT_CDP_PORT",
    "MAX_CDP_PORT",
    "MIN_CDP_PORT",
    "configure",
    "daemon_runtime_id",
    "lock_path",
    "public_status",
    "read_settings",
    "reconcile_daemon",
    "settings_path",
    "status_path",
    "write_daemon_status",
    "write_settings",
]

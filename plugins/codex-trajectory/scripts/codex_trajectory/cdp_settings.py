"""Persist and supervise the optional local CDP toolbar injector."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import http.client
import importlib
import json
import math
import os
import secrets
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, BinaryIO

from .cdp_peer import HostIdentity, browser_shortcut_supported, discover_host_identity
from .json_support import strict_json_loads

SETTINGS_VERSION = 1
BROWSER_SHORTCUT_AVAILABLE = browser_shortcut_supported()
DAEMON_RUNTIME_REVISION = 8
DEFAULT_CDP_PORT = 9222
MIN_CDP_PORT = 1024
MAX_CDP_PORT = 65535
MAX_LOCAL_RESPONSE_BYTES = 256 * 1024
MAX_STATE_FILE_BYTES = 64 * 1024
MAX_LOCK_FILE_BYTES = 64
STATUS_FRESH_SECONDS = 5.0
DAEMON_RESTART_TIMEOUT_SECONDS = 5.0
DAEMON_START_COOLDOWN_SECONDS = 5.0
DAEMON_CONTROL_LOCK_TIMEOUT_SECONDS = 10.0
DAEMON_CONTROL_LOCK_POLL_SECONDS = 0.05
DAEMON_HANDOFF_READY_TIMEOUT_SECONDS = 5.0
PUBLIC_DAEMON_ERROR = "CDP integration is temporarily unavailable."
MCP_STARTED_AT_ENV = "CODEX_TRAJECTORY_MCP_STARTED_AT"

_DAEMON_CONTROL_LOCK = threading.Lock()
_CONTROL_LOCK_BUSY_ERRNOS = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
_last_daemon_start_key: tuple[str, bool, int, str] | None = None
_last_daemon_start_at = float("-inf")
_last_daemon_start_failed = False


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


def _control_lock_path() -> Path:
    """Return the private lock that serializes settings and watcher replacement."""
    return _state_dir() / "cdp-toolbar-control.lock"


def _watcher_executable() -> Path:
    """Use the base GUI interpreter so a venv redirector cannot reopen a console."""
    executable = Path(sys.executable)
    if os.name != "nt":
        return executable
    base_executable = Path(getattr(sys, "_base_executable", executable))
    return _windowless_watcher_executable(base_executable)


def _windowless_watcher_executable(executable: Path) -> Path:
    """Prefer pythonw beside a selected Windows interpreter."""
    windowless = executable.with_name("pythonw.exe")
    return windowless if windowless.is_file() else executable


def _watcher_pythonpath() -> str:
    """Preserve the active uv script environment for the base GUI interpreter."""
    paths: list[str] = []
    for entry in sys.path:
        if not entry:
            continue
        path = Path(entry)
        if path.is_absolute() and path.is_dir():
            value = str(path)
            if value not in paths:
                paths.append(value)
    return os.pathsep.join(paths)


def daemon_runtime_id() -> str:
    """Identify plugin watcher code independently from its launch environment."""
    identity = (f"{DAEMON_RUNTIME_REVISION}\0{_daemon_script().resolve()}").encode(
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
        "injected": BROWSER_SHORTCUT_AVAILABLE and value.get("injected") is True,
        "viewerServing": (BROWSER_SHORTCUT_AVAILABLE and value.get("viewerServing") is True),
        # Watcher exceptions can contain private paths or peer-controlled CDP text.
        "lastError": _public_daemon_error(value.get("lastError")),
        "runtimeId": (
            str(value.get("runtimeId"))
            if isinstance(value.get("runtimeId"), str) and len(value["runtimeId"]) <= 128
            else None
        ),
        "mcpStartedAt": _bounded_status_number(value.get("mcpStartedAt")),
        "watcherStartedAt": _bounded_status_number(value.get("watcherStartedAt")),
        "viewerReadyAt": _bounded_status_number(value.get("viewerReadyAt")),
        "injectedAt": _bounded_status_number(value.get("injectedAt")),
        "injectionDurationMs": _bounded_status_number(
            value.get("injectionDurationMs"), maximum=60_000.0
        ),
        "updatedAt": time.time(),
    }
    _atomic_write(status_path(), allowed)


def _bounded_status_number(value: Any, *, maximum: float = 10_000_000_000.0) -> float | None:
    """Keep private timing telemetry numeric, finite, and tightly bounded."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and 0 <= number <= maximum else None


def _public_daemon_error(value: Any) -> str | None:
    """Expose only a stable status category, never exception text."""
    return PUBLIC_DAEMON_ERROR if value else None


def _pid_running(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return False
    if os.name == "nt":
        return _pid_running_windows(value)
    try:
        os.kill(value, 0)
    except (OSError, OverflowError, ValueError):
        return False
    return True


def _pid_running_windows(pid: int) -> bool:
    """Check a Windows process without sending it a console or control signal."""
    try:
        winapi: Any = importlib.import_module("_winapi")
        handle = winapi.OpenProcess(winapi.SYNCHRONIZE, False, pid)
    except (ImportError, OSError, OverflowError, ValueError):
        return False
    try:
        return bool(winapi.WaitForSingleObject(handle, 0) == winapi.WAIT_TIMEOUT)
    except (OSError, OverflowError, ValueError):
        return False
    finally:
        with suppress(OSError):
            winapi.CloseHandle(handle)


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
    result = {
        "pid": value.get("pid") if running else None,
        "runtimeId": runtime_id if fresh and isinstance(runtime_id, str) else None,
        "daemonRunning": running,
        "connected": running and value.get("connected") is True,
        "injected": running and value.get("injected") is True,
        "viewerServing": running and value.get("viewerServing") is True,
        "lastError": _public_daemon_error(value.get("lastError")) if fresh else None,
    }
    for key in (
        "mcpStartedAt",
        "watcherStartedAt",
        "viewerReadyAt",
        "injectedAt",
        "injectionDurationMs",
    ):
        result[key] = _bounded_status_number(value.get(key)) if fresh else None
    return result


def _startup_timings(runtime: dict[str, Any]) -> dict[str, float | None] | None:
    """Expose durations only, keeping absolute process timestamps private."""

    def elapsed(later_key: str, earlier_key: str) -> float | None:
        later = runtime.get(later_key)
        earlier = runtime.get(earlier_key)
        if not isinstance(later, (int, float)) or not isinstance(earlier, (int, float)):
            return None
        value = (float(later) - float(earlier)) * 1000
        return round(value, 3) if 0 <= value <= 60_000 else None

    timings = {
        "mcpToWatcherMs": elapsed("watcherStartedAt", "mcpStartedAt"),
        "watcherToViewerMs": elapsed("viewerReadyAt", "watcherStartedAt"),
        "viewerToInjectionMs": elapsed("injectedAt", "viewerReadyAt"),
        "lastInjectionDurationMs": runtime.get("injectionDurationMs"),
    }
    return timings if any(value is not None for value in timings.values()) else None


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
        "browserShortcutAvailable": BROWSER_SHORTCUT_AVAILABLE,
        "port": settings["port"],
        "cdpAvailable": settings["enabled"] is True and _probe_cdp(settings["port"]),
        "daemonRunning": runtime["daemonRunning"],
        "connected": runtime["connected"],
        "injected": runtime["injected"],
        "viewerServing": runtime["viewerServing"],
        "lastError": runtime["lastError"],
        "startupTimings": _startup_timings(runtime),
    }


def _daemon_script() -> Path:
    return Path(__file__).resolve().parent.parent / "codex_trajectory_cdp.py"


def _windows_file_user_pids(path: Path) -> set[int] | None:
    """Ask Windows Restart Manager which processes are using the watcher lock."""
    if os.name != "nt":
        return None

    class FileTime(ctypes.Structure):
        _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]

    class UniqueProcess(ctypes.Structure):
        _fields_ = [("pid", ctypes.c_ulong), ("started", FileTime)]

    class ProcessInfo(ctypes.Structure):
        _fields_ = [
            ("process", UniqueProcess),
            ("appName", ctypes.c_wchar * 256),
            ("serviceName", ctypes.c_wchar * 64),
            ("applicationType", ctypes.c_int),
            ("appStatus", ctypes.c_ulong),
            ("sessionId", ctypes.c_ulong),
            ("restartable", ctypes.c_int),
        ]

    session = ctypes.c_ulong()
    session_key = ctypes.create_unicode_buffer(33)
    try:
        dll_factory: Any = vars(ctypes).get("WinDLL")
        if not callable(dll_factory):
            return None
        restart_manager = dll_factory("rstrtmgr", use_last_error=True)
        start = restart_manager.RmStartSession
        start.argtypes = [ctypes.POINTER(ctypes.c_ulong), ctypes.c_ulong, ctypes.c_wchar_p]
        start.restype = ctypes.c_ulong
        register = restart_manager.RmRegisterResources
        register.argtypes = [
            ctypes.c_ulong,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_wchar_p),
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_void_p,
        ]
        register.restype = ctypes.c_ulong
        get_list = restart_manager.RmGetList
        get_list.argtypes = [
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ProcessInfo),
            ctypes.POINTER(ctypes.c_ulong),
        ]
        get_list.restype = ctypes.c_ulong
        end = restart_manager.RmEndSession
        end.argtypes = [ctypes.c_ulong]
        end.restype = ctypes.c_ulong

        if start(ctypes.byref(session), 0, session_key) != 0:
            return None
        try:
            resources = (ctypes.c_wchar_p * 1)(str(path.resolve()))
            if register(session, 1, resources, 0, None, 0, None) != 0:
                return None
            for _ in range(3):
                needed = ctypes.c_uint()
                available = ctypes.c_uint()
                reasons = ctypes.c_ulong()
                result = get_list(
                    session,
                    ctypes.byref(needed),
                    ctypes.byref(available),
                    None,
                    ctypes.byref(reasons),
                )
                if result == 0 and needed.value == 0:
                    return set()
                if result not in {0, 234} or needed.value > 4096:
                    return None
                process_array = (ProcessInfo * needed.value)()
                available.value = needed.value
                result = get_list(
                    session,
                    ctypes.byref(needed),
                    ctypes.byref(available),
                    process_array,
                    ctypes.byref(reasons),
                )
                if result == 0:
                    return {
                        int(process_array[index].process.pid) for index in range(available.value)
                    }
                if result != 234:
                    return None
            return None
        finally:
            end(session)
    except (AttributeError, OSError, ValueError):
        return None


def _lock_owner_pid(expected_pid: int | None = None) -> int | None:
    """Read the watcher PID, including the legacy Windows locked-byte layout."""
    raw_bytes = _read_bounded_regular(lock_path(), MAX_LOCK_FILE_BYTES)
    if raw_bytes is None:
        if os.name == "nt" and isinstance(expected_pid, int) and expected_pid > 0:
            try:
                lock_state = lock_path().lstat()
            except OSError:
                return None
            pid_size = len(str(expected_pid))
            file_user_pids = _windows_file_user_pids(lock_path())
            if (
                stat.S_ISREG(lock_state.st_mode)
                and lock_state.st_nlink == 1
                and lock_state.st_size in {pid_size, pid_size + 1}
                and file_user_pids is not None
                and expected_pid in file_user_pids
            ):
                return expected_pid
        return None
    try:
        raw = raw_bytes.decode("ascii").strip()
        value = int(raw)
    except (UnicodeDecodeError, ValueError):
        return None
    return value if value > 0 else None


def _open_control_lock() -> BinaryIO:
    """Open the private single-link regular file used for supervisor locking."""
    path = _control_lock_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise OSError("Refusing an unsafe CDP supervisor lock directory.")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    descriptor = os.open(path, flags, 0o600)
    stream: BinaryIO | None = None
    try:
        opened = os.fstat(descriptor)
        linked = os.lstat(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(linked.st_mode)
            or opened.st_dev != linked.st_dev
            or opened.st_ino != linked.st_ino
            or opened.st_nlink != 1
        ):
            raise OSError("Refusing an unsafe CDP supervisor lock file.")
        fchmod: Any = vars(os).get("fchmod")
        if callable(fchmod):
            with suppress(OSError):
                fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "r+b")
        descriptor = -1
        if os.name == "nt" and opened.st_size == 0:
            stream.seek(0)
            try:
                stream.write(b"0")
                stream.flush()
            except OSError:
                # A simultaneous creator may have initialized and locked the
                # byte between our fstat and write. The lock retry below will
                # wait for it as long as the byte now exists.
                if os.fstat(stream.fileno()).st_size == 0:
                    raise
        return stream
    except BaseException:
        if stream is not None:
            stream.close()
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _try_acquire_control_lock(stream: BinaryIO) -> None:
    stream.seek(0)
    if os.name == "nt":
        msvcrt: Any = importlib.import_module("msvcrt")
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        return

    fcntl: Any = importlib.import_module("fcntl")
    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def _cross_process_control_lock() -> Iterator[None]:
    stream = _open_control_lock()
    deadline = time.monotonic() + DAEMON_CONTROL_LOCK_TIMEOUT_SECONDS
    try:
        while True:
            try:
                _try_acquire_control_lock(stream)
                break
            except OSError as error:
                if error.errno not in _CONTROL_LOCK_BUSY_ERRNOS:
                    raise
                if time.monotonic() >= deadline:
                    raise OSError("Timed out waiting for the CDP supervisor lock.") from error
                time.sleep(DAEMON_CONTROL_LOCK_POLL_SECONDS)
        yield
    finally:
        stream.close()


@contextmanager
def _daemon_control() -> Iterator[None]:
    """Serialize daemon settings transitions within and between MCP processes."""
    with _DAEMON_CONTROL_LOCK, _cross_process_control_lock():
        yield


def _settings_revision() -> tuple[int, int, int, int] | None:
    """Return a revision that changes when the atomic settings file is replaced."""
    try:
        value = settings_path().lstat()
    except OSError:
        return None
    if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
        return None
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)


def _stop_outdated_daemon(runtime: dict[str, Any]) -> None:
    pid = runtime.get("pid")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or pid == os.getpid()
        or _lock_owner_pid(pid) != pid
    ):
        raise OSError("Could not verify the outdated CDP watcher process.")
    settings = read_settings()
    enabled = settings["enabled"] is True
    port = int(settings["port"])
    temporary_revision: tuple[int, int, int, int] | None = None
    if enabled:
        write_settings(False, port)
        temporary_revision = _settings_revision()
    deadline = time.monotonic() + DAEMON_RESTART_TIMEOUT_SECONDS
    try:
        while _pid_running(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        if _pid_running(pid):
            raise OSError("The outdated CDP watcher did not stop safely.")
    finally:
        current = read_settings()
        if (
            enabled
            and temporary_revision is not None
            and _settings_revision() == temporary_revision
            and current["enabled"] is False
            and current["port"] == port
        ):
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
    environment = os.environ.copy()
    active_pythonpath = _watcher_pythonpath()
    inherited_pythonpath = environment.get("PYTHONPATH")
    if active_pythonpath:
        environment["PYTHONPATH"] = (
            active_pythonpath
            if not inherited_pythonpath
            else os.pathsep.join((active_pythonpath, inherited_pythonpath))
        )
    options["env"] = environment
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


def _daemon_start_key() -> tuple[str, bool, int, str]:
    settings = read_settings()
    return (
        str(settings_path()),
        settings["enabled"] is True,
        int(settings["port"]),
        daemon_runtime_id(),
    )


def _handoff_ready_path() -> Path:
    """Return a unique private readiness marker for one watcher handoff."""
    return _state_dir() / f".cdp-handoff-{secrets.token_hex(16)}.ready"


def _wait_for_handoff_ready(path: Path) -> bool:
    """Wait until the replacement has bound its viewer without reading a token."""
    deadline = time.monotonic() + DAEMON_HANDOFF_READY_TIMEOUT_SECONDS
    expected = daemon_runtime_id().encode("ascii")
    while time.monotonic() < deadline:
        if _read_bounded_regular(path, 256) == expected:
            return True
        time.sleep(DAEMON_CONTROL_LOCK_POLL_SECONDS)
    return False


def _watcher_command(
    script: Path,
    *,
    host_identity: HostIdentity | None,
    handoff_ready: Path | None = None,
) -> list[str]:
    """Build one authenticated watcher command with an optional handoff gate."""
    command = [str(_watcher_executable()), str(script)]
    if host_identity is not None:
        command.extend(["--host-identity", host_identity.encode()])
    if handoff_ready is not None:
        command.extend(["--wait-for-lock", "--handoff-ready", str(handoff_ready)])
    command.append("--watch")
    return command


def _start_daemon_locked(*, cooldown: bool) -> None:
    """Start the detached watcher while the in-process supervisor lock is held."""
    global _last_daemon_start_at, _last_daemon_start_failed, _last_daemon_start_key

    runtime = _daemon_status()
    if runtime["daemonRunning"] and runtime.get("runtimeId") == daemon_runtime_id():
        _last_daemon_start_failed = False
        return
    key = _daemon_start_key()
    now = time.monotonic()
    if (
        cooldown
        and key == _last_daemon_start_key
        and 0 <= now - _last_daemon_start_at < DAEMON_START_COOLDOWN_SECONDS
    ):
        if _last_daemon_start_failed:
            raise OSError("The previous CDP watcher start attempt did not succeed.")
        return
    _last_daemon_start_key = key
    _last_daemon_start_at = now
    _last_daemon_start_failed = False
    script = _daemon_script()
    if not script.is_file():
        _last_daemon_start_failed = True
        raise OSError("CDP injector script is unavailable.")
    outdated = runtime["daemonRunning"]
    host_identity = discover_host_identity()
    handoff_ready = _handoff_ready_path() if outdated and host_identity is not None else None
    command = _watcher_command(
        script,
        host_identity=host_identity,
        handoff_ready=handoff_ready,
    )
    try:
        # Without a verifiable host identity the replacement cannot safely bind
        # a viewer before owning the watcher lock, so keep the proven fallback.
        if outdated and handoff_ready is None:
            _stop_outdated_daemon(runtime)
        _start_watcher_process(command, script)
        if handoff_ready is not None:
            try:
                if not _wait_for_handoff_ready(handoff_ready):
                    raise OSError("The replacement CDP watcher did not become ready.")
                _stop_outdated_daemon(runtime)
            finally:
                with suppress(OSError):
                    handoff_ready.unlink()
    except OSError:
        _last_daemon_start_failed = True
        raise


def start_daemon() -> None:
    """Start the detached watcher; its cross-process lock removes duplicate instances."""
    with _daemon_control():
        _start_daemon_locked(cooldown=False)


def reconcile_daemon() -> None:
    """Honor the persisted opt-in and replace a missing or outdated watcher."""
    with _daemon_control():
        if read_settings()["enabled"] is True:
            _start_daemon_locked(cooldown=True)


def recover_daemon(expected_port: int) -> dict[str, Any]:
    """Restart a missing opted-in watcher without overwriting a newer user setting."""
    validated_port = _validate_port(expected_port)
    with _daemon_control():
        settings = read_settings()
        if settings["enabled"] is True and settings["port"] == validated_port:
            _start_daemon_locked(cooldown=True)
    return public_status()


def configure(enabled: bool, port: int) -> dict[str, Any]:
    """Persist the choice and start a watcher that applies or removes the injection."""
    with _daemon_control():
        write_settings(enabled, port)
        _start_daemon_locked(cooldown=False)
    return public_status()


__all__ = [
    "BROWSER_SHORTCUT_AVAILABLE",
    "DEFAULT_CDP_PORT",
    "MAX_CDP_PORT",
    "MIN_CDP_PORT",
    "configure",
    "daemon_runtime_id",
    "lock_path",
    "public_status",
    "read_settings",
    "reconcile_daemon",
    "recover_daemon",
    "settings_path",
    "status_path",
    "write_daemon_status",
    "write_settings",
]

"""Bind token-bearing CDP connections to the Codex desktop host process."""

from __future__ import annotations

import base64
import ctypes
import json
import ntpath
import os
import socket
import struct
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MACOS_CODE_REQUIREMENT = (
    'identifier "com.openai.codex" and anchor apple generic and '
    'certificate leaf[subject.OU] = "2DC432GLL2"'
)
WINDOWS_PACKAGE_PREFIX = "OpenAI.Codex_"
MAX_PROCESS_DEPTH = 32
MAX_COMMAND_OUTPUT = 64 * 1024


class PeerAuthenticationError(RuntimeError):
    """The connected loopback peer could not be bound to the Codex host."""


@dataclass(frozen=True)
class HostIdentity:
    """Stable-enough identity for the desktop process that launched the plugin."""

    platform: str
    pid: int
    started: str
    executable: str
    package_family: str | None = None

    def encode(self) -> str:
        value = {
            "platform": self.platform,
            "pid": self.pid,
            "started": self.started,
            "executable": self.executable,
            "packageFamily": self.package_family,
        }
        raw = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @classmethod
    def decode(cls, value: str) -> HostIdentity:
        if not isinstance(value, str) or not 1 <= len(value) <= 8192:
            raise ValueError("invalid host identity")
        try:
            padding = "=" * (-len(value) % 4)
            decoded: Any = json.loads(base64.urlsafe_b64decode(value + padding))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("invalid host identity") from error
        if not isinstance(decoded, dict) or set(decoded) != {
            "platform",
            "pid",
            "started",
            "executable",
            "packageFamily",
        }:
            raise ValueError("invalid host identity")
        platform = decoded.get("platform")
        pid = decoded.get("pid")
        started = decoded.get("started")
        executable = decoded.get("executable")
        package_family = decoded.get("packageFamily")
        if (
            platform not in {"darwin", "win32"}
            or isinstance(pid, bool)
            or not isinstance(pid, int)
            or pid <= 0
            or not isinstance(started, str)
            or not 1 <= len(started) <= 256
            or not isinstance(executable, str)
            or not 1 <= len(executable) <= 4096
            or (package_family is not None and not isinstance(package_family, str))
        ):
            raise ValueError("invalid host identity")
        return cls(platform, pid, started, executable, package_family)


def browser_shortcut_supported() -> bool:
    """Return whether this operating system has peer-process binding support."""
    return sys.platform == "darwin" or os.name == "nt"


def discover_host_identity(start_pid: int | None = None) -> HostIdentity | None:
    """Find and authenticate the Codex desktop ancestor of this plugin process."""
    pid = os.getpid() if start_pid is None else start_pid
    if sys.platform == "darwin":
        return _discover_macos_host(pid)
    if os.name == "nt":
        return _discover_windows_host(pid)
    return None


def host_identity_alive(identity: HostIdentity) -> bool:
    """Reject PID reuse and host replacement after the watcher was launched."""
    if identity.platform == "darwin" and sys.platform == "darwin":
        mac_info = _macos_process_info(identity.pid)
        return (
            mac_info is not None
            and mac_info[1] == identity.started
            and mac_info[2] == identity.executable
        )
    if identity.platform == "win32" and os.name == "nt":
        win_info = _windows_process_info(identity.pid)
        return (
            win_info is not None
            and win_info[1] == identity.started
            and win_info[2].casefold() == identity.executable.casefold()
            and win_info[3] == identity.package_family
        )
    return False


def authenticate_connected_peer(sock: socket.socket, identity: HostIdentity) -> int:
    """Return the CDP server PID only when this exact connection belongs to Codex."""
    if not host_identity_alive(identity):
        raise PeerAuthenticationError("The Codex host identity is no longer valid.")
    local = _ipv4_endpoint(sock.getsockname())
    peer = _ipv4_endpoint(sock.getpeername())
    if local[0] != "127.0.0.1" or peer[0] != "127.0.0.1":
        raise PeerAuthenticationError("The CDP connection was not IPv4 loopback-only.")
    if identity.platform == "darwin" and sys.platform == "darwin":
        peer_pid = _macos_connected_peer_pid(local, peer)
        if not _macos_peer_matches_host(peer_pid, identity):
            raise PeerAuthenticationError("The CDP peer was not the authenticated Codex host.")
        return peer_pid
    if identity.platform == "win32" and os.name == "nt":
        peer_pid = _windows_connected_peer_pid(local, peer)
        if not _windows_peer_matches_host(peer_pid, identity):
            raise PeerAuthenticationError("The CDP peer was not the authenticated Codex host.")
        return peer_pid
    raise PeerAuthenticationError("CDP peer authentication is unsupported on this platform.")


def _ipv4_endpoint(value: Any) -> tuple[str, int]:
    if (
        not isinstance(value, tuple)
        or len(value) < 2
        or not isinstance(value[0], str)
        or isinstance(value[1], bool)
        or not isinstance(value[1], int)
    ):
        raise PeerAuthenticationError("The CDP socket endpoint was invalid.")
    return value[0], value[1]


def _run_bounded(command: list[str], timeout: float = 2.0) -> str:
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise PeerAuthenticationError("An operating-system identity check failed.") from error
    if len(result.stdout) > MAX_COMMAND_OUTPUT or len(result.stderr) > MAX_COMMAND_OUTPUT:
        raise PeerAuthenticationError("An operating-system identity response was too large.")
    try:
        return result.stdout.decode("utf-8")
    except UnicodeDecodeError as error:
        raise PeerAuthenticationError(
            "An operating-system identity response was invalid."
        ) from error


def _macos_process_path(pid: int) -> str | None:
    try:
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        function = libproc.proc_pidpath
        function.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        function.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(4096)
        length = int(function(pid, buffer, len(buffer)))
    except (AttributeError, OSError, ValueError):
        return None
    if length <= 0 or length >= len(buffer):
        return None
    try:
        return str(Path(os.fsdecode(buffer.raw[:length])).resolve(strict=True))
    except (OSError, UnicodeError):
        return None


def _macos_process_info(pid: int) -> tuple[int, str, str] | None:
    if pid <= 0:
        return None
    try:
        ppid = int(_run_bounded(["/bin/ps", "-p", str(pid), "-o", "ppid="]).strip())
        started = _run_bounded(["/bin/ps", "-p", str(pid), "-o", "lstart="]).strip()
    except (PeerAuthenticationError, ValueError):
        return None
    executable = _macos_process_path(pid)
    if not started or executable is None:
        return None
    return ppid, started, executable


def _macos_app_bundle(executable: str) -> Path | None:
    path = Path(executable)
    for parent in path.parents:
        if parent.suffix == ".app":
            return parent
    return None


def _verify_macos_codex(executable: str) -> bool:
    bundle = _macos_app_bundle(executable)
    if bundle is None:
        return False
    try:
        relative = Path(executable).relative_to(bundle)
    except ValueError:
        return False
    if relative.parts[:2] != ("Contents", "MacOS") or relative.name not in {
        "ChatGPT",
        "Codex",
    }:
        return False
    try:
        _run_bounded(
            [
                "/usr/bin/codesign",
                "--verify",
                "--strict",
                f"-R={MACOS_CODE_REQUIREMENT}",
                str(bundle),
            ],
            timeout=5.0,
        )
    except PeerAuthenticationError:
        return False
    return True


def _discover_macos_host(start_pid: int) -> HostIdentity | None:
    pid = start_pid
    seen: set[int] = set()
    for _ in range(MAX_PROCESS_DEPTH):
        if pid <= 1 or pid in seen:
            return None
        seen.add(pid)
        info = _macos_process_info(pid)
        if info is None:
            return None
        ppid, started, executable = info
        if _verify_macos_codex(executable):
            return HostIdentity("darwin", pid, started, executable)
        pid = ppid
    return None


def _parse_lsof_records(value: str) -> list[tuple[int, str]]:
    records: list[tuple[int, str]] = []
    pid: int | None = None
    for line in value.splitlines():
        if line.startswith("p"):
            try:
                pid = int(line[1:])
            except ValueError:
                pid = None
        elif line.startswith("n") and pid is not None:
            records.append((pid, line[1:]))
    return records


def _macos_connected_peer_pid(
    local: tuple[str, int],
    peer: tuple[str, int],
) -> int:
    expected = f"{peer[0]}:{peer[1]}->{local[0]}:{local[1]}"
    output = _run_bounded(
        [
            "/usr/sbin/lsof",
            "-nP",
            "-a",
            f"-iTCP@{peer[0]}:{peer[1]}",
            "-sTCP:ESTABLISHED",
            "-Fpn",
        ]
    )
    matches = {pid for pid, endpoint in _parse_lsof_records(output) if endpoint == expected}
    if len(matches) != 1:
        raise PeerAuthenticationError("The macOS CDP peer owner was ambiguous.")
    return matches.pop()


def _macos_peer_matches_host(peer_pid: int, identity: HostIdentity) -> bool:
    pid = peer_pid
    seen: set[int] = set()
    for _ in range(MAX_PROCESS_DEPTH):
        if pid <= 0 or pid in seen:
            return False
        seen.add(pid)
        info = _macos_process_info(pid)
        if info is None:
            return False
        ppid, started, executable = info
        if pid == identity.pid:
            return started == identity.started and executable == identity.executable
        pid = ppid
    return False


def _windows_dll(name: str) -> Any:
    factory: Any = vars(ctypes).get("WinDLL")
    if not callable(factory):
        raise AttributeError("ctypes.WinDLL is unavailable")
    return factory(name, use_last_error=True)


def _windows_process_table() -> dict[int, int]:
    kernel32 = _windows_dll("kernel32")

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", ctypes.c_ulong),
            ("cntUsage", ctypes.c_ulong),
            ("th32ProcessID", ctypes.c_ulong),
            ("th32DefaultHeapID", ctypes.c_void_p),
            ("th32ModuleID", ctypes.c_ulong),
            ("cntThreads", ctypes.c_ulong),
            ("th32ParentProcessID", ctypes.c_ulong),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", ctypes.c_ulong),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    create_snapshot = kernel32.CreateToolhelp32Snapshot
    create_snapshot.argtypes = [ctypes.c_ulong, ctypes.c_ulong]
    create_snapshot.restype = ctypes.c_void_p
    process_first = kernel32.Process32FirstW
    process_first.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessEntry)]
    process_first.restype = ctypes.c_int
    process_next = kernel32.Process32NextW
    process_next.argtypes = [ctypes.c_void_p, ctypes.POINTER(ProcessEntry)]
    process_next.restype = ctypes.c_int
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    close_handle.restype = ctypes.c_int

    snapshot = create_snapshot(0x00000002, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise PeerAuthenticationError("Windows process discovery failed.")

    entry = ProcessEntry()
    entry.dwSize = ctypes.sizeof(ProcessEntry)
    table: dict[int, int] = {}
    try:
        more = bool(process_first(snapshot, ctypes.byref(entry)))
        while more:
            table[int(entry.th32ProcessID)] = int(entry.th32ParentProcessID)
            more = bool(process_next(snapshot, ctypes.byref(entry)))
    finally:
        close_handle(snapshot)
    return table


def _windows_process_info(pid: int) -> tuple[int, str, str, str | None] | None:
    try:
        table = _windows_process_table()
        ppid = table.get(pid)
        if ppid is None:
            return None
        kernel32 = _windows_dll("kernel32")

        class FileTime(ctypes.Structure):
            _fields_ = [("low", ctypes.c_ulong), ("high", ctypes.c_ulong)]

        open_process = kernel32.OpenProcess
        open_process.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        open_process.restype = ctypes.c_void_p
        query_image = kernel32.QueryFullProcessImageNameW
        query_image.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        query_image.restype = ctypes.c_int
        get_process_times = kernel32.GetProcessTimes
        get_process_times.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
            ctypes.POINTER(FileTime),
        ]
        get_process_times.restype = ctypes.c_int
        get_package_family = kernel32.GetPackageFamilyName
        get_package_family.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.c_void_p,
        ]
        get_package_family.restype = ctypes.c_long
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [ctypes.c_void_p]
        close_handle.restype = ctypes.c_int

        handle = open_process(0x1000, False, pid)
        if not handle:
            return None
        try:
            length = ctypes.c_ulong(32768)
            path_buffer = ctypes.create_unicode_buffer(length.value)
            if not query_image(handle, 0, path_buffer, ctypes.byref(length)):
                return None
            creation = FileTime()
            exit_time = FileTime()
            kernel = FileTime()
            user = FileTime()
            if not get_process_times(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ):
                return None
            package_family = _windows_package_family(get_package_family, handle)
        finally:
            close_handle(handle)
    except (AttributeError, OSError, OverflowError, ValueError):
        return None
    creation_token = (int(creation.high) << 32) | int(creation.low)
    return ppid, str(creation_token), str(Path(path_buffer.value)), package_family


def _windows_package_family(get_package_family: Any, handle: Any) -> str | None:
    length = ctypes.c_uint32(0)
    result = int(get_package_family(handle, ctypes.byref(length), None))
    if result == 15700:  # APPMODEL_ERROR_NO_PACKAGE
        return None
    if result not in {0, 122} or length.value <= 1 or length.value > 512:
        return None
    buffer = ctypes.create_unicode_buffer(length.value)
    if int(get_package_family(handle, ctypes.byref(length), buffer)) != 0:
        return None
    return str(buffer.value)


def _discover_windows_host(start_pid: int) -> HostIdentity | None:
    try:
        table = _windows_process_table()
    except PeerAuthenticationError:
        return None
    pid = start_pid
    seen: set[int] = set()
    for _ in range(MAX_PROCESS_DEPTH):
        if pid <= 1 or pid in seen:
            return None
        seen.add(pid)
        info = _windows_process_info(pid)
        if info is None:
            return None
        ppid, started, executable, package_family = info
        if (
            ntpath.basename(executable).casefold() in {"chatgpt.exe", "codex.exe"}
            and isinstance(package_family, str)
            and package_family.startswith(WINDOWS_PACKAGE_PREFIX)
        ):
            return HostIdentity("win32", pid, started, executable, package_family)
        pid = table.get(pid, ppid)
    return None


def _windows_tcp_rows() -> list[tuple[str, int, str, int, int, int]]:
    iphlpapi = _windows_dll("iphlpapi")
    get_tcp_table = iphlpapi.GetExtendedTcpTable
    get_tcp_table.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_int,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_ulong,
    ]
    get_tcp_table.restype = ctypes.c_ulong
    size = ctypes.c_ulong(0)
    result = int(get_tcp_table(None, ctypes.byref(size), False, 2, 5, 0))
    if result not in {0, 122} or size.value < 4 or size.value > 16 * 1024 * 1024:
        raise PeerAuthenticationError("Windows TCP owner discovery failed.")
    buffer = ctypes.create_string_buffer(size.value)
    result = int(get_tcp_table(buffer, ctypes.byref(size), False, 2, 5, 0))
    if result != 0:
        raise PeerAuthenticationError("Windows TCP owner discovery failed.")
    count = struct.unpack_from("<I", buffer.raw, 0)[0]
    row_size = struct.calcsize("<IIIIII")
    if count > 1_000_000 or 4 + count * row_size > size.value:
        raise PeerAuthenticationError("Windows TCP owner data was invalid.")
    rows: list[tuple[str, int, str, int, int, int]] = []
    for index in range(count):
        state, local_address, local_port, remote_address, remote_port, pid = struct.unpack_from(
            "<IIIIII", buffer.raw, 4 + index * row_size
        )
        rows.append(
            (
                socket.inet_ntoa(struct.pack("=I", local_address)),
                socket.ntohs(local_port & 0xFFFF),
                socket.inet_ntoa(struct.pack("=I", remote_address)),
                socket.ntohs(remote_port & 0xFFFF),
                state,
                pid,
            )
        )
    return rows


def _windows_connected_peer_pid(
    local: tuple[str, int],
    peer: tuple[str, int],
) -> int:
    matches = {
        pid
        for local_address, local_port, remote_address, remote_port, state, pid in (
            _windows_tcp_rows()
        )
        if state == 5
        and (local_address, local_port) == peer
        and (remote_address, remote_port) == local
    }
    if len(matches) != 1:
        raise PeerAuthenticationError("The Windows CDP peer owner was ambiguous.")
    return matches.pop()


def _windows_peer_matches_host(peer_pid: int, identity: HostIdentity) -> bool:
    try:
        table = _windows_process_table()
    except PeerAuthenticationError:
        return False
    pid = peer_pid
    seen: set[int] = set()
    for _ in range(MAX_PROCESS_DEPTH):
        if pid <= 0 or pid in seen:
            return False
        seen.add(pid)
        info = _windows_process_info(pid)
        if info is None:
            return False
        ppid, started, executable, package_family = info
        if package_family != identity.package_family:
            return False
        if pid == identity.pid:
            return (
                started == identity.started
                and executable.casefold() == identity.executable.casefold()
            )
        pid = table.get(pid, ppid)
    return False


__all__ = [
    "HostIdentity",
    "PeerAuthenticationError",
    "authenticate_connected_peer",
    "browser_shortcut_supported",
    "discover_host_identity",
    "host_identity_alive",
]

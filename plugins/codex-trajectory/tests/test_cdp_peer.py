"""OS peer-process binding coverage for token-bearing CDP connections."""

from __future__ import annotations

import os
import socket
import struct
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from codex_trajectory import cdp_peer
from codex_trajectory.cdp_peer import HostIdentity, PeerAuthenticationError


def mac_identity(pid: int = 100) -> HostIdentity:
    return HostIdentity(
        "darwin",
        pid,
        "Mon Aug 24 18:01:41 2026",
        "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT",
    )


def windows_identity(pid: int = 100) -> HostIdentity:
    return HostIdentity(
        "win32",
        pid,
        "134000000000000000",
        r"C:\Program Files\WindowsApps\OpenAI.Codex_1.0.0.0_x64__publisher\app\ChatGPT.exe",
        "OpenAI.Codex_publisher",
    )


def test_host_identity_round_trip_and_validation() -> None:
    for identity in (mac_identity(), windows_identity()):
        assert HostIdentity.decode(identity.encode()) == identity

    for invalid in ("", "not-base64", HostIdentity("darwin", 1, "s", "e").encode() + "x"):
        with pytest.raises(ValueError, match="host identity"):
            HostIdentity.decode(invalid)


def test_macos_host_discovery_requires_the_signed_codex_ancestor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    processes = {
        300: (200, "child-start", "/usr/bin/python3"),
        200: (100, "server-start", "/Applications/ChatGPT.app/Contents/Resources/codex"),
        100: (
            1,
            "host-start",
            "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT",
        ),
    }
    monkeypatch.setattr(cdp_peer, "_macos_process_info", processes.get)
    monkeypatch.setattr(
        cdp_peer,
        "_verify_macos_codex",
        lambda executable: executable.endswith("/Contents/MacOS/ChatGPT"),
    )

    assert cdp_peer._discover_macos_host(300) == HostIdentity(
        "darwin",
        100,
        "host-start",
        "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT",
    )

    monkeypatch.setattr(cdp_peer, "_verify_macos_codex", lambda _executable: False)
    assert cdp_peer._discover_macos_host(300) is None


def test_macos_lsof_parser_requires_one_exact_server_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = "\n".join(
        [
            "p100",
            "f8",
            "n127.0.0.1:9222->127.0.0.1:51004",
            "p200",
            "f9",
            "n127.0.0.1:51004->127.0.0.1:9222",
        ]
    )
    monkeypatch.setattr(cdp_peer, "_run_bounded", lambda _command: output)
    assert cdp_peer._macos_connected_peer_pid(("127.0.0.1", 51004), ("127.0.0.1", 9222)) == 100

    monkeypatch.setattr(
        cdp_peer,
        "_run_bounded",
        lambda _command: output + "\np101\nn127.0.0.1:9222->127.0.0.1:51004\n",
    )
    with pytest.raises(PeerAuthenticationError, match="ambiguous"):
        cdp_peer._macos_connected_peer_pid(("127.0.0.1", 51004), ("127.0.0.1", 9222))


def test_windows_host_and_peer_require_one_package_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = windows_identity()
    table = {300: 200, 200: 100, 100: 1}
    processes: dict[int, tuple[int, str, str, str | None]] = {
        300: (200, "child", r"C:\Python\python.exe", "OpenAI.Codex_publisher"),
        200: (100, "renderer", r"C:\Codex\Renderer.exe", "OpenAI.Codex_publisher"),
        100: (1, identity.started, identity.executable, identity.package_family),
    }
    monkeypatch.setattr(cdp_peer, "_windows_process_table", lambda: table)
    monkeypatch.setattr(cdp_peer, "_windows_process_info", processes.get)

    assert cdp_peer._discover_windows_host(300) == identity
    assert cdp_peer._windows_peer_matches_host(200, identity) is True

    processes[200] = (100, "renderer", r"C:\Malware\fake.exe", "Other.App_attacker")
    assert cdp_peer._windows_peer_matches_host(200, identity) is False


def test_windows_host_discovery_selects_outer_desktop_above_packaged_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desktop = windows_identity(pid=100)
    app_server = HostIdentity(
        "win32",
        200,
        "app-server-start",
        r"C:\Program Files\WindowsApps\OpenAI.Codex_1.0.0.0_x64__publisher\app\resources\codex.exe",
        desktop.package_family,
    )
    table = {400: 300, 300: 200, 200: 100, 100: 1}
    processes: dict[int, tuple[int, str, str, str | None]] = {
        400: (300, "python", r"C:\Python\python.exe", None),
        300: (200, "uv", r"C:\Tools\uv.exe", None),
        200: (100, app_server.started, app_server.executable, app_server.package_family),
        100: (1, desktop.started, desktop.executable, desktop.package_family),
    }
    monkeypatch.setattr(cdp_peer, "_windows_process_table", lambda: table)
    monkeypatch.setattr(cdp_peer, "_windows_process_info", processes.get)

    assert cdp_peer._discover_windows_host(400) == desktop
    assert cdp_peer._windows_peer_matches_host(100, desktop) is True
    assert cdp_peer._windows_peer_matches_host(200, desktop) is True


def test_windows_tcp_owner_match_is_exact_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = ("127.0.0.1", 51004)
    peer = ("127.0.0.1", 9222)
    monkeypatch.setattr(
        cdp_peer,
        "_windows_tcp_rows",
        lambda: [
            ("127.0.0.1", 9222, "127.0.0.1", 51004, 5, 100),
            ("127.0.0.1", 51004, "127.0.0.1", 9222, 5, 200),
        ],
    )
    assert cdp_peer._windows_connected_peer_pid(local, peer) == 100

    monkeypatch.setattr(
        cdp_peer,
        "_windows_tcp_rows",
        lambda: [
            ("127.0.0.1", 9222, "127.0.0.1", 51004, 5, 100),
            ("127.0.0.1", 9222, "127.0.0.1", 51004, 5, 101),
        ],
    )
    with pytest.raises(PeerAuthenticationError, match="ambiguous"):
        cdp_peer._windows_connected_peer_pid(local, peer)


def test_windows_process_table_reads_and_closes_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []
    entries = iter(((300, 200), (200, 100)))

    class FakeFunction:
        def __init__(self, function: Any) -> None:
            self.function = function

        def __call__(self, *args: Any) -> Any:
            return self.function(*args)

    def read_entry(_snapshot: int, entry_pointer: Any) -> int:
        try:
            pid, ppid = next(entries)
        except StopIteration:
            return 0
        entry_pointer._obj.th32ProcessID = pid
        entry_pointer._obj.th32ParentProcessID = ppid
        return 1

    kernel32 = SimpleNamespace(
        CreateToolhelp32Snapshot=FakeFunction(lambda _flags, _pid: 123),
        Process32FirstW=FakeFunction(read_entry),
        Process32NextW=FakeFunction(read_entry),
        CloseHandle=FakeFunction(lambda handle: closed.append(handle) or 1),
    )
    monkeypatch.setattr(cdp_peer, "_windows_dll", lambda name: kernel32)

    assert cdp_peer._windows_process_table() == {300: 200, 200: 100}
    assert closed == [123]


def test_windows_process_info_reads_image_start_time_and_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[int] = []

    class FakeFunction:
        def __init__(self, function: Any) -> None:
            self.function = function

        def __call__(self, *args: Any) -> Any:
            return self.function(*args)

    def query_image(_handle: int, _flags: int, buffer: Any, length: Any) -> int:
        buffer.value = r"C:\Program Files\WindowsApps\OpenAI.Codex\ChatGPT.exe"
        length._obj.value = len(buffer.value)
        return 1

    def get_process_times(
        _handle: int,
        creation: Any,
        _exit_time: Any,
        _kernel: Any,
        _user: Any,
    ) -> int:
        creation._obj.low = 7
        creation._obj.high = 2
        return 1

    def get_package_family(_handle: int, length: Any, buffer: Any) -> int:
        family = "OpenAI.Codex_publisher"
        if buffer is None:
            length._obj.value = len(family) + 1
            return 122
        buffer.value = family
        return 0

    kernel32 = SimpleNamespace(
        OpenProcess=FakeFunction(lambda _access, _inherit, _pid: 456),
        QueryFullProcessImageNameW=FakeFunction(query_image),
        GetProcessTimes=FakeFunction(get_process_times),
        GetPackageFamilyName=FakeFunction(get_package_family),
        CloseHandle=FakeFunction(lambda handle: closed.append(handle) or 1),
    )
    monkeypatch.setattr(cdp_peer, "_windows_process_table", lambda: {300: 200})
    monkeypatch.setattr(cdp_peer, "_windows_dll", lambda name: kernel32)

    assert cdp_peer._windows_process_info(300) == (
        200,
        str((2 << 32) | 7),
        r"C:\Program Files\WindowsApps\OpenAI.Codex\ChatGPT.exe",
        "OpenAI.Codex_publisher",
    )
    assert closed == [456]


def test_windows_tcp_table_decodes_the_owner_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = struct.pack(
        "<IIIIII",
        5,
        struct.unpack("=I", socket.inet_aton("127.0.0.1"))[0],
        socket.htons(9222),
        struct.unpack("=I", socket.inet_aton("127.0.0.1"))[0],
        socket.htons(51004),
        100,
    )
    payload = struct.pack("<I", 1) + row

    class FakeFunction:
        def __call__(self, buffer: Any, size: Any, *_args: Any) -> int:
            size._obj.value = len(payload)
            if buffer is None:
                return 122
            cdp_peer.ctypes.memmove(buffer, payload, len(payload))
            return 0

    iphlpapi = SimpleNamespace(GetExtendedTcpTable=FakeFunction())
    monkeypatch.setattr(cdp_peer, "_windows_dll", lambda name: iphlpapi)

    assert cdp_peer._windows_tcp_rows() == [("127.0.0.1", 9222, "127.0.0.1", 51004, 5, 100)]


def test_authentication_rejects_non_loopback_before_owner_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeSocket:
        def getsockname(self) -> tuple[str, int]:
            return "127.0.0.1", 51004

        def getpeername(self) -> tuple[str, int]:
            return "192.0.2.10", 9222

    monkeypatch.setattr(cdp_peer, "host_identity_alive", lambda _identity: True)
    with pytest.raises(PeerAuthenticationError, match="loopback"):
        cdp_peer.authenticate_connected_peer(FakeSocket(), mac_identity())  # type: ignore[arg-type]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS kernel owner lookup")
def test_macos_kernel_reports_the_exact_connected_server_pid() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = int(listener.getsockname()[1])
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            accepted, _address = listener.accept()
            with accepted:
                local = cdp_peer._ipv4_endpoint(client.getsockname())
                peer = cdp_peer._ipv4_endpoint(client.getpeername())
                assert cdp_peer._macos_connected_peer_pid(local, peer) == os.getpid()


@pytest.mark.skipif(os.name != "nt", reason="Windows kernel owner lookup")
def test_windows_kernel_reports_the_exact_connected_server_pid() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = int(listener.getsockname()[1])
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            accepted, _address = listener.accept()
            with accepted:
                local = cdp_peer._ipv4_endpoint(client.getsockname())
                peer = cdp_peer._ipv4_endpoint(client.getpeername())
                assert cdp_peer._windows_connected_peer_pid(local, peer) == os.getpid()


def test_host_identity_decode_rejects_oversized_input() -> None:
    with pytest.raises(ValueError, match="host identity"):
        HostIdentity.decode("x" * 9000)


def test_process_info_helpers_fail_closed_on_os_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_run(*_args: Any, **_kwargs: Any) -> Any:
        raise OSError("unavailable")

    monkeypatch.setattr(cdp_peer.subprocess, "run", fail_run)
    assert cdp_peer._macos_process_info(123) is None

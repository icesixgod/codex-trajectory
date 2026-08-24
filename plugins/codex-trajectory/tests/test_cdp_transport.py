"""Live loopback-CDP coverage for the in-app Browser shortcut transport."""

from __future__ import annotations

import json
import os
import re
import socket
from pathlib import Path

import codex_trajectory_cdp
import pytest
from codex_trajectory.cdp_peer import HostIdentity
from codex_trajectory_cdp import (
    REMOVE_SOURCE,
    _acquire_process_lock,
    _inject_cycle,
    _read_active_task_state,
    _read_codex_theme,
    _request_active_task_stop,
    _targets,
    request_task_stop,
)
from playwright.sync_api import sync_playwright


def unused_loopback_port() -> int:
    """Reserve and release an ephemeral loopback port for Chromium startup."""
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def test_targets_include_codex_webviews(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        codex_trajectory_cdp,
        "_http_json",
        lambda _port, _route, **_kwargs: [
            {"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/page"},
            {"type": "iframe", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/iframe"},
            {"type": "webview", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/webview"},
            {"type": "worker", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/worker"},
        ],
    )

    assert [target["type"] for target in _targets(9222)] == ["page", "iframe", "webview"]


HOST_IDENTITY = HostIdentity(
    "darwin",
    4321,
    "Mon Aug 24 18:01:41 2026",
    "/Applications/ChatGPT.app/Contents/MacOS/ChatGPT",
)


def test_injection_sends_the_viewer_token_only_to_an_authenticated_codex_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets = [
        {
            "url": "https://example.invalid/",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/external",
        },
        {
            "url": "app://-/index.html?initialRoute=%2Ftask",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/codex",
        },
    ]
    monkeypatch.setattr(codex_trajectory_cdp, "_targets", lambda _port, **_kwargs: targets)
    evaluated: dict[str, list[str]] = {}
    identities: dict[str, HostIdentity | None] = {}

    class Connection:
        def __init__(
            self,
            url: str,
            *,
            host_identity: HostIdentity | None = None,
            **_kwargs: object,
        ) -> None:
            self.url = url
            identities[url] = host_identity

        def close(self) -> None:
            return None

    def fake_evaluate(connection: Connection, source: str, **_kwargs: object) -> object:
        evaluated.setdefault(connection.url, []).append(source)
        return {"installed": True, "visible": True}

    monkeypatch.setattr(codex_trajectory_cdp, "WebSocketConnection", Connection)
    monkeypatch.setattr(codex_trajectory_cdp, "_evaluate", fake_evaluate)

    assert _inject_cycle(
        9222,
        True,
        "http://127.0.0.1:43123/private-token/",
        HOST_IDENTITY,
    ) == (
        True,
        True,
    )
    assert evaluated["ws://127.0.0.1:9222/external"] == [REMOVE_SOURCE]
    assert "private-token" in evaluated["ws://127.0.0.1:9222/codex"][0]
    assert identities["ws://127.0.0.1:9222/external"] is None
    assert identities["ws://127.0.0.1:9222/codex"] == HOST_IDENTITY


def test_enabled_injection_fails_closed_without_host_authentication() -> None:
    with pytest.raises(ValueError, match="authenticated host"):
        _inject_cycle(9222, True, "http://127.0.0.1:43123/private-token/")


def test_state_and_stop_continue_past_an_unusable_codex_renderer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    targets = [
        {
            "url": "app://-/index.html",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/unavailable",
        },
        {
            "url": "app://-/index.html",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/healthy",
        },
    ]

    class Connection:
        def __init__(self, url: str, **_kwargs: object) -> None:
            self.url = url

        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def close(self) -> None:
            return None

    def fake_evaluate(connection: Connection, source: str, **_kwargs: object) -> object:
        if connection.url.endswith("/unavailable"):
            return {"matched": True, "reason": "bridge-unavailable"}
        if "turn/interrupt" in source:
            return {"matched": True, "sent": True}
        return {"matched": True, "running": True, "turnId": "turn-healthy"}

    monkeypatch.setattr(codex_trajectory_cdp, "_targets", lambda _port, **_kwargs: targets)
    monkeypatch.setattr(codex_trajectory_cdp, "WebSocketConnection", Connection)
    monkeypatch.setattr(codex_trajectory_cdp, "_evaluate", fake_evaluate)

    assert _read_active_task_state(9222, "session-alpha", "turn-candidate") == {
        "running": True,
        "turnId": "turn-healthy",
    }
    assert _request_active_task_stop(
        9222,
        {
            "sessionId": "session-alpha",
            "turnId": "turn-healthy",
            "source": "manual",
            "threshold": 10,
            "language": "en",
        },
    ) == {"sent": True}


def test_stop_entrypoints_recheck_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = {
        "sessionId": "session-alpha",
        "turnId": "turn-old",
        "source": "auto",
        "threshold": 10,
        "language": "en",
    }
    monkeypatch.setattr(
        codex_trajectory_cdp,
        "read_settings",
        lambda: {"enabled": False, "port": 9222},
    )
    monkeypatch.setattr(
        codex_trajectory_cdp,
        "_request_active_task_stop",
        lambda *_args, **_kwargs: pytest.fail("disabled stop reached CDP"),
    )
    assert request_task_stop(request) == {
        "sent": False,
        "error": "Direct stop requires the experimental loopback CDP integration.",
    }
    assert codex_trajectory_cdp._browser_stop(request) == {
        "sent": False,
        "error": "Direct stop requires the experimental loopback CDP integration.",
    }


def test_stop_rebinds_once_from_the_current_app_server_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = {
        "sessionId": "session-alpha",
        "turnId": "turn-old",
        "source": "auto",
        "threshold": 10,
        "language": "en",
    }
    targets = [
        {
            "url": "app://-/index.html",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9333/codex",
        }
    ]
    monkeypatch.setattr(
        codex_trajectory_cdp,
        "_targets",
        lambda _port, **_kwargs: targets,
    )
    evaluated: list[str] = []

    class Connection:
        def __init__(self, _url: str, **_kwargs: object) -> None:
            pass

        def close(self) -> None:
            return None

    def fake_evaluate(_connection: Connection, source: str, **_kwargs: object) -> object:
        evaluated.append(source)
        if 'const EXPECTED_TURN_ID = "turn-old"' in source:
            return {
                "matched": True,
                "sent": False,
                "reason": "turn-stale",
                "activeTurnId": "turn-new",
            }
        return {"matched": True, "sent": True}

    monkeypatch.setattr(codex_trajectory_cdp, "WebSocketConnection", Connection)
    monkeypatch.setattr(codex_trajectory_cdp, "_evaluate", fake_evaluate)

    assert _request_active_task_stop(9333, request) == {"sent": True}
    assert len(evaluated) == 2
    assert 'const EXPECTED_TURN_ID = "turn-old"' in evaluated[0]
    assert 'const EXPECTED_TURN_ID = "turn-new"' in evaluated[1]
    assert 'detail.includes("expected active turn id")' in evaluated[0]
    assert " but found `?([^`]+)`?$" in evaluated[0]


def test_app_private_stop_bootstraps_a_missing_turn_and_reports_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        codex_trajectory_cdp,
        "read_settings",
        lambda: {"enabled": True, "port": 9222},
    )
    monkeypatch.setattr(
        codex_trajectory_cdp,
        "_read_active_task_state",
        lambda _port, _session: {"running": False, "turnId": None},
    )
    assert request_task_stop(
        {
            "sessionId": "session-alpha",
            "source": "manual",
            "threshold": 10,
            "language": "en",
        }
    ) == {"sent": False, "idle": True}


def test_missing_turn_bootstrap_is_history_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = {
        "url": "app://-/index.html",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/codex",
    }
    monkeypatch.setattr(
        codex_trajectory_cdp,
        "_targets",
        lambda _port, **_kwargs: [target],
    )
    evaluated: list[str] = []

    class Connection:
        def __init__(self, _url: str, **_kwargs: object) -> None:
            pass

        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def fake_evaluate(_connection: Connection, source: str, **_kwargs: object) -> object:
        evaluated.append(source)
        encoded_candidate = re.search(r"const CANDIDATE_TURN_ID = (.+);", source)
        assert encoded_candidate is not None
        return {
            "matched": True,
            "running": True,
            "turnId": json.loads(encoded_candidate.group(1)),
        }

    monkeypatch.setattr(codex_trajectory_cdp, "WebSocketConnection", Connection)
    monkeypatch.setattr(codex_trajectory_cdp, "_evaluate", fake_evaluate)

    state = _read_active_task_state(9222, "session-alpha")
    assert state["running"] is True
    assert str(state["turnId"]).startswith("codex-trajectory-probe-")
    assert "includeTurns: false" in evaluated[0]
    assert "read.thread.turns" not in evaluated[0]


@pytest.mark.parametrize(
    "websocket_url",
    [
        "ws://127.0.0.1:9333/forged",
        "ws://[::1]:9222/forged",
    ],
)
def test_websocket_target_must_match_the_discovery_endpoint(
    monkeypatch: pytest.MonkeyPatch,
    websocket_url: str,
) -> None:
    monkeypatch.setattr(
        codex_trajectory_cdp.socket,
        "create_connection",
        lambda *_args, **_kwargs: pytest.fail("cross-port target was contacted"),
    )

    with pytest.raises(
        codex_trajectory_cdp.CdpError,
        match=r"did not match the (IPv4 )?discovery endpoint",
    ):
        codex_trajectory_cdp.WebSocketConnection(
            websocket_url,
            expected_port=9222,
        )


def test_http_discovery_and_websocket_handshake_use_absolute_deadlines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [0.0]
    sockets: list[TrickleSocket] = []

    class TrickleSocket:
        def __init__(self, first_chunk: bytes) -> None:
            self.first_chunk = first_chunk
            self.recv_calls = 0
            self.closed = False

        def settimeout(self, _seconds: float) -> None:
            return None

        def sendall(self, _value: bytes) -> None:
            return None

        def recv(self, _length: int) -> bytes:
            self.recv_calls += 1
            clock[0] += 0.2
            if self.first_chunk:
                value = self.first_chunk
                self.first_chunk = b""
                return value
            return b"x"

        def close(self) -> None:
            self.closed = True

    first_chunks = iter(
        [
            b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n[",
            b"H",
        ]
    )

    def create_connection(*_args: object, **_kwargs: object) -> TrickleSocket:
        sock = TrickleSocket(next(first_chunks))
        sockets.append(sock)
        return sock

    monkeypatch.setattr(codex_trajectory_cdp.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        codex_trajectory_cdp.socket,
        "create_connection",
        create_connection,
    )

    with pytest.raises(codex_trajectory_cdp.CdpError, match="discovery timed out"):
        codex_trajectory_cdp._http_json(9222, "/json/list")
    assert sockets[0].recv_calls < 10
    assert sockets[0].closed is True

    clock[0] = 0.0
    with pytest.raises(codex_trajectory_cdp.CdpError, match="handshake timed out"):
        codex_trajectory_cdp.WebSocketConnection("ws://127.0.0.1:9222/codex")
    assert sockets[1].recv_calls < 10
    assert sockets[1].closed is True


def test_state_and_stop_share_one_deadline_across_all_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    targets = [
        {
            "url": "app://-/index.html",
            "webSocketDebuggerUrl": f"ws://127.0.0.1:9222/target-{index}",
        }
        for index in range(3)
    ]
    discovery_deadlines: list[float] = []
    connections: list[tuple[str, float]] = []

    def fake_targets(_port: int, *, deadline: float) -> list[dict[str, str]]:
        discovery_deadlines.append(deadline)
        return targets

    class Connection:
        def __init__(self, url: str, *, deadline: float, **_kwargs: object) -> None:
            self.deadline = deadline
            connections.append((url, deadline))

        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def close(self) -> None:
            return None

    def exhaust_deadline(connection: Connection, _source: str, **_kwargs: object) -> object:
        clock[0] = connection.deadline
        raise codex_trajectory_cdp.CdpError("deadline exhausted")

    monkeypatch.setattr(codex_trajectory_cdp.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(codex_trajectory_cdp, "_targets", fake_targets)
    monkeypatch.setattr(codex_trajectory_cdp, "WebSocketConnection", Connection)
    monkeypatch.setattr(codex_trajectory_cdp, "_evaluate", exhaust_deadline)

    with pytest.raises(codex_trajectory_cdp.CdpError):
        _read_active_task_state(9222, "session-alpha", "turn-active")
    assert discovery_deadlines == [
        100.0 + codex_trajectory_cdp.TASK_STATE_OPERATION_TIMEOUT_SECONDS
    ]
    assert len(connections) == 1

    clock[0] = 200.0
    assert _request_active_task_stop(
        9222,
        {
            "sessionId": "session-alpha",
            "turnId": "turn-active",
            "source": "manual",
            "threshold": 10,
            "language": "en",
        },
    ) == {"sent": False, "error": "Could not reach the bound Codex task."}
    assert discovery_deadlines[-1] == (200.0 + codex_trajectory_cdp.STOP_OPERATION_TIMEOUT_SECONDS)
    assert len(connections) == 2


def test_stop_transport_budget_covers_every_inner_app_server_window() -> None:
    inner_seconds = codex_trajectory_cdp.STOP_APP_SERVER_RPC_TIMEOUT_MS / 1000
    assert 4 * inner_seconds < codex_trajectory_cdp.STOP_APP_SERVER_COMMAND_TIMEOUT_SECONDS
    assert 5 * inner_seconds < codex_trajectory_cdp.STOP_OPERATION_TIMEOUT_SECONDS


def test_process_lock_rejects_links_without_modifying_targets(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("keep", encoding="utf-8")

    hardlink = tmp_path / "hardlink"
    os.link(target, hardlink)
    assert _acquire_process_lock(hardlink) is None
    assert target.read_text(encoding="utf-8") == "keep"

    if os.name != "nt":
        symlink = tmp_path / "symlink"
        symlink.symlink_to(target)
        assert _acquire_process_lock(symlink) is None
        assert target.read_text(encoding="utf-8") == "keep"


@pytest.mark.ui
@pytest.mark.skipif(os.environ.get("RUN_UI_TESTS") != "1", reason="UI tests are opt-in")
def test_authenticated_cdp_transport_replaces_a_legacy_browser_link(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = unused_loopback_port()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            args=[
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={port}",
            ]
        )
        page = browser.new_page()
        html = (
            '<div data-app-action-sidebar-thread-active="true" '
            'data-app-action-sidebar-thread-id="local:client-new-thread:temporary"></div>'
            '<div data-above-composer-conversation-id="session-alpha"></div>'
            '<div id="composer"><div><button type="button" aria-label="Change permissions">'
            "<span>Full access</span></button></div><div><div>"
            '<textarea></textarea></div></div><div><button type="button" '
            'aria-label="Send message" onclick="document.body.dataset.sent=\'true\'">'
            "Send</button></div></div>"
            '<a id="codex-trajectory-toolbar-entry" '
            'href="http://127.0.0.1:43123/private-token/">Legacy shortcut</a>'
            '<style id="codex-trajectory-toolbar-style"></style>'
            '<div id="codex-trajectory-cdp-drawer"></div>'
        )
        page.set_content(html)
        targets = _targets(port)
        monkeypatch.setattr(
            codex_trajectory_cdp,
            "_targets",
            lambda _port, **_kwargs: [
                {**target, "url": "app://-/index.html"}
                for target in targets
                if target.get("type") == "page"
            ],
        )
        monkeypatch.setattr(
            codex_trajectory_cdp,
            "authenticate_connected_peer",
            lambda _socket, _identity: 4321,
        )

        viewer_url = "http://127.0.0.1:43123/private-token/"
        connected, injected = _inject_cycle(port, True, viewer_url, HOST_IDENTITY)
        assert (connected, injected) == (True, True)
        assert page.locator("#codex-trajectory-toolbar-entry").count() == 1
        assert page.locator("#codex-trajectory-toolbar-style").count() == 1
        assert page.locator("#codex-trajectory-cdp-drawer").count() == 0
        assert "private-token" in page.locator("#codex-trajectory-toolbar-entry").get_attribute(
            "href"
        )
        assert page.locator("textarea").input_value() == ""
        assert page.locator("body").get_attribute("data-sent") is None

        connected, injected = _inject_cycle(port, False)
        assert (connected, injected) == (True, False)
        assert page.locator("#codex-trajectory-toolbar-entry").count() == 0
        browser.close()


def test_disabled_cleanup_cycle_does_not_require_a_browser_view_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        codex_trajectory_cdp,
        "_targets",
        lambda _port, **_kwargs: [],
    )

    assert _inject_cycle(9222, False) == (False, False)


def test_watch_removes_injection_from_previous_port_before_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[int, bool, str | None, HostIdentity | None]] = []

    class Resource:
        closed = False

        def close(self) -> None:
            self.closed = True

    lock = Resource()
    statuses: list[dict[str, object]] = []
    settings = iter(
        [
            {"enabled": True, "port": 9222},
            {"enabled": True, "port": 9333},
            {"enabled": False, "port": 9333},
        ]
    )

    def fake_inject(
        port: int,
        enabled: bool,
        viewer_url: str | None = None,
        host_identity: HostIdentity | None = None,
    ) -> tuple[bool, bool]:
        calls.append((port, enabled, viewer_url, host_identity))
        return True, False

    monkeypatch.setattr(codex_trajectory_cdp, "_acquire_process_lock", lambda _path: lock)
    monkeypatch.setattr(codex_trajectory_cdp, "lock_path", lambda: tmp_path / "lock")
    monkeypatch.setattr(codex_trajectory_cdp, "read_settings", lambda: next(settings))
    monkeypatch.setattr(codex_trajectory_cdp, "_inject_cycle", fake_inject)
    monkeypatch.setattr(
        codex_trajectory_cdp,
        "write_daemon_status",
        lambda value: statuses.append(value),
    )
    monkeypatch.setattr(codex_trajectory_cdp.time, "sleep", lambda _seconds: None)

    assert codex_trajectory_cdp.watch() == 0
    assert calls == [
        (9222, True, None, None),
        (9222, False, None, None),
        (9333, True, None, None),
        (9333, False, None, None),
    ]
    assert all(status["viewerServing"] is False for status in statuses)
    assert all(status["injected"] is False for status in statuses)
    assert lock.closed is True


@pytest.mark.ui
@pytest.mark.skipif(os.environ.get("RUN_UI_TESTS") != "1", reason="UI tests are opt-in")
def test_cdp_reads_the_effective_codex_palette(monkeypatch: pytest.MonkeyPatch) -> None:
    port = unused_loopback_port()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            args=[
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={port}",
            ]
        )
        page = browser.new_page()
        page.set_content("<main>Codex fixture</main>")
        page.evaluate(
            """() => {
              document.documentElement.className = "electron-dark";
              const values = {
                "--color-token-bg-primary": "#141414",
                "--color-token-main-surface-primary": "#181818",
                "--color-background-editor-opaque": "rgb(40, 40, 40)",
                "--color-border": "rgba(255, 255, 255, 0.084)",
                "--color-border-heavy": "rgba(255, 255, 255, 0.156)",
                "--color-token-text-primary": "#dfdfdf",
                "--color-text-foreground-tertiary": "rgba(255, 255, 255, 0.498)",
                "--color-text-accent": "rgb(131, 195, 255)",
                "--color-background-accent": "#0d273f",
                "--color-accent-red": "#ff6764",
                "--color-accent-green": "#40c977",
                "--color-accent-blue": "#339cff",
                "--color-accent-purple": "#ad7bf9",
                "--color-icon-warning": "#ff8549",
              };
              for (const [name, value] of Object.entries(values)) {
                document.documentElement.style.setProperty(name, value);
              }
            }"""
        )
        targets = _targets(port)
        monkeypatch.setattr(
            codex_trajectory_cdp,
            "_targets",
            lambda _port, **_kwargs: [
                {**target, "url": "app://-/index.html"}
                for target in targets
                if target.get("type") == "page"
            ],
        )

        theme = _read_codex_theme(port)
        assert theme["scheme"] == "dark"
        assert theme["colors"]["bg"] == "#141414"
        assert theme["colors"]["panel"] == "#181818"
        assert theme["colors"]["accent"] == "rgb(131, 195, 255)"
        assert theme["colors"]["tool"] == "#ff8549"
        browser.close()


@pytest.mark.ui
@pytest.mark.skipif(os.environ.get("RUN_UI_TESTS") != "1", reason="UI tests are opt-in")
def test_cdp_stop_interrupts_only_the_bound_active_turn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    port = unused_loopback_port()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            args=[
                "--remote-debugging-address=127.0.0.1",
                f"--remote-debugging-port={port}",
            ]
        )
        page = browser.new_page()
        page.set_content("<main>Codex fixture without a selected sidebar row</main>")
        page.evaluate(
            """() => {
              window.__appServerRequests = [];
              window.__taskRunning = true;
              window.__activeTurnId = "turn-active";
              window.__interruptFailure = null;
              window.__goalStatus = "active";
              window.__goalSetFailure = false;
              window.electronBridge = {
                async sendMessageFromView(message) {
                  window.__appServerRequests.push(structuredClone(message));
                  let result = message.request.method === "thread/read" ? {
                        thread: {
                          id: "session-alpha",
                          status: window.__taskRunning
                            ? {type: "active", activeFlags: []}
                            : {type: "idle"},
                          turns: [{
                            id: window.__activeTurnId,
                            status: window.__taskRunning ? "inProgress" : "completed",
                          }],
                        },
                      } : {};
                  let error = null;
                  if (message.request.method === "thread/goal/get") {
                    if (message.request.params.threadId !== "session-alpha") {
                      error = {code: -32000, message: "Thread not found"};
                    } else {
                      result = {
                        goal: window.__goalStatus ? {
                          threadId: "session-alpha",
                          status: window.__goalStatus,
                        } : null,
                      };
                    }
                  }
                  if (message.request.method === "thread/goal/set") {
                    if (window.__goalSetFailure) {
                      error = {code: -32000, message: "Goal persistence unavailable"};
                    } else {
                      window.__goalStatus = message.request.params.status;
                      result = {
                        goal: {
                          threadId: "session-alpha",
                          status: window.__goalStatus,
                        },
                      };
                    }
                  }
                  const staleTurn = message.request.method === "turn/interrupt"
                    && message.request.params.turnId !== window.__activeTurnId;
                  if (message.request.method === "turn/interrupt" && (
                    (window.__interruptFailure && window.__interruptFailure !== "stale")
                    || staleTurn
                    || !window.__taskRunning
                    || message.request.params.threadId !== "session-alpha"
                  )) {
                    if (window.__interruptFailure === "race") window.__taskRunning = false;
                    error = {
                      code: -32000,
                      message: message.request.params.threadId !== "session-alpha"
                        ? "Thread not found"
                          : staleTurn
                            ? `expected active turn id \\`${message.request.params.turnId}\\``
                              + ` but found \\`${window.__activeTurnId}\\``
                          : window.__interruptFailure === "race"
                            || !window.__taskRunning
                          ? "Expected turn mismatch: turn already completed"
                          : "Interrupt transport unavailable",
                    };
                  }
                  setTimeout(() => window.dispatchEvent(new MessageEvent("message", {
                    data: {
                      type: "mcp-response",
                      hostId: message.hostId,
                      message: error
                        ? {id: message.request.id, error}
                        : {id: message.request.id, result},
                    },
                  })), message.request.method === "thread/read"
                    && message.request.params.includeTurns ? 900 : 0);
                },
              };
            }"""
        )
        targets = _targets(port)
        monkeypatch.setattr(
            codex_trajectory_cdp,
            "_targets",
            lambda _port, **_kwargs: [
                {**target, "url": "app://-/index.html"}
                for target in targets
                if target.get("type") == "page"
            ],
        )

        request = {
            "sessionId": "session-alpha",
            "turnId": "turn-active",
            "source": "manual",
            "threshold": 10,
            "language": "en",
        }
        bootstrap = _read_active_task_state(port, "session-alpha")
        assert bootstrap["running"] is True
        assert str(bootstrap["turnId"]).startswith("codex-trajectory-probe-")
        assert page.evaluate("window.__appServerRequests.at(-1).request.params") == {
            "threadId": "session-alpha",
            "includeTurns": False,
        }
        assert _request_active_task_stop(port, request) == {"sent": True}
        requests = page.evaluate("window.__appServerRequests")
        assert [item["request"]["method"] for item in requests] == [
            "thread/read",
            "thread/goal/get",
            "thread/goal/set",
            "turn/interrupt",
        ]
        assert requests[2]["request"]["params"] == {
            "threadId": "session-alpha",
            "status": "paused",
        }
        interrupt = requests[3]["request"]["params"]
        assert interrupt == {"threadId": "session-alpha", "turnId": "turn-active"}
        assert page.evaluate("window.__goalStatus") == "paused"

        assert _read_active_task_state(port, "session-alpha", "turn-active") == {
            "running": True,
            "turnId": "turn-active",
        }
        assert page.evaluate("window.__appServerRequests.at(-1).request.params") == {
            "threadId": "session-alpha",
            "includeTurns": False,
        }

        page.evaluate("window.__taskRunning = false")
        assert _read_active_task_state(port, "session-alpha") == {
            "running": False,
            "turnId": None,
        }
        assert _request_active_task_stop(port, request) == {"sent": False, "idle": True}
        requests = page.evaluate("window.__appServerRequests")
        assert [item["request"]["method"] for item in requests[-3:]] == [
            "thread/goal/get",
            "turn/interrupt",
            "thread/read",
        ]

        page.evaluate("() => { window.__taskRunning = true; window.__interruptFailure = 'race'; }")
        assert _request_active_task_stop(port, request) == {"sent": False, "idle": True}
        requests = page.evaluate("window.__appServerRequests")
        assert [item["request"]["method"] for item in requests[-3:]] == [
            "thread/goal/get",
            "turn/interrupt",
            "thread/read",
        ]

        page.evaluate(
            """() => {
              window.__taskRunning = true;
              window.__activeTurnId = "turn-new";
              window.__interruptFailure = "stale";
            }"""
        )
        assert _request_active_task_stop(port, request) == {"sent": True}
        requests = page.evaluate("window.__appServerRequests")
        assert [item["request"]["method"] for item in requests[-4:]] == [
            "thread/goal/get",
            "turn/interrupt",
            "thread/goal/get",
            "turn/interrupt",
        ]
        assert requests[-1]["request"]["params"]["turnId"] == "turn-new"

        page.evaluate(
            """() => {
              window.__taskRunning = true;
              window.__activeTurnId = "turn-active";
              window.__interruptFailure = "persistent";
            }"""
        )
        assert _request_active_task_stop(port, request) == {
            "sent": False,
            "error": "The Codex App Server could not interrupt the active turn.",
        }
        requests = page.evaluate("window.__appServerRequests")
        assert [item["request"]["method"] for item in requests[-3:]] == [
            "thread/goal/get",
            "turn/interrupt",
            "thread/read",
        ]

        page.evaluate(
            """() => {
              window.__taskRunning = true;
              window.__interruptFailure = null;
              window.__goalStatus = "active";
              window.__goalSetFailure = true;
            }"""
        )
        assert _request_active_task_stop(port, request) == {
            "sent": False,
            "error": "The Codex App Server could not pause the active Goal.",
        }
        requests = page.evaluate("window.__appServerRequests")
        assert [item["request"]["method"] for item in requests[-2:]] == [
            "thread/goal/get",
            "thread/goal/set",
        ]
        assert page.evaluate("window.__taskRunning") is True

        mismatched = {**request, "sessionId": "session-other"}
        assert _request_active_task_stop(port, mismatched) == {
            "sent": False,
            "error": "The bound Codex task could not be verified.",
        }
        assert page.evaluate("window.__appServerRequests.at(-1).request.method") == (
            "thread/goal/get"
        )
        browser.close()

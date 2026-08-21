"""Live loopback-CDP coverage for the in-app Browser shortcut transport."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import codex_trajectory_cdp
import pytest
from codex_trajectory_cdp import (
    REMOVE_SOURCE,
    _acquire_process_lock,
    _inject_cycle,
    _read_active_task_state,
    _read_codex_theme,
    _request_active_task_stop,
    _targets,
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
        lambda _port, _route: [
            {"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/page"},
            {"type": "iframe", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/iframe"},
            {"type": "webview", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/webview"},
            {"type": "worker", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/worker"},
        ],
    )

    assert [target["type"] for target in _targets(9222)] == ["page", "iframe", "webview"]


def test_injection_exposes_viewer_token_only_to_codex_shell(
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
    monkeypatch.setattr(codex_trajectory_cdp, "_targets", lambda _port: targets)
    evaluated: dict[str, str] = {}

    class Connection:
        def __init__(self, url: str) -> None:
            self.url = url

        def close(self) -> None:
            return None

    def fake_evaluate(connection: Connection, source: str, **_kwargs: object) -> object:
        evaluated[connection.url] = source
        return {"installed": True, "visible": True}

    monkeypatch.setattr(codex_trajectory_cdp, "WebSocketConnection", Connection)
    monkeypatch.setattr(codex_trajectory_cdp, "_evaluate", fake_evaluate)

    assert _inject_cycle(9222, True, "http://127.0.0.1:43123/private-token/") == (
        True,
        True,
    )
    assert evaluated["ws://127.0.0.1:9222/external"] == REMOVE_SOURCE
    assert "private-token" not in evaluated["ws://127.0.0.1:9222/external"]
    assert "private-token" in evaluated["ws://127.0.0.1:9222/codex"]


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
        def __init__(self, url: str) -> None:
            self.url = url

        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def fake_evaluate(connection: Connection, source: str, **_kwargs: object) -> object:
        if connection.url.endswith("/unavailable"):
            return {"matched": True, "reason": "bridge-unavailable"}
        if "turn/interrupt" in source:
            return {"matched": True, "sent": True}
        return {"matched": True, "running": True, "turnId": "turn-healthy"}

    monkeypatch.setattr(codex_trajectory_cdp, "_targets", lambda _port: targets)
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
def test_dependency_free_cdp_transport_injects_in_app_browser_link(
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
        )
        page.set_content(html)
        targets = _targets(port)
        monkeypatch.setattr(
            codex_trajectory_cdp,
            "_targets",
            lambda _port: [
                {**target, "url": "app://-/index.html"}
                for target in targets
                if target.get("type") == "page"
            ],
        )

        viewer_url = "http://127.0.0.1:43123/private-token/"
        connected, injected = _inject_cycle(port, True, viewer_url)
        assert (connected, injected) == (True, True)
        assert page.locator("#codex-trajectory-toolbar-entry").inner_text() == "查看轨迹"

        link = page.locator("#codex-trajectory-toolbar-entry")
        link.evaluate(
            "element => element.addEventListener('click', event => event.preventDefault(), "
            "{once: true})"
        )
        link.click()
        assert link.get_attribute("href") == (
            "http://127.0.0.1:43123/private-token/?sessionId=session-alpha&lang=en-US"
        )
        assert page.locator("textarea").input_value() == ""
        assert page.locator("body").get_attribute("data-sent") is None
        assert page.locator("#codex-trajectory-cdp-drawer").count() == 0

        connected, injected = _inject_cycle(port, False)
        assert (connected, injected) == (True, False)
        assert page.locator("#codex-trajectory-toolbar-entry").count() == 0
        browser.close()


def test_enabled_injection_requires_browser_view_url() -> None:
    with pytest.raises(ValueError, match="viewer_url"):
        _inject_cycle(9222, True)


def test_watch_removes_injection_from_previous_port_before_switch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[tuple[int, bool, str | None]] = []

    class Resource:
        closed = False

        def close(self) -> None:
            self.closed = True

    class Server(Resource):
        url = "http://127.0.0.1:43123/private-token/"

        def __init__(self, *_args: object) -> None:
            self.started = False

        def start(self) -> None:
            self.started = True

    lock = Resource()
    settings = iter(
        [
            {"enabled": True, "port": 9222},
            {"enabled": True, "port": 9333},
            {"enabled": False, "port": 9333},
        ]
    )

    def fake_inject(port: int, enabled: bool, viewer_url: str | None = None) -> tuple[bool, bool]:
        calls.append((port, enabled, viewer_url))
        return True, enabled

    monkeypatch.setattr(codex_trajectory_cdp, "_acquire_process_lock", lambda _path: lock)
    monkeypatch.setattr(codex_trajectory_cdp, "lock_path", lambda: tmp_path / "lock")
    monkeypatch.setattr(codex_trajectory_cdp, "read_settings", lambda: next(settings))
    monkeypatch.setattr(codex_trajectory_cdp, "BrowserViewServer", Server)
    monkeypatch.setattr(codex_trajectory_cdp, "_inject_cycle", fake_inject)
    monkeypatch.setattr(codex_trajectory_cdp, "write_daemon_status", lambda _value: None)
    monkeypatch.setattr(codex_trajectory_cdp.time, "sleep", lambda _seconds: None)

    assert codex_trajectory_cdp.watch() == 0
    assert calls == [
        (9222, True, Server.url),
        (9222, False, None),
        (9333, True, Server.url),
        (9333, False, Server.url),
    ]
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
            lambda _port: [
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
                            id: "turn-active",
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
                  if (message.request.method === "turn/interrupt" && (
                    window.__interruptFailure
                    || !window.__taskRunning
                    || message.request.params.threadId !== "session-alpha"
                  )) {
                    if (window.__interruptFailure === "race") window.__taskRunning = false;
                    error = {
                      code: -32000,
                      message: message.request.params.threadId !== "session-alpha"
                        ? "Thread not found"
                          : ["race", "stale"].includes(window.__interruptFailure)
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
            lambda _port: [
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
        assert _read_active_task_state(port, "session-alpha") == {
            "running": True,
            "turnId": "turn-active",
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

        page.evaluate("() => { window.__taskRunning = true; window.__interruptFailure = 'stale'; }")
        assert _request_active_task_stop(port, request) == {
            "sent": False,
            "stale": True,
            "error": "The task advanced to a newer turn; refresh and retry stopping it.",
        }
        requests = page.evaluate("window.__appServerRequests")
        assert [item["request"]["method"] for item in requests[-3:]] == [
            "thread/goal/get",
            "turn/interrupt",
            "thread/read",
        ]

        page.evaluate(
            "() => { window.__taskRunning = true; window.__interruptFailure = 'persistent'; }"
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

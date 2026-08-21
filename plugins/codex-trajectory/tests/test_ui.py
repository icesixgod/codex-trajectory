"""Browser acceptance coverage for the trajectory app resource."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from copy import deepcopy
from threading import Event
from typing import Any

import pytest
from codex_trajectory.browser_view import THEME_COLOR_KEYS, BrowserViewServer, injection_source
from playwright.sync_api import FrameLocator, Page, expect, sync_playwright
from ui_harness import demo_trajectories, start_server

pytestmark = [
    pytest.mark.ui,
    pytest.mark.skipif(os.environ.get("RUN_UI_TESTS") != "1", reason="UI tests are opt-in"),
]


@pytest.fixture(scope="module")
def harness_url() -> Iterator[str]:
    """Serve the UI harness for a test module."""
    server, thread = start_server()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


@pytest.fixture(scope="module")
def page() -> Iterator[Page]:
    """Launch one isolated Chromium page."""
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        context.add_init_script(
            """
            (() => {
              let activePipElement = null;
              const define = (target, name, descriptor) => {
                try { Object.defineProperty(target, name, descriptor); } catch {}
              };
              define(Document.prototype, "pictureInPictureEnabled", {
                configurable: true,
                get: () => true,
              });
              define(Document.prototype, "pictureInPictureElement", {
                configurable: true,
                get: () => activePipElement,
              });
              define(HTMLMediaElement.prototype, "readyState", {
                configurable: true,
                get: () => HTMLMediaElement.HAVE_ENOUGH_DATA,
              });
              define(HTMLMediaElement.prototype, "play", {
                configurable: true,
                value() {
                  this.dispatchEvent(new Event("canplay"));
                  return Promise.resolve();
                },
              });
              define(HTMLVideoElement.prototype, "requestPictureInPicture", {
                configurable: true,
                value() {
                  if (new URLSearchParams(location.search).get("nativePipUnavailable") === "1") {
                    return Promise.reject(new DOMException(
                      "Picture-in-Picture is not available.",
                      "NotSupportedError",
                    ));
                  }
                  activePipElement = this;
                  this.dataset.requestCount = String(Number(this.dataset.requestCount || 0) + 1);
                  this.dispatchEvent(new Event("enterpictureinpicture"));
                  return Promise.resolve({ addEventListener() {} });
                },
              });
              define(Document.prototype, "exitPictureInPicture", {
                configurable: true,
                value() {
                  const previous = activePipElement;
                  activePipElement = null;
                  previous?.dispatchEvent(new Event("leavepictureinpicture"));
                  return Promise.resolve();
                },
              });
            })();
            """
        )
        yield context.new_page()
        context.close()
        browser.close()


def viewer(page: Page) -> FrameLocator:
    """Return the app-resource iframe locator."""
    return page.frame_locator("#viewer")


def test_cdp_injection_places_safe_entry_after_full_access(page: Page, harness_url: str) -> None:
    page.goto(f"{harness_url}/toolbar-fixture")
    viewer_url = "http://127.0.0.1:43123/private-token/"
    result = page.evaluate(injection_source(viewer_url))
    assert result["installed"] is True
    assert result["visible"] is True
    link = page.get_by_role("link", name="查看轨迹")
    expect(link).to_be_visible()
    assert link.evaluate("element => element.previousElementSibling?.id") == "access"

    link.evaluate(
        "element => element.addEventListener('click', event => event.preventDefault(), "
        "{once: true})"
    )
    link.click()
    assert page.evaluate("window.__submitted") == []
    assert page.locator("textarea").input_value() == ""
    expect(link).to_have_attribute(
        "href",
        "http://127.0.0.1:43123/private-token/?sessionId=session-alpha&lang=en-US",
    )
    expect(page.locator("#codex-trajectory-cdp-drawer")).to_have_count(0)

    page.locator("textarea").fill("draft")
    link.evaluate(
        "element => element.addEventListener('click', event => event.preventDefault(), "
        "{once: true})"
    )
    link.click()
    assert page.locator("textarea").input_value() == "draft"

    page.locator('[data-app-action-sidebar-thread-id="local:session-alpha"]').evaluate(
        "element => element.setAttribute("
        "'data-app-action-sidebar-thread-id', "
        "'local:client-new-thread:temporary')"
    )
    page.locator("body").evaluate(
        "body => { const marker = document.createElement('div'); "
        "marker.dataset.aboveComposerConversationId = "
        "'01a01e91-c881-7641-bc8b-acb1173ba846'; body.append(marker); }"
    )
    page.evaluate("window.__codexTrajectoryToolbarV1.ensure()")
    expect(link).to_have_attribute(
        "href",
        "http://127.0.0.1:43123/private-token/"
        "?sessionId=01a01e91-c881-7641-bc8b-acb1173ba846&lang=en-US",
    )
    expect(link).to_have_attribute("aria-disabled", "false")

    page.locator("[data-above-composer-conversation-id]").evaluate("element => element.remove()")
    page.evaluate("window.__codexTrajectoryToolbarV1.ensure()")
    expect(link).to_have_attribute("aria-disabled", "true")
    expect(link).to_have_attribute(
        "href",
        "http://127.0.0.1:43123/private-token/?lang=en-US",
    )

    page.evaluate("window.__codexTrajectoryToolbarV1.dispose()")
    expect(link).to_have_count(0)


def test_loopback_browser_view_renders_the_full_trajectory_ui(page: Page) -> None:
    payload = deepcopy(demo_trajectories()["session-alpha"])
    payload["turns"][-1]["status"] = "complete"
    requested: list[tuple[str, dict[str, object]]] = []
    stop_requests: list[dict[str, object]] = []
    task_state: dict[str, object] = {"running": True, "turnId": "turn-active"}
    theme_state: dict[str, Any] = {
        "scheme": "dark",
        "colors": {key: "#181818" for key in THEME_COLOR_KEYS},
    }
    theme_state["colors"].update(
        {
            "bg": "#141414",
            "text": "#dfdfdf",
            "accent": "rgb(131, 195, 255)",
        }
    )

    def provider(name: str, arguments: dict[str, object]) -> dict[str, object]:
        requested.append((name, arguments))
        if name == "get_codex_toolbar_injection_status":
            return {
                "structuredContent": {
                    "schemaVersion": 1,
                    "enabled": True,
                    "port": 9222,
                    "cdpAvailable": True,
                    "daemonRunning": True,
                    "connected": True,
                    "injected": True,
                    "viewerServing": True,
                    "lastError": None,
                }
            }
        if name == "get_codex_trajectory_update":
            return {
                "structuredContent": {
                    "schemaVersion": 1,
                    "unchanged": True,
                    "revision": "1" * 64,
                }
            }
        value = deepcopy(payload)
        value["detailLevel"] = arguments.get("detailLevel", "summary")
        return {"structuredContent": value}

    def stop_provider(value: dict[str, object]) -> dict[str, object]:
        stop_requests.append(value)
        return {"sent": True}

    server = BrowserViewServer(
        provider,
        stop_provider,
        lambda: deepcopy(theme_state),
        lambda _session_id, _turn_id: deepcopy(task_state),
    )
    server.start()
    try:
        page.goto(f"{server.url}?sessionId=session-alpha&lang=en")
        frame = viewer(page)
        expect(frame.get_by_role("heading", name="Inspect the latest task")).to_be_visible()
        expect(frame.locator("html")).to_have_attribute("data-codex-theme", "dark")
        assert (
            frame.locator("html").evaluate(
                "element => getComputedStyle(element).getPropertyValue('--bg').trim()"
            )
            == "#141414"
        )
        assert (
            frame.locator("html").evaluate(
                "element => getComputedStyle(element).getPropertyValue('--accent').trim()"
            )
            == "rgb(131, 195, 255)"
        )
        expect(frame.get_by_text("Safe summary", exact=True)).to_be_visible()
        expect(frame.get_by_text("Token details", exact=True)).to_be_visible()
        expect(frame.get_by_role("cell", name="Failure isolated and explained")).to_be_visible()
        expect(frame.locator(".stat").nth(1).locator(".stat-value")).to_have_text("9")
        expect(frame.locator("#cdpToolbarStatus")).to_have_text(
            "Ready; opens in the Codex in-app Browser without a message"
        )
        mismatch = frame.locator("body").evaluate(
            """() => new Promise(resolve => {
              const id = 8675309;
              const onMessage = event => {
                if (event.data?.id !== id) return;
                window.removeEventListener('message', onMessage);
                resolve(event.data.result);
              };
              window.addEventListener('message', onMessage);
              window.parent.postMessage({
                jsonrpc: '2.0',
                id,
                method: 'trajectory/request-stop',
                params: {sessionId: 'session-beta', source: 'manual', threshold: 10},
              }, '*');
            })"""
        )
        assert mismatch == {
            "sent": False,
            "error": (
                "The displayed trajectory does not match the Codex task that opened this page."
            ),
        }
        assert stop_requests == []
        frame.get_by_role("button", name="Refresh").click()
        expect(frame.get_by_text("Safe summary", exact=True)).to_be_visible()
        names = [name for name, _arguments in requested]
        assert names.count("get_codex_trajectory") >= 2
        assert "get_codex_toolbar_injection_status" in names

        frame.locator("body").evaluate(
            """() => {
              Object.defineProperty(HTMLVideoElement.prototype, "requestPictureInPicture", {
                configurable: true,
                value: () => Promise.reject(new DOMException(
                  "Picture-in-Picture is not available.",
                  "NotSupportedError",
                )),
              });
            }"""
        )
        frame.get_by_role("button", name="Live window").click()
        dock = frame.locator("#liveDock")
        expect(dock).to_have_attribute("data-presentation", "inline-live")
        stop_button = frame.locator("#requestStop")
        expect(dock).to_have_attribute("data-task-state-source", "app-server")
        expect(dock).to_have_attribute("data-projected-task-running", "false")
        expect(dock).to_have_attribute("data-task-running", "true")
        expect(stop_button).to_be_enabled()

        task_state.update({"running": False, "turnId": None})
        expect(dock).to_have_attribute("data-task-running", "false", timeout=3_000)
        expect(stop_button).to_have_text("Idle")
        expect(stop_button).to_be_disabled()

        task_state.update({"running": True, "turnId": "turn-next"})
        expect(dock).to_have_attribute("data-task-running", "true", timeout=3_000)
        expect(stop_button).to_have_text("Stop")
        expect(stop_button).to_be_enabled()
        stop_button.click()
        expect(stop_button).to_have_text("Requested")
        assert stop_requests == [
            {
                "sessionId": "session-alpha",
                "turnId": "turn-next",
                "source": "manual",
                "threshold": 10,
                "language": "en",
            }
        ]

        theme_state["scheme"] = "light"
        theme_state["colors"] = {key: "#ffffff" for key in THEME_COLOR_KEYS}
        theme_state["colors"].update(
            {"bg": "#f7f7f7", "text": "#202020", "accent": "rgb(0, 102, 204)"}
        )
        expect(frame.locator("html")).to_have_attribute("data-codex-theme", "light", timeout=3_000)
        assert (
            frame.locator("html").evaluate(
                "element => getComputedStyle(element).getPropertyValue('--bg').trim()"
            )
            == "#f7f7f7"
        )
    finally:
        server.close()


def test_loopback_auto_stop_interrupts_at_exact_limit_without_task_state_poll(
    page: Page,
) -> None:
    payload = deepcopy(demo_trajectories()["session-alpha"])
    payload["turns"][-1]["status"] = "running"
    payload["turns"][-1]["completedAt"] = None
    payload["stats"]["rateLimits"] = {
        "primary": {
            "usedPercent": 2.0,
            "windowMinutes": 10_080,
            "resetsAt": "2026-08-27T05:02:11Z",
        }
    }
    stop_requests: list[dict[str, object]] = []

    def provider(name: str, arguments: dict[str, object]) -> dict[str, object]:
        if name == "get_codex_trajectory_update":
            return {
                "structuredContent": {
                    "schemaVersion": 1,
                    "unchanged": True,
                    "revision": "1" * 64,
                }
            }
        value = deepcopy(payload)
        value["detailLevel"] = arguments.get("detailLevel", "summary")
        return {"structuredContent": value}

    def stop_provider(value: dict[str, object]) -> dict[str, object]:
        stop_requests.append(value)
        return {"sent": True}

    server = BrowserViewServer(provider, stop_provider)
    server.start()
    try:
        page.goto(f"{server.url}?sessionId=session-alpha&lang=en")
        frame = viewer(page)
        expect(frame.get_by_role("heading", name="Inspect the latest task")).to_be_visible()
        frame.locator("body").evaluate(
            """() => {
              Object.defineProperty(HTMLVideoElement.prototype, "requestPictureInPicture", {
                configurable: true,
                value: () => Promise.reject(new DOMException(
                  "Picture-in-Picture is not available.",
                  "NotSupportedError",
                )),
              });
            }"""
        )
        frame.get_by_role("button", name="Live window").click()

        dock = frame.locator("#liveDock")
        expect(dock).to_have_attribute("data-task-state-source", "trajectory")
        expect(dock).to_have_attribute(
            "data-quota",
            "primary:98:2026-08-27T05:02:11Z",
        )
        threshold = frame.locator("#autoStopThreshold")
        threshold.fill("98")
        threshold.press("Tab")
        frame.locator("#autoStopEnabled").check()

        expect(frame.locator("#requestStop")).to_have_text("Requested")
        expect(frame.locator("#stopRequestStatus")).to_have_text("Stop requested")
        assert stop_requests == [
            {
                "sessionId": "session-alpha",
                "turnId": "turn-2",
                "source": "auto",
                "threshold": 98,
                "language": "en",
            }
        ]
    finally:
        server.close()


def test_loopback_stale_turn_is_rebound_before_stop_retry(page: Page) -> None:
    payload = deepcopy(demo_trajectories()["session-alpha"])
    payload["turns"][-1].update(
        {"id": "turn-old", "status": "running", "completedAt": None, "durationMs": None}
    )
    stop_requests: list[dict[str, object]] = []
    state_candidates: list[str | None] = []
    advanced = False
    candidate_started = Event()
    release_candidate = Event()
    stale_returned = Event()

    def provider(name: str, arguments: dict[str, object]) -> dict[str, object]:
        if name == "get_codex_trajectory_update":
            return {
                "structuredContent": {
                    "schemaVersion": 1,
                    "unchanged": True,
                    "revision": "1" * 64,
                }
            }
        value = deepcopy(payload)
        value["detailLevel"] = arguments.get("detailLevel", "summary")
        return {"structuredContent": value}

    def stop_provider(value: dict[str, object]) -> dict[str, object]:
        nonlocal advanced
        stop_requests.append(value)
        if not advanced:
            advanced = True
            stale_returned.set()
            return {
                "sent": False,
                "stale": True,
                "error": "The task advanced to a newer turn; refresh and retry stopping it.",
            }
        return {"sent": True}

    def task_state_provider(_session_id: str, candidate: str | None) -> dict[str, object]:
        state_candidates.append(candidate)
        if candidate is not None:
            if not advanced:
                candidate_started.set()
                release_candidate.wait(timeout=3)
            return {"running": True, "turnId": candidate}
        return {"running": True, "turnId": "turn-next" if advanced else "turn-old"}

    server = BrowserViewServer(
        provider,
        stop_provider,
        task_state_provider=task_state_provider,
    )
    server.start()
    try:
        page.goto(f"{server.url}?sessionId=session-alpha&lang=en")
        frame = viewer(page)
        expect(frame.get_by_role("heading", name="Inspect the latest task")).to_be_visible()
        frame.locator("body").evaluate(
            """() => {
              Object.defineProperty(HTMLVideoElement.prototype, "requestPictureInPicture", {
                configurable: true,
                value: () => Promise.reject(new DOMException(
                  "Picture-in-Picture is not available.",
                  "NotSupportedError",
                )),
              });
            }"""
        )
        frame.get_by_role("button", name="Live window").click()
        stop_button = frame.locator("#requestStop")
        expect(stop_button).to_be_enabled()
        assert candidate_started.wait(timeout=3)

        stop_button.click()
        assert stale_returned.wait(timeout=3)
        release_candidate.set()
        expect(stop_button).to_be_enabled()
        expect(frame.locator("#stopRequestStatus")).to_have_text("Off", timeout=3_000)
        expect(frame.locator("#liveDock")).to_have_attribute("data-task-state-source", "app-server")
        assert None in state_candidates
        assert [request["turnId"] for request in stop_requests] == ["turn-old"]

        stop_button.click()
        expect(stop_button).to_have_text("Requested")
        assert [request["turnId"] for request in stop_requests] == ["turn-old", "turn-next"]
    finally:
        release_candidate.set()
        server.close()


def test_loopback_auto_stop_does_not_repeat_for_goal_continuation(page: Page) -> None:
    payload = deepcopy(demo_trajectories()["session-alpha"])
    payload["turns"][-1]["status"] = "complete"
    payload["stats"]["rateLimits"]["primary"]["usedPercent"] = 91
    stop_requests: list[dict[str, object]] = []
    task_state: dict[str, object] = {"running": True, "turnId": "goal-turn-1"}

    def provider(name: str, arguments: dict[str, object]) -> dict[str, object]:
        if name == "get_codex_trajectory_update":
            return {
                "structuredContent": {
                    "schemaVersion": 1,
                    "unchanged": True,
                    "revision": "1" * 64,
                }
            }
        value = deepcopy(payload)
        value["detailLevel"] = arguments.get("detailLevel", "summary")
        return {"structuredContent": value}

    def stop_provider(value: dict[str, object]) -> dict[str, object]:
        stop_requests.append(value)
        return {"sent": True}

    server = BrowserViewServer(
        provider,
        stop_provider,
        task_state_provider=lambda _session_id, _turn_id: deepcopy(task_state),
    )
    server.start()
    try:
        page.goto(f"{server.url}?sessionId=session-alpha&lang=en")
        frame = viewer(page)
        expect(frame.get_by_role("heading", name="Inspect the latest task")).to_be_visible()
        frame.locator("body").evaluate(
            """() => {
              Object.defineProperty(HTMLVideoElement.prototype, "requestPictureInPicture", {
                configurable: true,
                value: () => Promise.reject(new DOMException(
                  "Picture-in-Picture is not available.",
                  "NotSupportedError",
                )),
              });
            }"""
        )
        frame.get_by_role("button", name="Live window").click()
        frame.locator("#autoStopEnabled").check()
        expect(frame.locator("#requestStop")).to_have_text("Requested")
        assert [request["turnId"] for request in stop_requests] == ["goal-turn-1"]

        task_state.update({"running": False, "turnId": None})
        expect(frame.locator("#requestStop")).to_have_text("Idle", timeout=3_000)
        task_state.update({"running": True, "turnId": "goal-turn-2"})
        expect(frame.locator("#requestStop")).to_be_enabled(timeout=3_000)
        expect(frame.locator("#stopRequestStatus")).to_have_text(
            "Triggered for this quota cycle · ≤ 10%"
        )
        page.wait_for_timeout(2_000)
        assert [request["turnId"] for request in stop_requests] == ["goal-turn-1"]
    finally:
        server.close()


def test_host_theme_tracks_codex_light_dark_and_system_resolution(
    page: Page, harness_url: str
) -> None:
    page.goto(f"{harness_url}/en-dock")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()
    root = frame.locator("html")
    expect(root).to_have_attribute("data-codex-theme", "dark")
    assert (
        root.evaluate("element => getComputedStyle(element).getPropertyValue('--bg').trim()")
        == "#141414"
    )

    root.evaluate("() => window.__setOpenAITheme('light')")
    expect(root).to_have_attribute("data-codex-theme", "light")
    assert (
        root.evaluate("element => getComputedStyle(element).getPropertyValue('--bg').trim()")
        == "#f7f7f7"
    )

    root.evaluate("() => window.__setOpenAITheme('dark')")
    expect(root).to_have_attribute("data-codex-theme", "dark")


def test_viewer_can_enable_and_disable_cdp_toolbar_setting(page: Page, harness_url: str) -> None:
    page.goto(f"{harness_url}/en")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()
    toggle = frame.locator("#cdpToolbarEnabled")
    expect(toggle).not_to_be_checked()
    expect(frame.locator("#cdpToolbarStatus")).to_have_text("Off; no debugging-port connection")

    toggle.check()
    page.wait_for_function("window.__trajectoryCdpToolbar.enabled === true")
    expect(toggle).to_be_checked()
    expect(frame.locator("#cdpToolbarStatus")).to_have_text(
        "Ready; opens in the Codex in-app Browser without a message"
    )
    assert page.evaluate("window.__trajectoryCdpToolbar.injected") is True

    frame.locator("#cdpToolbarPort").fill("9333")
    frame.locator("#cdpToolbarPort").press("Tab")
    page.wait_for_function("window.__trajectoryCdpToolbar.port === 9333")
    assert page.evaluate("window.__trajectoryCdpToolbar.enabled") is True

    toggle.uncheck()
    page.wait_for_function("window.__trajectoryCdpToolbar.enabled === false")
    expect(toggle).not_to_be_checked()


def test_safe_summary_search_filter_keyboard_and_detail_inspector(
    page: Page, harness_url: str
) -> None:
    page.goto(f"{harness_url}/en")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()
    assert frame.locator("tr.record").count() == 5
    turn_toggles = frame.locator(".turn-toggle")
    assert turn_toggles.count() == 2
    assert turn_toggles.first.get_attribute("aria-expanded") == "false"
    assert turn_toggles.last.get_attribute("aria-expanded") == "true"
    assert "Model gpt-5" in turn_toggles.first.inner_text()
    turn_groups = frame.locator("tbody.turn-group")
    assert turn_groups.count() == 2
    assert turn_groups.first.locator(".turn-token-label").all_inner_texts() == [
        "UNCACHED INPUT",
        "CACHE READS",
        "OUTPUT",
    ]
    assert turn_groups.first.locator(".turn-token-value").all_inner_texts() == ["64", "256", "72"]
    assert turn_groups.last.locator(".turn-token-value").all_inner_texts() == ["64", "128", "56"]
    assert turn_groups.locator(".turn-token").evaluate_all(
        "cells => cells.every(cell => !cell.hasAttribute('title'))"
    )
    assert turn_groups.locator(".turn-token-value").evaluate_all(
        "values => values.every(value => !value.hasAttribute('data-ledger-tooltip'))"
    )
    tooltip = frame.locator("#ledgerTooltip")
    assert frame.locator("thead").count() == 0
    assert frame.locator(".turn-column-row").count() == 1
    assert frame.locator(".turn-column-row th").all_inner_texts()[:5] == [
        "INDEX",
        "STEP",
        "EVENT",
        "CONTENT",
        "TIME",
    ]
    assert frame.locator('tr[data-id="record-2-7"] td').nth(1).inner_text() == "S2"
    assert frame.locator(".turn-column-row th").evaluate_all(
        "cells => cells.every(cell => getComputedStyle(cell).overflowX === 'hidden')"
    )
    assert (
        frame.locator(".turn-column-row").evaluate("element => getComputedStyle(element).position")
        == "sticky"
    )
    assert frame.get_by_text("Tool input, output, and raw metadata are hidden.").is_visible()
    token_panel = frame.locator("#tokenDetails")
    assert token_panel.get_by_text("Token details", exact=True).is_visible()
    assert (
        token_panel.locator('[data-token-metric="total"] .token-metric-value').inner_text() == "640"
    )
    assert (
        token_panel.locator('[data-token-metric="cached"] .token-metric-value').inner_text()
        == "384"
    )
    assert token_panel.locator(".token-metric[title]").count() == 0
    assert frame.locator(".stat[title]").count() == 0
    assert "cache is part of input and reasoning is part of output" in token_panel.inner_text()
    assert "Cache hit 75%" in token_panel.locator(".token-badges").inner_text()
    token_turns = token_panel.locator("details.token-turns")
    assert token_turns.evaluate("element => element.open") is False
    assert token_panel.locator(".token-turn-row").count() == 0
    token_turns.locator("summary").click()
    token_panel.locator(".token-turn-row").first.wait_for()
    assert token_panel.locator(".token-turn-row").count() == 2
    assert token_panel.locator(".token-turn-row").first.is_visible()
    assert token_panel.locator(".token-turn-row").first.locator(".token-cell").count() == 7
    assert token_panel.locator(".token-requests").count() == 0
    assert (
        turn_groups.first.locator(".turn-row").evaluate(
            "element => getComputedStyle(element).cursor"
        )
        == "pointer"
    )
    turn_groups.first.locator('[data-turn-token-kind="output"]').click()
    assert frame.locator(".turn-toggle").first.get_attribute("aria-expanded") == "true"
    assert frame.locator("tr.record").count() == 9
    frame.locator("tbody.turn-group").first.locator('[data-turn-token-kind="output"]').click()
    assert frame.locator(".turn-toggle").first.get_attribute("aria-expanded") == "false"
    assert frame.locator("tr.record").count() == 5
    turn_toggles.first.click()
    assert frame.locator("tr.record").count() == 9
    assert frame.locator(".turn-column-row").count() == 2
    assert turn_groups.first.locator(".turn-column-row th").all_inner_texts()[-3:] == [
        "UNCACHED INPUT",
        "CACHE READS",
        "OUTPUT",
    ]
    event_header = frame.locator(".turn-column-row th").nth(2)
    content_header = frame.locator(".turn-column-row th").nth(3)
    event_width = event_header.evaluate("element => element.getBoundingClientRect().width")
    content_width = content_header.evaluate("element => element.getBoundingClientRect().width")
    assert event_width > content_width
    assert event_width <= 150
    assert content_header.evaluate("element => element.getBoundingClientRect().width") <= 150
    assert frame.locator(".ledger-wrap").evaluate(
        "element => element.scrollWidth === element.clientWidth"
    )
    long_event = frame.locator('tr[data-id="record-2-7"] .event-name')
    assert long_event.evaluate("element => element.scrollWidth > element.clientWidth")
    long_event.hover()
    assert tooltip.is_visible()
    assert tooltip.inner_text() == "Subagent activity from the long-running reviewer worker"
    long_content = frame.locator('tr[data-id="record-2-7"] .summary')
    assert long_content.evaluate("element => element.scrollWidth > element.clientWidth")
    long_content.hover()
    assert tooltip.inner_text() == (
        "Reviewer completed after checking the full implementation and focused regressions"
    )
    short_event = frame.locator('tr[data-id="record-2-6"] .event-name')
    assert short_event.evaluate("element => element.scrollWidth === element.clientWidth")
    short_event.hover()
    assert tooltip.is_visible()
    assert tooltip.inner_text() == "exec"
    assert (
        frame.locator('tr[data-id="record-2-6"] .event-cell').get_attribute("data-ledger-tooltip")
        == "exec"
    )
    assert tooltip.evaluate(
        """(tooltip, selector) => {
          const target = document.querySelector(selector);
          return tooltip.getBoundingClientRect().bottom <= target.getBoundingClientRect().top;
        }""",
        'tr[data-id="record-2-6"] .event-cell',
    )
    frame.locator('tr[data-id="record-2-7"]').focus()
    assert "Event: Subagent activity from the long-running reviewer worker" in tooltip.inner_text()
    assert "Content: Reviewer completed after checking" in tooltip.inner_text()
    frame.locator('tr[data-id="record-2-7"]').press("Escape")
    assert not tooltip.is_visible()
    token_cells = frame.locator('tr[data-id="record-1-4"] .record-token')
    assert token_cells.count() == 3
    assert token_cells.all_inner_texts() == ["64", "256", "72"]
    assert token_cells.evaluate_all(
        "cells => cells.every(cell => !cell.hasAttribute('data-ledger-tooltip'))"
    )
    no_usage_tokens = frame.locator('tr[data-id="record-1-3"] .record-token')
    assert no_usage_tokens.all_inner_texts() == [
        "—",
        "—",
        "—",
    ]
    assert no_usage_tokens.evaluate_all(
        "cells => cells.every(cell => !cell.hasAttribute('data-ledger-tooltip'))"
    )
    detail_labels = frame.locator("details summary").all_inner_texts()
    assert "Input" not in detail_labels
    assert "Output" not in detail_labels
    assert "Metadata" not in detail_labels

    search = frame.locator("#search")
    search.fill("failure")
    assert frame.locator("tr.record").count() == 2
    search.fill("")
    frame.locator("#kindFilter").select_option("tool")
    assert frame.locator("tr.record").count() == 2
    frame.locator("#kindFilter").select_option("all")

    first = frame.locator("tr.record").first
    first.focus()
    first.press("Enter")
    assert frame.locator("#inspector h2").inner_text().startswith("#1")


def test_full_details_refresh_and_task_switch_safety(page: Page, harness_url: str) -> None:
    page.goto(f"{harness_url}/en")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()
    assert page.locator("#viewer").get_attribute("sandbox") == "allow-scripts"
    frame.get_by_role("button", name="Load full details").click()
    warning = frame.locator("#fullDetailsWarning")
    assert "source code, command output, and sensitive data" in warning.inner_text()
    assert frame.get_by_text("Safe summary", exact=True).is_visible()

    frame.get_by_role("button", name="Cancel").click()
    assert frame.get_by_role("button", name="Load full details").is_visible()

    frame.get_by_role("button", name="Load full details").click()
    frame.get_by_role("button", name="Continue loading").click()
    frame.get_by_text("Full details", exact=True).wait_for()
    frame.locator(".turn-toggle").first.click()
    frame.locator('tr[data-id="record-1-3"]').click()
    assert "uv run pytest" in frame.locator("#inspector").inner_text()

    open_pip = frame.get_by_role("button", name="Live window")
    expect(open_pip).to_be_enabled()
    open_pip.click()
    close_pip = frame.get_by_role("button", name="Close live window")
    expect(close_pip).to_have_attribute("aria-pressed", "true")
    expect(frame.locator("#pipVideo")).to_have_attribute("data-active", "true")
    payload = json.loads(frame.locator("#pipCanvas").get_attribute("data-payload") or "{}")
    assert payload["detailLevel"] == "summary"
    assert [limit["remainingPercent"] for limit in payload["rateLimits"]] == [68.5, 44]
    assert "uv run pytest" not in json.dumps(payload)
    assert frame.get_by_text("Full details", exact=True).is_visible()
    close_pip.click()
    frame.get_by_text("Full details", exact=True).wait_for()

    frame.get_by_role("button", name="Refresh").click()
    frame.get_by_text("Full details", exact=True).wait_for()
    assert frame.get_by_text("Full details", exact=True).is_visible()

    frame.locator("#sessionSelect").select_option("session-beta")
    frame.get_by_text("Safe summary", exact=True).wait_for()
    assert frame.locator("#sessionSelect").input_value() == "session-beta"
    assert frame.locator("tr.record").count() == 3
    assert frame.locator(".turn-toggle").count() == 1
    assert frame.locator(".turn-toggle").get_attribute("aria-expanded") == "true"


def test_live_pip_refreshes_index_and_tokens_then_stops(page: Page, harness_url: str) -> None:
    page.goto(f"{harness_url}/en")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()

    open_pip = frame.get_by_role("button", name="Live window")
    expect(open_pip).to_be_enabled()
    open_pip.click()
    close_pip = frame.get_by_role("button", name="Close live window")
    expect(close_pip).to_have_attribute("aria-pressed", "true")
    expect(frame.locator("#pipVideo")).to_have_attribute("data-active", "true")
    assert (
        frame.locator("html").evaluate("() => typeof window.openai?.requestDisplayMode")
        == "undefined"
    )
    expect(frame.locator("#pipCanvas")).to_have_attribute("data-cursor", "T2 / S4 / #9")
    expect(frame.locator("#pipCanvas")).to_have_attribute("data-tokens", "640,128,384,128,40")
    expect(frame.locator("#pipCanvas")).to_have_attribute(
        "data-quota",
        "primary:68.5:2026-08-14T02:00:00Z|secondary:44:2026-08-21T00:00:00Z",
    )
    assert frame.get_by_text("Safe summary", exact=True).is_visible()
    assert frame.locator("#ledger").count() == 1
    page.wait_for_function("window.__trajectoryToolNames.includes('get_codex_trajectory_update')")

    page.evaluate("window.__advanceTrajectoryLive()")
    expect(frame.locator("#pipCanvas")).to_have_attribute(
        "data-latest", "Live update arrived", timeout=6_000
    )
    expect(frame.locator("#pipCanvas")).to_have_attribute("data-cursor", "T3 / S1 / #10")
    expect(frame.locator("#pipCanvas")).to_have_attribute("data-tokens", "704,144,416,144,44")
    expect(frame.locator("#pipCanvas")).to_have_attribute(
        "data-quota",
        "primary:68:2026-08-14T02:00:00Z|secondary:44:2026-08-21T00:00:00Z",
    )
    live_calls = page.evaluate(
        """() => window.__trajectoryCalls.filter(
          (_, index) => window.__trajectoryToolNames[index] === 'get_codex_trajectory_update'
        )"""
    )
    assert len(live_calls) >= 2
    assert live_calls[0].get("revision") is None
    assert live_calls[-1]["revision"] == "1".zfill(64)

    close_pip.click()
    frame.get_by_role("button", name="Live window").wait_for()
    expect(frame.locator("#pipVideo")).to_have_attribute("data-active", "false")
    live_call_count = (
        "toolName => window.__trajectoryToolNames.filter(name => name === toolName).length"
    )
    stopped_at = page.evaluate(live_call_count, "get_codex_trajectory_update")
    page.wait_for_timeout(2_700)
    assert page.evaluate(live_call_count, "get_codex_trajectory_update") == stopped_at


def test_unavailable_native_pip_falls_back_to_in_page_live_view(
    page: Page, harness_url: str
) -> None:
    page.goto(f"{harness_url}/en-pip-unavailable")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()

    open_live = frame.get_by_role("button", name="Live window")
    expect(open_live).to_be_enabled()
    open_live.click()

    dock = frame.locator("#liveDock")
    dock.wait_for()
    expect(dock).to_have_attribute("data-presentation", "inline-live")
    expect(frame.get_by_text("In-page live view", exact=True)).to_be_visible()
    expect(frame.locator("#requestStop")).to_be_disabled()
    assert frame.get_by_role("alert").count() == 0
    assert page.evaluate("window.__trajectoryDisplayModes") == []
    page.wait_for_function("window.__trajectoryToolNames.includes('get_codex_trajectory_update')")

    frame.get_by_role("button", name="Return to inline view").click()
    frame.get_by_role("button", name="Live window").wait_for()
    assert frame.locator("#liveDock").count() == 0
    assert not frame.locator("body").evaluate("body => body.classList.contains('dock-mode')")


def test_codex_host_uses_full_height_frozen_totals_and_scrolling_live_output(
    page: Page, harness_url: str
) -> None:
    page.goto(f"{harness_url}/en-dock")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()

    frame.get_by_role("button", name="Live window").click()
    dock = frame.locator("#liveDock")
    dock.wait_for()
    expect(dock).to_have_attribute("data-presentation", "docked")
    expect(dock).to_have_attribute("data-detail-level", "summary")
    expect(dock).to_have_attribute("data-cursor", "T2 / S4 / #9")
    expect(dock).to_have_attribute("data-tokens", "640,128,384,128,40")
    expect(dock).to_have_attribute(
        "data-quota",
        "primary:68.5:2026-08-14T02:00:00Z|secondary:44:2026-08-21T00:00:00Z",
    )
    expect(dock).to_have_attribute("data-record-count", "9")
    assert frame.get_by_text("Codex side panel", exact=True).is_visible()
    assert frame.get_by_text("Task total", exact=True).is_visible()
    assert frame.get_by_text("Live output", exact=True).is_visible()
    quota = frame.locator("#dockQuota")
    assert quota.is_visible()
    assert quota.locator(".dock-quota-window span").all_inner_texts() == ["5h", "Weekly"]
    assert quota.locator(".dock-quota-window strong").all_inner_texts() == ["68.5%", "44%"]
    assert quota.get_attribute("aria-label") == (
        "Codex quota: 5h 68.5% remaining, Weekly 44% remaining"
    )
    assert quota.evaluate(
        """element => {
          const quota = element.getBoundingClientRect();
          const close = document.querySelector('#closeDock').getBoundingClientRect();
          return quota.right <= close.left && quota.top < 50;
        }"""
    )
    assert frame.locator(".dock-total-value").inner_text() == "640"
    assert frame.locator("#pipVideo").count() == 0
    assert frame.get_by_role("alert").count() == 0
    assert frame.locator("body").evaluate("body => body.classList.contains('dock-mode')")
    assert dock.evaluate(
        """element => {
          const rect = element.getBoundingClientRect();
          return rect.top <= 1 && rect.left <= 1
            && rect.right >= window.innerWidth - 1
            && rect.bottom >= window.innerHeight - 1;
        }"""
    )
    fixed_summary = frame.locator("#dockFixedSummary")
    record_stream = frame.locator("#liveRecordStream")
    assert fixed_summary.is_visible()
    assert fixed_summary.evaluate(
        "element => !document.querySelector('#liveRecordStream').contains(element)"
    )
    assert record_stream.evaluate("element => getComputedStyle(element).overflowY") == "auto"
    assert frame.locator(".dock-record").count() == 9
    expect(frame.locator('.dock-record[data-index="8"] .dock-record-state')).to_have_text(
        "complete · —"
    )
    latest_record = frame.locator(".dock-record.latest")
    expect(latest_record).to_have_attribute("data-index", "9")
    expect(latest_record).to_have_attribute("data-record-tokens", "248,64,128,56,16")
    whale_miner = latest_record.locator(".dock-whale-miner")
    expect(whale_miner).to_have_attribute("data-record-id", "record-2-9")
    expect(whale_miner).to_have_attribute("data-mining", "false")
    assert whale_miner.evaluate(
        """element => {
          const card = element.closest('.dock-record');
          const sprite = element.getBoundingClientRect();
          const frame = card.getBoundingClientRect();
          const epsilon = .5;
          return sprite.left >= frame.left - epsilon
            && sprite.top >= frame.top - epsilon
            && sprite.right <= frame.right + epsilon
            && sprite.bottom <= frame.bottom + epsilon
            && sprite.width <= 64 + epsilon
            && sprite.height <= 64 + epsilon
            && getComputedStyle(card).overflow === 'hidden';
        }"""
    )
    assert latest_record.locator(".dock-record-event").evaluate(
        "element => parseFloat(getComputedStyle(element).paddingLeft) >= 68"
    )
    assert latest_record.locator(".dock-whale-miner-sheet").evaluate(
        """element => getComputedStyle(element).backgroundImage
          .startsWith('url("data:image/png;base64,')"""
    )
    usage_rows = latest_record.locator(".dock-usage-row")
    assert usage_rows.count() == 3
    assert usage_rows.locator(".dock-usage-name").all_inner_texts() == [
        "TOTAL TOKENS",
        "INPUT",
        "OUTPUT",
    ]
    assert usage_rows.locator(".dock-usage-value").all_inner_texts() == ["248", "192", "56"]
    input_row = latest_record.locator('[data-token-group="input"]')
    assert input_row.locator(".dock-usage-part-label").all_inner_texts() == [
        "Uncached input",
        "Cache reads",
    ]
    assert input_row.locator(".dock-usage-part-value").all_inner_texts() == ["64", "128"]
    output_row = latest_record.locator('[data-token-group="output"]')
    assert output_row.locator(".dock-usage-part-label").all_inner_texts() == [
        "Visible output",
        "Reasoning output",
    ]
    assert output_row.locator(".dock-usage-part-value").all_inner_texts() == ["40", "16"]
    assert record_stream.evaluate(
        "element => Math.abs(element.scrollHeight - element.clientHeight - element.scrollTop) <= 2"
    )
    page.wait_for_function("window.__trajectoryDisplayModes.length === 1")
    assert page.evaluate("window.__trajectoryDisplayModes") == ["fullscreen"]
    page.wait_for_function("window.__trajectoryToolNames.includes('get_codex_trajectory_update')")

    status_before = frame.locator(".dock-status").evaluate(
        """element => {
          document.querySelector('#liveDock').dataset.stabilityProbe = 'kept';
          element.dataset.stabilityProbe = 'kept';
          const rect = element.getBoundingClientRect();
          return {top: rect.top, left: rect.left, width: rect.width, height: rect.height};
        }"""
    )
    page.wait_for_timeout(2_200)
    expect(dock).to_have_attribute("data-stability-probe", "kept")
    expect(frame.locator(".dock-status")).to_have_attribute("data-stability-probe", "kept")
    expect(frame.locator("#dockLiveState")).to_have_text("Live refresh")
    assert (
        frame.locator(".dock-live-dot").evaluate(
            "element => getComputedStyle(element).animationName"
        )
        == "none"
    )
    status_after = frame.locator(".dock-status").evaluate(
        """element => {
          const rect = element.getBoundingClientRect();
          return {top: rect.top, left: rect.left, width: rect.width, height: rect.height};
        }"""
    )
    assert status_after == status_before

    page.evaluate("window.__advanceTrajectoryLive()")
    expect(dock).to_have_attribute("data-latest", "Live update arrived", timeout=6_000)
    expect(dock).to_have_attribute("data-cursor", "T3 / S1 / #10")
    expect(dock).to_have_attribute("data-tokens", "704,144,416,144,44")
    expect(dock).to_have_attribute(
        "data-quota",
        "primary:68:2026-08-14T02:00:00Z|secondary:44:2026-08-21T00:00:00Z",
    )
    expect(dock).to_have_attribute("data-record-count", "10")
    expect(frame.locator(".dock-total-value")).to_have_text("704")
    expect(quota.locator('[data-quota-window="primary"] strong')).to_have_text("68%")
    latest_record = frame.locator(".dock-record.latest")
    expect(latest_record).to_have_attribute("data-index", "10")
    whale_miner = latest_record.locator(".dock-whale-miner")
    expect(whale_miner).to_have_attribute("data-record-id", "record-3-10")
    expect(whale_miner).to_have_attribute("data-mining", "true")
    assert (
        whale_miner.locator(".dock-whale-miner-y").evaluate(
            "element => getComputedStyle(element).animationName"
        )
        == "dock-whale-mining-y"
    )
    assert (
        whale_miner.locator(".dock-whale-miner-sheet").evaluate(
            "element => getComputedStyle(element).animationName"
        )
        == "dock-whale-mining-x"
    )
    expect(latest_record).to_have_attribute("data-record-tokens", "64,16,32,16,4")
    assert latest_record.locator(".dock-usage-value").all_inner_texts() == ["64", "48", "16"]
    assert latest_record.locator(
        '[data-token-group="output"] .dock-usage-part-value'
    ).all_inner_texts() == [
        "12",
        "4",
    ]
    assert "Live update arrived" in latest_record.inner_text()
    assert record_stream.evaluate(
        "element => Math.abs(element.scrollHeight - element.clientHeight - element.scrollTop) <= 2"
    )
    expect(whale_miner).to_have_attribute("data-mining", "false", timeout=2_500)
    whale_miner.evaluate("element => { element.dataset.stabilityProbe = 'kept'; }")
    page.wait_for_timeout(1_300)
    expect(whale_miner).to_have_attribute("data-stability-probe", "kept")
    expect(whale_miner).to_have_attribute("data-mining", "false")

    frame.get_by_role("button", name="Return to inline view").click()
    frame.get_by_role("button", name="Live window").wait_for()
    page.wait_for_function("window.__trajectoryDisplayModes.length === 2")
    assert page.evaluate("window.__trajectoryDisplayModes") == ["fullscreen", "inline"]
    assert frame.locator("#liveDock").count() == 0
    assert not frame.locator("body").evaluate("body => body.classList.contains('dock-mode')")

    live_call_count = (
        "toolName => window.__trajectoryToolNames.filter(name => name === toolName).length"
    )
    stopped_at = page.evaluate(live_call_count, "get_codex_trajectory_update")
    page.wait_for_timeout(2_700)
    assert page.evaluate(live_call_count, "get_codex_trajectory_update") == stopped_at


def test_codex_dock_sends_one_click_stop_request(page: Page, harness_url: str) -> None:
    page.goto(f"{harness_url}/en-dock")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()
    page.evaluate("window.__setTrajectoryRunning(true)")
    frame.get_by_role("button", name="Live window").click()

    dock = frame.locator("#liveDock")
    stop_button = frame.locator("#requestStop")
    expect(stop_button).to_be_enabled()
    expect(frame.locator("#autoStopEnabled")).not_to_be_checked()
    expect(frame.locator("#autoStopThreshold")).to_have_value("10")
    expect(frame.locator("#stopRequestStatus")).to_have_text("Off")
    assert stop_button.evaluate(
        """element => {
          const stop = element.getBoundingClientRect();
          const quota = document.querySelector('#dockQuota').getBoundingClientRect();
          const close = document.querySelector('#closeDock').getBoundingClientRect();
          return quota.right <= stop.left && stop.right <= close.left;
        }"""
    )

    stop_button.click()
    page.wait_for_function("window.__trajectoryFollowUps.length === 1")
    request = page.evaluate("window.__trajectoryFollowUps[0]")
    assert request["scrollToBottom"] is False
    assert "recursively interrupt every active related subagent" in request["prompt"]
    assert "Preserve every worktree" in request["prompt"]
    assert "Do not delete, clean, reset, or roll back files" in request["prompt"]
    expect(stop_button).to_have_text("Requested")
    expect(stop_button).to_be_disabled()
    expect(dock).to_have_attribute("data-stop-status", "sent")
    expect(frame.locator("#stopRequestStatus")).to_have_text("Stop requested")
    page.wait_for_timeout(1_200)
    assert page.evaluate("window.__trajectoryFollowUps.length") == 1

    page.evaluate("window.__setTrajectoryRunning(false)")
    expect(stop_button).to_have_text("Idle")
    page.evaluate("window.__advanceTrajectoryLive()")
    expect(stop_button).to_be_enabled()
    expect(stop_button).to_have_text("Stop")
    expect(frame.locator("#stopRequestStatus")).to_have_text("Off")
    stop_button.click()
    page.wait_for_function("window.__trajectoryFollowUps.length === 2")


def test_codex_dock_auto_stops_once_when_remaining_quota_crosses_threshold(
    page: Page, harness_url: str
) -> None:
    page.goto(f"{harness_url}/en-dock")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()
    page.evaluate("window.__setTrajectoryRunning(true)")
    frame.get_by_role("button", name="Live window").click()
    dock = frame.locator("#liveDock")

    frame.locator("#autoStopEnabled").check()
    expect(dock).to_have_attribute("data-auto-stop", "true")
    expect(dock).to_have_attribute("data-auto-stop-threshold", "10")
    expect(frame.locator("#stopRequestStatus")).to_have_text("Armed · ≤ 10%")
    page.wait_for_function("window.__trajectoryWidgetStates.length >= 1")
    armed_state = page.evaluate("window.__trajectoryWidgetStates.at(-1)")
    assert armed_state["codexTrajectoryStopGuard"] == {
        "enabled": True,
        "threshold": 10,
        "firedKey": None,
    }

    page.evaluate("window.__setTrajectoryRemaining(9)")
    page.wait_for_function("window.__trajectoryFollowUps.length === 1")
    request = page.evaluate("window.__trajectoryFollowUps[0]")
    assert "5h 9%" in request["prompt"]
    assert "automatic stop threshold of ≤ 10% remaining" in request["prompt"]
    expect(dock).to_have_attribute("data-stop-status", "sent")
    expect(frame.locator("#requestStop")).to_have_text("Requested")
    page.wait_for_function(
        "window.__trajectoryWidgetStates.at(-1)?.codexTrajectoryStopGuard?.firedKey"
    )
    fired_state = page.evaluate("window.__trajectoryWidgetStates.at(-1)")
    guard = fired_state["codexTrajectoryStopGuard"]
    assert guard["enabled"] is True
    assert guard["threshold"] == 10
    assert guard["firedKey"].startswith("v3:session-alpha:10:primary:300:")

    page.wait_for_timeout(2_500)
    assert page.evaluate("window.__trajectoryFollowUps.length") == 1


def test_codex_dock_auto_stop_retries_after_a_transient_failure(
    page: Page, harness_url: str
) -> None:
    page.goto(f"{harness_url}/en-dock")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()
    page.evaluate("window.__setTrajectoryRunning(true)")
    page.frames[1].evaluate("window.__trajectoryFollowUpFailures = 1")
    frame.get_by_role("button", name="Live window").click()
    frame.locator("#autoStopEnabled").check()

    page.evaluate("window.__setTrajectoryRemaining(9)")
    expect(frame.locator("#stopRequestStatus")).to_contain_text("Temporary follow-up failure")
    assert page.evaluate("window.__trajectoryFollowUps.length") == 0
    failed_state = page.evaluate("window.__trajectoryWidgetStates.at(-1)")
    assert failed_state["codexTrajectoryStopGuard"]["firedKey"] is None

    page.wait_for_function("window.__trajectoryFollowUps.length === 1", timeout=6_000)
    expect(frame.locator("#stopRequestStatus")).to_have_text("Stop requested")
    retry_state = page.evaluate("window.__trajectoryWidgetStates.at(-1)")
    assert retry_state["codexTrajectoryStopGuard"]["firedKey"].startswith("v3:session-alpha:10:")


def test_codex_dock_goal_continuation_does_not_retrigger_in_same_quota_cycle(
    page: Page, harness_url: str
) -> None:
    page.goto(f"{harness_url}/en-dock")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()
    page.evaluate("window.__setTrajectoryRunning(true)")
    frame.get_by_role("button", name="Live window").click()
    frame.locator("#autoStopEnabled").check()

    page.evaluate("window.__setTrajectoryRemaining(9)")
    page.wait_for_function("window.__trajectoryFollowUps.length === 1")
    first_state = page.evaluate("window.__trajectoryWidgetStates.at(-1)")
    assert first_state["codexTrajectoryStopGuard"]["firedKey"].startswith("v3:session-alpha:10:")

    page.evaluate(
        """() => {
          window.__setTrajectoryRunning(false);
          window.__advanceTrajectoryLive();
          window.__setTrajectoryRemaining(9);
        }"""
    )
    expect(frame.locator("#requestStop")).to_be_enabled(timeout=4_000)
    expect(frame.locator("#stopRequestStatus")).to_have_text(
        "Triggered for this quota cycle · ≤ 10%"
    )
    expect(frame.locator("#liveDock")).to_have_attribute("data-auto-stop-latched", "true")
    page.wait_for_timeout(2_500)
    assert page.evaluate("window.__trajectoryFollowUps.length") == 1


def test_codex_dock_auto_stop_rearms_after_observed_quota_recovery(
    page: Page, harness_url: str
) -> None:
    page.goto(f"{harness_url}/en-dock")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()
    page.evaluate("window.__setTrajectoryRunning(true)")
    frame.get_by_role("button", name="Live window").click()
    frame.locator("#autoStopEnabled").check()

    page.evaluate("window.__setTrajectoryRemaining(9)")
    page.wait_for_function("window.__trajectoryFollowUps.length === 1")
    page.evaluate(
        """() => {
          window.__setTrajectoryRunning(false);
          window.__advanceTrajectoryLive();
        }"""
    )
    expect(frame.locator("#stopRequestStatus")).to_have_text("Armed · ≤ 10%", timeout=4_000)
    page.wait_for_function(
        "window.__trajectoryWidgetStates.at(-1)?.codexTrajectoryStopGuard?.firedKey === null"
    )

    page.evaluate("window.__setTrajectoryRemaining(9)")
    page.wait_for_function("window.__trajectoryFollowUps.length === 2", timeout=6_000)
    second_state = page.evaluate("window.__trajectoryWidgetStates.at(-1)")
    assert second_state["codexTrajectoryStopGuard"]["firedKey"].startswith("v3:session-alpha:10:")


def test_codex_dock_auto_stop_rearms_after_quota_window_reset(page: Page, harness_url: str) -> None:
    page.goto(f"{harness_url}/en-dock")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()
    page.evaluate("window.__setTrajectoryRunning(true)")
    frame.get_by_role("button", name="Live window").click()
    frame.locator("#autoStopEnabled").check()

    page.evaluate("window.__setTrajectoryRemaining(9)")
    page.wait_for_function("window.__trajectoryFollowUps.length === 1")
    page.evaluate(
        """() => {
          window.__setTrajectoryRunning(false);
          window.__advanceTrajectoryLive();
          window.__setTrajectoryRemaining(9);
        }"""
    )
    expect(frame.locator("#stopRequestStatus")).to_have_text(
        "Triggered for this quota cycle · ≤ 10%", timeout=4_000
    )

    page.evaluate("window.__setTrajectoryResetAt('2026-08-14T07:00:00Z')")
    page.wait_for_function("window.__trajectoryFollowUps.length === 2", timeout=6_000)
    reset_state = page.evaluate("window.__trajectoryWidgetStates.at(-1)")
    assert reset_state["codexTrajectoryStopGuard"]["firedKey"].endswith(
        "primary:300:2026-08-14T07:00:00Z|secondary:10080:2026-08-21T00:00:00Z"
    )


def test_stop_controls_are_disabled_after_selecting_a_different_task(
    page: Page, harness_url: str
) -> None:
    page.goto(f"{harness_url}/en-dock")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()
    frame.locator("#sessionSelect").select_option("session-beta")
    frame.get_by_role("heading", name="Review the documentation").wait_for()
    frame.get_by_role("button", name="Live window").click()

    expect(frame.locator("#requestStop")).to_be_disabled()
    expect(frame.locator("#stopRequestStatus")).to_have_text(
        "Stop controls apply only to the Codex task that opened this trajectory"
    )
    assert page.evaluate("window.__trajectoryFollowUps.length") == 0


def test_codex_dock_waits_for_a_running_task_before_auto_stop(page: Page, harness_url: str) -> None:
    page.goto(f"{harness_url}/en-dock")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()
    frame.get_by_role("button", name="Live window").click()

    dock = frame.locator("#liveDock")
    stop_button = frame.locator("#requestStop")
    expect(dock).to_have_attribute("data-task-running", "false")
    expect(stop_button).to_be_disabled()
    expect(stop_button).to_have_text("Idle")
    expect(frame.locator("#stopRequestStatus")).to_have_text("The current task is already stopped")

    frame.locator("#autoStopEnabled").check()
    expect(frame.locator("#stopRequestStatus")).to_have_text("Armed · waiting for the task to run")
    page.evaluate("window.__setTrajectoryRemaining(9)")
    page.wait_for_timeout(1_300)
    assert page.evaluate("window.__trajectoryFollowUps.length") == 0
    waiting_state = page.evaluate("window.__trajectoryWidgetStates.at(-1)")
    assert waiting_state["codexTrajectoryStopGuard"]["firedKey"] is None

    page.evaluate("window.__setTrajectoryRunning(true)")
    page.wait_for_function("window.__trajectoryFollowUps.length === 1")
    expect(dock).to_have_attribute("data-task-running", "true")
    expect(dock).to_have_attribute("data-stop-status", "sent")
    expect(stop_button).to_have_text("Requested")


def test_tool_error_is_reported_without_locking_controls(page: Page, harness_url: str) -> None:
    page.goto(f"{harness_url}/en")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()
    frame.locator("#sessionSelect").select_option("session-missing")

    alert = frame.get_by_role("alert")
    assert "Task disappeared" in alert.inner_text()
    assert frame.get_by_role("button", name="Refresh").is_enabled()

    frame.locator("#sessionSelect").select_option("session-beta")
    frame.get_by_role("heading", name="Review the documentation").wait_for()


def test_hostile_task_content_is_rendered_only_as_text(page: Page, harness_url: str) -> None:
    page.goto(f"{harness_url}/en")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()
    frame.locator("#sessionSelect").select_option("session-xss")

    hostile = '<img src=x onerror="window.__trajectoryXss=true">'
    assert frame.locator("h1").inner_text() == hostile
    assert frame.locator("img, svg").count() == 0
    assert frame.locator(".event-name").inner_text() == hostile
    assert frame.locator("body").evaluate("element => window.__trajectoryXss") is None

    frame.get_by_role("button", name="Load full details").click()
    frame.get_by_role("button", name="Continue loading").click()
    frame.get_by_text("Full details", exact=True).wait_for()
    frame.locator("tr.record").click()
    assert hostile in frame.locator("#inspector").inner_text()
    assert frame.locator("img, svg").count() == 0
    assert frame.locator("body").evaluate("element => window.__trajectoryXss") is None


def test_large_task_materializes_only_the_latest_turn(page: Page, harness_url: str) -> None:
    page.goto(f"{harness_url}/en")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()
    frame.locator("#sessionSelect").select_option("session-large")
    frame.get_by_role("heading", name="Inspect a 500-record task").wait_for()

    assert frame.locator(".turn-toggle").count() == 100
    assert frame.locator("tr.record").count() == 5
    assert frame.locator(".turn-toggle").first.get_attribute("aria-expanded") == "false"
    assert frame.locator(".turn-toggle").last.get_attribute("aria-expanded") == "true"
    assert "Model gpt-5" in frame.locator(".turn-toggle").last.inner_text()
    assert frame.locator("tbody.turn-group").last.locator(
        ".turn-token-value"
    ).all_inner_texts() == ["64", "0", "12"]
    assert frame.locator(".turn-column-row").count() == 1
    assert frame.locator(".token-turn-row").count() == 0
    ledger_bounds = frame.locator(".ledger-wrap").evaluate(
        "element => { const bounds = element.getBoundingClientRect(); "
        "return { left: bounds.left, right: bounds.right }; }"
    )
    table_bounds = frame.locator("#ledger").evaluate(
        "element => { const bounds = element.getBoundingClientRect(); "
        "return { left: bounds.left, right: bounds.right }; }"
    )
    assert table_bounds["left"] - ledger_bounds["left"] >= 18
    assert ledger_bounds["right"] - table_bounds["right"] >= 18

    frame.locator(".turn-toggle").first.click()
    assert frame.locator("tr.record").count() == 10
    assert frame.locator(".turn-column-row").count() == 2


def test_load_earlier_records_pages_without_duplicates_or_scroll_jump(
    page: Page, harness_url: str
) -> None:
    page.goto(f"{harness_url}/en")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()
    frame.locator("#sessionSelect").select_option("session-paged")
    frame.get_by_role("heading", name="Inspect a 1,205-record task").wait_for()

    pagination = frame.locator(".pagination-bar")
    assert "500 / 1,205 records loaded" in pagination.inner_text()
    assert frame.locator("tbody.turn-group").count() == 100
    assert frame.locator("tbody.turn-group").first.get_attribute("data-turn") == "142"
    assert frame.locator("tbody.turn-group").last.get_attribute("data-turn") == "241"
    assert frame.locator("tr.record").count() == 5
    ledger = frame.locator(".ledger-wrap")
    before_bottom = ledger.evaluate(
        "element => element.scrollHeight - element.scrollTop - element.clientHeight"
    )

    frame.get_by_role("button", name="Load earlier records").click()
    frame.get_by_text("1,000 / 1,205 records loaded", exact=False).wait_for()
    after_bottom = ledger.evaluate(
        "element => element.scrollHeight - element.scrollTop - element.clientHeight"
    )
    assert abs(after_bottom - before_bottom) <= 2
    assert frame.locator("tbody.turn-group").count() == 200
    assert frame.locator("tbody.turn-group").first.get_attribute("data-turn") == "42"
    assert frame.locator("tbody.turn-group").last.get_attribute("data-turn") == "241"
    assert frame.locator("tr.record").count() == 5

    calls = page.evaluate("window.__trajectoryCalls")
    assert calls[-1]["sessionId"] == "session-paged"
    assert calls[-1]["maxRecords"] == 500
    assert calls[-1]["beforeRecord"] == 706

    frame.get_by_role("button", name="Load earlier records").click()
    pagination.wait_for(state="detached")
    assert frame.locator("tbody.turn-group").count() == 241
    assert frame.locator("tbody.turn-group").first.get_attribute("data-turn") == "1"
    assert frame.locator("tbody.turn-group").last.get_attribute("data-turn") == "241"
    assert frame.locator("tbody.turn-group").evaluate_all(
        "groups => new Set(groups.map(group => group.dataset.turn)).size === groups.length"
    )
    calls = page.evaluate("window.__trajectoryCalls")
    assert calls[-1]["beforeRecord"] == 206


def test_hundred_billion_token_values_are_fully_visible(page: Page, harness_url: str) -> None:
    page.goto(f"{harness_url}/en")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()
    frame.locator("#sessionSelect").select_option("session-big-tokens")
    frame.get_by_role("heading", name="Inspect a 119-billion-token task").wait_for()

    expected = {
        "total": "119,234,337,188",
        "input": "118,700,200,000",
        "cached": "115,665,200,000",
        "uncached": "3,035,000,000",
        "output": "534,137,188",
        "reasoning": "400,000,000",
    }
    for key, value in expected.items():
        metric = frame.locator(f'[data-token-metric="{key}"] .token-metric-value')
        assert metric.inner_text() == value
        assert metric.evaluate("element => element.scrollWidth <= element.clientWidth + 1")
        assert "…" not in metric.inner_text()
    assert frame.get_by_text("Cache writes", exact=True).count() == 0
    assert frame.locator(".token-metric").count() == 6
    assert (
        frame.locator('[data-token-metric="total"] .token-metric-value').evaluate(
            "element => getComputedStyle(element).textOverflow"
        )
        == "clip"
    )
    frame.locator("details.token-turns summary").click()
    numeric_cells = frame.locator(".token-turn-row .token-cell.numeric")
    assert numeric_cells.all_inner_texts() == [
        "1",
        "119,234,337,188",
        "118,700,200,000",
        "97.4%",
        "534,137,188",
        "400,000,000",
    ]
    assert numeric_cells.evaluate_all(
        "cells => cells.every(cell => cell.scrollWidth <= cell.clientWidth + 1)"
    )


def test_timeline_selection_native_wheel_zoom_and_reset(page: Page, harness_url: str) -> None:
    page.goto(f"{harness_url}/en")
    frame = viewer(page)
    timeline = frame.locator("#timeline")
    bounds = timeline.bounding_box()
    assert bounds is not None
    page.mouse.move(bounds["x"] + bounds["width"] * 0.15, bounds["y"] + bounds["height"] / 2)
    page.mouse.down()
    page.mouse.move(bounds["x"] + bounds["width"] * 0.65, bounds["y"] + bounds["height"] / 2)
    page.mouse.up()
    assert frame.locator("#rangeChip").inner_text().startswith("Time range")

    timeline.click(button="right", position={"x": 8, "y": 8})
    assert frame.locator("#rangeChip").inner_text() == ""

    before = frame.locator("#tickEnd").inner_text()
    timeline.hover(position={"x": bounds["width"] / 2, "y": bounds["height"] / 2})
    page.mouse.wheel(0, -600)
    after = frame.locator("#tickEnd").inner_text()
    assert after != before
    frame.get_by_role("button", name="Reset view").click()
    assert frame.locator("#tickEnd").inner_text() == before


def test_english_desktop_and_chinese_mobile_layout(page: Page, harness_url: str) -> None:
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{harness_url}/en")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()
    content_columns = frame.locator(".content").evaluate(
        "element => getComputedStyle(element).gridTemplateColumns"
    )
    assert "340px" in content_columns

    page.set_viewport_size({"width": 600, "height": 900})
    page.goto(f"{harness_url}/zh")
    frame = viewer(page)
    frame.get_by_text("安全摘要", exact=True).wait_for()
    frame.get_by_role("button", name="加载完整详情").click()
    assert frame.get_by_role("button", name="继续加载").is_visible()
    assert frame.get_by_role("button", name="取消").is_visible()
    assert frame.get_by_text("Token 详情", exact=True).is_visible()
    assert frame.locator(".token-metric[title]").count() == 0
    assert frame.locator("tr.record").count() == 5
    assert frame.locator(".turn-toggle").first.get_attribute("aria-expanded") == "false"
    assert frame.locator(".turn-toggle").last.get_attribute("aria-expanded") == "true"
    assert "模型 gpt-5" in frame.locator(".turn-toggle").last.inner_text()
    assert frame.locator("tbody.turn-group").last.locator(
        ".turn-token-label"
    ).all_inner_texts() == ["非缓存输入", "缓存读取", "输出"]
    assert frame.locator("tbody.turn-group").last.locator(
        ".turn-token-value"
    ).all_inner_texts() == ["64", "128", "56"]
    frame.locator("tbody.turn-group").first.locator('[data-turn-token-kind="output"]').click()
    assert frame.locator(".turn-toggle").first.get_attribute("aria-expanded") == "true"
    assert frame.locator("tr.record").count() == 9
    frame.locator("tbody.turn-group").first.locator('[data-turn-token-kind="output"]').click()
    assert frame.locator(".turn-toggle").first.get_attribute("aria-expanded") == "false"
    assert frame.locator("tr.record").count() == 5
    turn_columns = frame.locator(".turn-column-row th")
    assert turn_columns.all_inner_texts()[:5] == ["索引", "步骤", "事件", "内容", "耗时"]
    assert frame.locator('tr[data-id="record-2-7"] td').nth(1).inner_text() == "S2"
    assert frame.locator('tr[data-id="record-2-8"] td.duration').inner_text() == "—"
    assert turn_columns.all_inner_texts()[-3:] == ["非缓存输入", "缓存读取", "输出"]
    assert turn_columns.last.is_visible()
    assert (
        turn_columns.nth(1).evaluate("element => getComputedStyle(element).display") == "table-cell"
    )
    assert turn_columns.nth(2).evaluate(
        "element => element.getBoundingClientRect().width"
    ) > turn_columns.nth(3).evaluate("element => element.getBoundingClientRect().width")
    ledger_wrap = frame.locator(".ledger-wrap")
    assert ledger_wrap.evaluate("element => element.scrollWidth === element.clientWidth")
    assert ledger_wrap.evaluate("element => getComputedStyle(element).overflowX") == "hidden"
    assert turn_columns.nth(2).evaluate("element => element.getBoundingClientRect().width") <= 100
    assert turn_columns.last.evaluate(
        "element => element.getBoundingClientRect().right"
    ) <= ledger_wrap.evaluate("element => element.getBoundingClientRect().right")
    assert (
        frame.locator(".content").evaluate(
            "element => getComputedStyle(element).gridTemplateColumns"
        )
        == "600px"
    )

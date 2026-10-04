"""Browser acceptance coverage for the trajectory app resource."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from codex_trajectory.projection import parse_session, ui_html
from conftest import write_rollout
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
              window.__trajectoryCanvasTexts = [];
              const fillText = CanvasRenderingContext2D.prototype.fillText;
              define(CanvasRenderingContext2D.prototype, "fillText", {
                configurable: true,
                value(text, ...args) {
                  window.__trajectoryCanvasTexts.push({text: String(text), font: this.font});
                  return fillText.call(this, text, ...args);
                },
              });
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


@pytest.mark.parametrize("width", [400, 600, 800, 1280])
@pytest.mark.parametrize("locale", ["en", "zh"])
def test_long_title_keeps_header_actions_reachable(
    page: Page, harness_url: str, width: int, locale: str
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    try:
        page.goto(f"{harness_url}/{locale}")
        frame = viewer(page)
        expect(frame.locator("#refresh")).to_be_visible()
        frame.locator(".topbar").evaluate(
            """header => {
              header.querySelector('h1').textContent = 'LongProjectName'.repeat(20);
              header.querySelector('.subtitle').textContent = '/workspace/'.repeat(40);
              const select = header.querySelector('select');
              select.selectedOptions[0].textContent = 'LongSessionName'.repeat(20);
            }"""
        )
        for selector in ("h1", ".subtitle", "#sessionSelect", "#openPip", "#refresh"):
            assert frame.locator(selector).evaluate(
                """element => {
                  const r = element.getBoundingClientRect();
                  const header = element.closest('.topbar').getBoundingClientRect();
                  const x = (r.left + r.right) / 2, y = (r.top + r.bottom) / 2;
                  return r.width > 0 && r.left >= header.left && r.right <= header.right
                    && r.right <= innerWidth && r.top >= header.top && r.bottom <= header.bottom
                    && element.contains(document.elementFromPoint(x, y));
                }"""
            ), selector
        frame.locator("#openPip").click(trial=True)
        frame.locator("#refresh").click(trial=True)
    finally:
        page.set_viewport_size({"width": 1280, "height": 900})


@pytest.mark.parametrize(
    "delivery",
    [
        "handshake",
        "globals",
        "initial",
        "initial-rejected",
        "notification",
        "rejected",
        "unsupported",
    ],
)
def test_viewer_receives_initial_data_from_host(page: Page, delivery: str) -> None:
    """Real hosts may wait for initialization or inject globals after page startup."""
    page.set_content('<iframe id="viewer" sandbox="allow-scripts"></iframe>')
    page.evaluate(
        """({delivery, payload}) => {
          window.startupMessages = [];
          const viewer = document.getElementById('viewer');
          const send = message => viewer.contentWindow.postMessage(message, '*');
          const notify = () => send({jsonrpc: '2.0',
            method: 'ui/notifications/tool-result', params: {structuredContent: payload}});
          window.addEventListener('message', event => {
            if (event.source !== viewer.contentWindow) return;
            const message = event.data;
            window.startupMessages.push(message);
            if (message.method === 'ui/initialize') {
              if (delivery === 'rejected' || delivery === 'initial-rejected') {
                send({jsonrpc: '2.0', id: message.id,
                  error: {code: -32601, message: 'Unsupported initialization'}});
                return;
              }
              send({jsonrpc: '2.0', id: message.id, result: {
                protocolVersion: delivery === 'unsupported' ? 'unknown' : '2026-01-26',
                hostInfo: {name: 'test-host', version: '1'},
                hostCapabilities: {}, hostContext: {theme: 'dark'}}});
            } else if (message.method === 'ui/notifications/initialized') {
              if (delivery === 'handshake') notify();
            } else if (message.method === 'tools/call') {
              send({jsonrpc: '2.0', id: message.id, result: {structuredContent: {}}});
            }
          });
          if (delivery === 'notification') viewer.addEventListener('load', notify);
        }""",
        {"delivery": delivery, "payload": demo_trajectories()["session-alpha"]},
    )
    html = ui_html()
    if delivery in {"initial", "initial-rejected"}:
        payload = json.dumps(demo_trajectories()["session-alpha"]).replace("<", "\\u003c")
        html = html.replace(
            "<head>", f"<head><script>window.openai={{toolOutput:{payload}}}</script>"
        )
    page.locator("iframe").evaluate("(el, html) => el.srcdoc = html", html)
    frame = page.frame_locator("iframe")
    if delivery in {"rejected", "unsupported"}:
        expect(frame.get_by_role("alert")).to_be_visible()
        assert "ui/notifications/initialized" not in page.evaluate(
            "startupMessages.map(message => message.method)"
        )
        # A legacy host can still deliver a result after rejecting the shared handshake.
        page.evaluate(
            """payload => document.querySelector('iframe').contentWindow.postMessage({
              jsonrpc: '2.0', method: 'ui/notifications/tool-result',
              params: {structuredContent: payload}}, '*')""",
            demo_trajectories()["session-alpha"],
        )
    if delivery == "globals":
        expect(frame.locator(".loading")).to_be_visible()
        frame.locator("#app").evaluate(
            """(el, payload) => window.dispatchEvent(new CustomEvent('openai:set_globals',
              {detail: {globals: {toolOutput: payload}}}))""",
            demo_trajectories()["session-alpha"],
        )
    expect(frame.locator("#sessionSelect")).to_be_visible(timeout=3000)
    expect(frame.locator(".loading")).to_have_count(0)
    expect(frame.get_by_role("alert")).to_have_count(0)
    if delivery == "handshake":
        messages = page.evaluate("startupMessages")
        methods = [message.get("method") for message in messages]
        assert methods[:2] == ["ui/initialize", "ui/notifications/initialized"]
        assert methods.count("ui/initialize") == 1
        assert messages[0]["params"]["protocolVersion"] == "2026-01-26"
        assert messages[0]["params"]["appCapabilities"] == {}


def test_viewer_initialization_timeout_recovers_when_data_arrives(page: Page) -> None:
    with page.context.new_page() as isolated:
        isolated.clock.install()
        isolated.set_content('<iframe sandbox="allow-scripts"></iframe>')
        isolated.locator("iframe").evaluate("(el, html) => el.srcdoc = html", ui_html())
        frame = isolated.frame_locator("iframe")
        expect(frame.locator(".loading")).to_be_visible()
        isolated.clock.fast_forward(60_001)
        expect(frame.get_by_role("alert")).to_be_visible()
        isolated.evaluate(
            """payload => document.querySelector('iframe').contentWindow.postMessage({
              jsonrpc: '2.0', method: 'ui/notifications/tool-result',
              params: {structuredContent: payload}}, '*')""",
            demo_trajectories()["session-alpha"],
        )
        expect(frame.locator("#sessionSelect")).to_be_visible()
        expect(frame.get_by_role("alert")).to_have_count(0)


@pytest.fixture
def timing_trajectory(tmp_path: Path) -> dict[str, Any]:
    """Project a synthetic log through the backend before checking its UI."""
    events: list[dict[str, Any]] = [
        {"timestamp": 1, "type": "session_meta", "payload": {"id": "session-alpha"}},
        {
            "timestamp": 1,
            "type": "event_msg",
            "payload": {"type": "turn_started", "turn_id": "timing"},
        },
    ]
    for index, duration in enumerate(
        [
            {"secs": 0, "nanos": 9_375},
            {"secs": 0, "nanos": 1_000_000},
            {"secs": 0, "nanos": 0},
            {"secs": 1, "nanos": 250_000_000},
        ],
        1,
    ):
        events.append(
            {
                "timestamp": 1 + index,
                "type": "event_msg",
                "payload": {
                    "type": "mcp_tool_call_end",
                    "call_id": f"call-{index}",
                    "invocation": {"tool": "timing-probe"},
                    "duration": duration,
                    "result": {"Ok": {"content": []}},
                },
            }
        )
    events.extend(
        [
            {
                "timestamp": 6,
                "type": "response_item",
                "payload": {"type": "function_call_output", "call_id": "orphan", "output": "ok"},
            },
            {
                "timestamp": 7,
                "type": "response_item",
                "payload": {"type": "web_search_call", "id": "point", "status": "completed"},
            },
            {
                "timestamp": 8,
                "type": "response_item",
                "payload": {"type": "reasoning", "id": "reasoning", "summary": []},
            },
            {
                "timestamp": 9,
                "type": "event_msg",
                "payload": {"type": "turn_complete", "turn_id": "timing"},
            },
        ]
    )
    return parse_session(write_rollout(tmp_path / "timing.jsonl", events))


@pytest.fixture
def reasoning_trajectory(tmp_path: Path) -> dict[str, Any]:
    """Keep activity evidence, unavailable summaries, and private fields distinct."""
    events: list[dict[str, Any]] = [
        {"timestamp": 1, "type": "session_meta", "payload": {"id": "session-alpha"}},
        {
            "timestamp": 2,
            "type": "event_msg",
            "payload": {"type": "turn_started", "turn_id": "explain-1"},
        },
        {
            "timestamp": 3,
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Explain the recorded activities"},
        },
    ]

    def add_reasoning(record_id: str, summary: str) -> None:
        events.append(
            {
                "timestamp": len(events) + 1,
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "id": record_id,
                    "summary": [{"type": "summary_text", "text": summary}] if summary else [],
                    "encrypted_content": "private-encrypted-reasoning",
                },
            }
        )

    add_reasoning(
        "docs", "**Reading `README.md` and `AGENTS.md`** Check the interface conventions."
    )
    events.extend(
        [
            {
                "timestamp": 5,
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "call_id": "read-docs",
                    "name": "exec",
                    "arguments": '{"cmd":"private-tool-input"}',
                },
            },
            {
                "timestamp": 6,
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "read-docs",
                    "output": "private-tool-output",
                },
            },
        ]
    )
    add_reasoning("feedback", "Reviewing the command output to see what still needs checking.")
    add_reasoning("headed", "**An ambiguous topic** We might run tests or edit code later.")
    add_reasoning("negated", "Do not run tests yet; more information is needed.")
    add_reasoning("unknown", "An observation without a described activity.")
    add_reasoning("missing", "")
    add_reasoning(
        "long",
        "Reading documentation to confirm the expected behavior. "
        + "A longer recorded summary sentence. " * 10
        + '\n\ndetail-beyond-excerpt <img src=x onerror="window.__trajectoryXss=true">',
    )
    events.extend(
        [
            {
                "timestamp": 15,
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "The review is complete."}],
                },
            },
            {
                "timestamp": 16,
                "type": "event_msg",
                "payload": {"type": "turn_complete", "turn_id": "explain-1"},
            },
            {
                "timestamp": 17,
                "type": "event_msg",
                "payload": {"type": "turn_started", "turn_id": "explain-2"},
            },
        ]
    )
    add_reasoning("next-turn", "Plan the next inspection.")
    return parse_session(write_rollout(tmp_path / "reasoning.jsonl", events), detail_level="full")


@pytest.mark.parametrize("language", ["en", "zh"])
def test_reasoning_activity_explanations_keep_originals_and_navigable_context(
    page: Page, harness_url: str, reasoning_trajectory: dict[str, Any], language: str
) -> None:
    page.goto(f"{harness_url}/{language}-native")
    frame = viewer(page)
    expect(frame.locator("#refresh")).to_be_visible()
    page.evaluate("payload => { trajectories['session-alpha'] = payload; }", reasoning_trajectory)
    frame.locator("#refresh").click()
    frame.locator(".turn-toggle").first.click()
    docs = frame.locator('tr[data-id="docs"]')
    expect(docs.locator(".event-name")).to_have_text(
        "分析与决策" if language == "zh" else "Analysis & decision"
    )
    assert ("核对文档" if language == "zh" else "Checking documentation") in docs.locator(
        ".summary"
    ).inner_text()
    expect(docs.locator(".summary")).to_contain_text("README.md")
    expect(docs.locator(".summary")).to_contain_text("AGENTS.md")
    expect(docs.locator(".reasoning-preview")).to_have_text(
        "Reading `README.md` and `AGENTS.md` Check the interface conventions."
    )
    assert (
        docs.locator(".reasoning-preview").evaluate(
            "element => getComputedStyle(element).webkitLineClamp"
        )
        == "none"
    )
    docs.locator(".summary").hover()
    expect(frame.locator("#ledgerTooltip")).to_contain_text("Reading `README.md` and `AGENTS.md`")
    docs.click()
    expect(frame.locator(".reasoning-explanation")).to_have_attribute("data-activity", "docs")
    expect(frame.locator(".reasoning-source-text")).to_have_text(
        "**Reading `README.md` and `AGENTS.md`** Check the interface conventions."
    )
    context = frame.locator(".reasoning-context")
    expect(context.locator("button")).to_have_count(2)
    assert "private-tool-input" not in context.inner_text()
    assert "private-tool-output" not in context.inner_text()
    assert "private-encrypted-reasoning" not in frame.locator("body").inner_text()
    context.locator("button").last.click()
    expect(frame.locator("#inspector h2")).to_contain_text("exec")
    frame.locator('tr[data-id="feedback"]').click()
    expect(frame.locator(".reasoning-explanation")).to_have_attribute("data-activity", "results")
    frame.locator("#search").fill("核对文档" if language == "zh" else "Checking documentation")
    expect(frame.locator('tr[data-id="docs"]')).to_be_visible()
    expect(frame.locator('tr[data-id="feedback"]')).to_have_count(0)
    frame.locator("#search").fill("")
    # An event in a different turn cannot serve as the explanation's context.
    frame.locator('tr[data-id="next-turn"]').click()
    expect(frame.locator(".reasoning-explanation")).to_have_attribute("data-activity", "plan")
    expect(frame.locator(".reasoning-context")).to_have_count(0)


def test_reasoning_explanations_do_not_guess_from_negation_headings_or_missing_records(
    page: Page, harness_url: str, reasoning_trajectory: dict[str, Any]
) -> None:
    page.goto(f"{harness_url}/zh")
    frame = viewer(page)
    expect(frame.locator("#refresh")).to_be_visible()
    page.evaluate("payload => { trajectories['session-alpha'] = payload; }", reasoning_trajectory)
    frame.locator("#refresh").click()
    frame.locator(".turn-toggle").first.click()
    for record_id in ["headed", "negated", "unknown"]:
        row = frame.locator(f'tr[data-id="{record_id}"]')
        assert "无法可靠概括" not in row.locator(".summary").inner_text()
        row.click()
        expect(frame.locator(".reasoning-explanation")).to_have_attribute(
            "data-activity", "unknown"
        )
        expect(frame.locator(".reasoning-explanation")).to_contain_text("无法可靠概括")
    frame.locator('tr[data-id="missing"]').click()
    expect(frame.locator(".reasoning-explanation")).to_have_attribute("data-activity", "missing")
    expect(frame.locator(".reasoning-explanation")).to_contain_text("无法确认具体")
    expect(frame.locator(".reasoning-source")).to_have_count(0)
    # Omitted records must not be silently treated as directly adjacent events.
    page.evaluate(
        """trajectories['session-alpha'].records = trajectories['session-alpha'].records
          .filter(record => record.kind !== 'tool');"""
    )
    frame.locator("#refresh").click()
    frame.locator('tr[data-id="feedback"]').click()
    assert "此前记录" not in frame.locator(".reasoning-context").inner_text()


@pytest.mark.parametrize("language", ["en", "zh"])
def test_missing_reasoning_shows_safe_context_without_crossing_turns_or_page_gaps(
    page: Page, harness_url: str, reasoning_trajectory: dict[str, Any], language: str
) -> None:
    page.goto(f"{harness_url}/{language}-native")
    frame = viewer(page)
    expect(frame.locator("#refresh")).to_be_visible()
    # Another turn has no readable summary or neighboring recorded events.
    last = reasoning_trajectory["records"][-1]
    last.update(summary="Reasoning", output=None)
    page.evaluate("payload => { trajectories['session-alpha'] = payload; }", reasoning_trajectory)
    frame.locator("#refresh").click()
    frame.locator(".turn-toggle").first.click()
    missing = frame.locator('tr[data-id="missing"] .summary')
    before = "此前记录" if language == "zh" else "Previous record"
    after = "随后记录" if language == "zh" else "Following record"
    expect(missing).to_contain_text(before)
    expect(missing).to_contain_text(after)
    expect(missing).to_contain_text("exec")
    expect(missing).to_contain_text("The review is complete.")
    missing_notice = "没有保存" if language == "zh" else "No readable"
    assert missing_notice not in missing.inner_text()
    assert "未保存可读分析摘要" not in missing.inner_text()
    missing.hover()
    expect(frame.locator("#ledgerTooltip")).to_contain_text(missing_notice)
    expect(frame.locator("#ledgerTooltip")).to_contain_text(
        "不能据此还原" if language == "zh" else "cannot reconstruct"
    )
    assert "private-tool-input" not in missing.inner_text()
    assert "private-tool-output" not in missing.inner_text()
    isolated = frame.locator('tr[data-id="next-turn"]')
    expect(isolated.locator(".summary")).to_have_text("—")
    assert before not in isolated.locator(".summary").inner_text()
    assert "The review is complete." not in isolated.locator(".summary").inner_text()
    isolated.click()
    expect(frame.locator("#inspector")).to_contain_text(
        "没有同轮次" if language == "zh" else "No neighboring events"
    )
    page.evaluate(
        """trajectories['session-alpha'].records = trajectories['session-alpha'].records
          .filter(record => !['tool', 'assistant'].includes(record.kind));"""
    )
    frame.locator("#refresh").click()
    missing = frame.locator('tr[data-id="missing"] .summary')
    expect(missing).not_to_contain_text(before)
    expect(missing).not_to_contain_text(after)
    expect(missing).to_have_text("—")
    missing.click()
    expect(frame.locator(".reasoning-context")).to_have_count(0)
    expect(frame.locator("#inspector")).to_contain_text(
        "没有同轮次" if language == "zh" else "No neighboring events"
    )


@pytest.mark.parametrize("language", ["en", "zh"])
def test_long_reasoning_summaries_require_full_details_and_stay_out_of_live_view(
    page: Page, harness_url: str, reasoning_trajectory: dict[str, Any], language: str
) -> None:
    page.goto(f"{harness_url}/{language}-native")
    frame = viewer(page)
    expect(frame.locator("#refresh")).to_be_visible()
    page.evaluate("payload => { trajectories['session-alpha'] = payload; }", reasoning_trajectory)
    frame.locator("#refresh").click()
    frame.locator(".turn-toggle").first.click()
    frame.locator('tr[data-id="long"]').click()
    assert "detail-beyond-excerpt" not in frame.locator("#inspector").inner_text()
    expect(frame.locator(".reasoning-source-text")).to_contain_text("…")
    frame.locator("#loadFull").click()
    frame.locator("#confirmFull").click()
    expect(frame.locator(".reasoning-source-text")).to_contain_text("detail-beyond-excerpt")
    expect(frame.locator('tr[data-id="long"] .reasoning-preview')).to_contain_text(
        "detail-beyond-excerpt"
    )
    # Longer summaries remain plain text, including their original paragraph breaks.
    expect(frame.locator(".reasoning-source pre")).to_have_count(0)
    full_source = next(
        record["output"] for record in reasoning_trajectory["records"] if record["id"] == "long"
    )
    assert frame.locator(".reasoning-source-text").inner_text() == full_source
    assert "private-tool-output" not in frame.locator(".reasoning-source-text").inner_text()
    expect(frame.locator("#inspector img")).to_have_count(0)
    assert frame.locator("body").evaluate("element => window.__trajectoryXss") is None
    assert "private-encrypted-reasoning" not in frame.locator("#inspector").inner_text()
    frame.locator("#openPip").click()
    expect(frame.locator("#liveDock")).to_be_visible()
    assert "detail-beyond-excerpt" not in frame.locator("#liveDock").inner_text()
    expect(frame.locator('.dock-record[data-index="2"] .dock-record-summary')).to_contain_text(
        "核对文档" if language == "zh" else "Checking documentation"
    )


def test_missing_reasoning_live_context_uses_only_its_safe_tail(
    page: Page, harness_url: str, reasoning_trajectory: dict[str, Any]
) -> None:
    page.goto(f"{harness_url}/en-native")
    frame = viewer(page)
    expect(frame.locator("#refresh")).to_be_visible()
    user = dict(reasoning_trajectory["records"][0], summary="outside-live-window")
    missing = next(
        record for record in reasoning_trajectory["records"] if record["id"] == "missing"
    )
    reasoning_trajectory["records"] = [user] + [
        dict(missing, index=index, id=f"missing-{index}") for index in range(2, 57)
    ]
    reasoning_trajectory["stats"].update(records=56, visibleRecords=56)
    reasoning_trajectory["turns"] = reasoning_trajectory["turns"][:1]
    page.evaluate("payload => { trajectories['session-alpha'] = payload; }", reasoning_trajectory)
    frame.locator("#refresh").click()
    expect(frame.locator('tr[data-id="missing-56"] .summary')).to_contain_text(
        "outside-live-window"
    )
    frame.locator("#openPip").click()
    expect(frame.locator("#liveDock")).to_be_visible()
    expect(frame.locator(".dock-record")).to_have_count(50)
    assert "outside-live-window" not in frame.locator("#liveDock").inner_html()
    expect(frame.locator(".dock-record-summary").last).to_have_text("—")
    assert "No readable analysis summary was saved." not in frame.locator("#liveDock").inner_text()
    expect(frame.locator(".dock-record-summary").last).to_have_attribute(
        "title", re.compile("No readable summary was saved")
    )


@pytest.fixture
def command_trajectory(tmp_path: Path) -> dict[str, Any]:
    """Project commands with distinct purposes, outcomes, and sensitive details."""
    events: list[dict[str, Any]] = [
        {"timestamp": 1, "type": "session_meta", "payload": {"id": "session-alpha"}},
        {
            "timestamp": 2,
            "type": "event_msg",
            "payload": {"type": "turn_started", "turn_id": "commands"},
        },
    ]
    commands = [
        ("read", ["cat", "/private/source.md"], "completed", 0),
        ("tests", ["uv", "run", "pytest", "private-selection"], "completed", 0),
        ("nonzero", ["pytest", "private-selection"], "completed", 1),
        ("error", ["pytest", "private-selection"], "failed", 2),
        ("unknown", ["zsh", "-lc", "cat $(private-program)"], "completed", 0),
    ]
    for offset, (record_id, command, status, exit_code) in enumerate(commands, 3):
        events.append(
            {
                "timestamp": offset,
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "turn_id": "commands",
                    "item": {
                        "type": "CommandExecution",
                        "id": record_id,
                        "command": command,
                        "cwd": "/private/project",
                        "status": status,
                        "exit_code": exit_code,
                        "stdout": (
                            'private-output <img src=x onerror="window.__trajectoryXss=true">'
                        ),
                    },
                },
            }
        )
    return parse_session(write_rollout(tmp_path / "commands.jsonl", events), detail_level="full")


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("native", [False, True])
def test_command_content_explains_activity_separately_from_status_and_exit_code(
    page: Page,
    harness_url: str,
    command_trajectory: dict[str, Any],
    language: str,
    native: bool,
) -> None:
    page.goto(f"{harness_url}/{language}-native" if native else f"{harness_url}/{language}")
    frame = viewer(page)
    expect(frame.locator("#refresh")).to_be_visible()
    page.evaluate("payload => { trajectories['session-alpha'] = payload; }", command_trajectory)
    frame.locator("#refresh").click()
    read = frame.locator('tr[data-id="read"]')
    expect(read.locator(".event-name")).to_have_text("命令执行" if language == "zh" else "Command")
    expect(read.locator(".command-state")).to_have_text("已结束" if language == "zh" else "Ended")
    expect(read.locator(".summary")).to_contain_text(
        "读取文件" if language == "zh" else "Read files"
    )
    read.click()
    expect(frame.locator(".command-explanation")).to_have_attribute("data-recognized", "true")
    expect(frame.locator(".command-exit strong")).to_have_text("0")
    assert "private" not in frame.locator("body").inner_text()
    read.locator(".summary").hover()
    expect(frame.locator("#ledgerTooltip")).to_contain_text(
        "退出码: 0" if language == "zh" else "Exit code: 0"
    )
    frame.locator('tr[data-id="nonzero"]').click()
    expect(frame.locator(".inspect-meta")).to_contain_text(
        "已结束" if language == "zh" else "Ended"
    )
    expect(frame.locator(".command-exit strong")).to_have_text("1")
    expect(frame.locator(".command-note").last).to_contain_text(
        "是否达到预期" if language == "zh" else "intended result"
    )
    frame.locator('tr[data-id="error"]').click()
    expect(frame.locator(".inspect-meta")).to_contain_text(
        "执行失败" if language == "zh" else "Failed"
    )
    expect(frame.locator(".command-exit strong")).to_have_text("2")
    frame.locator('tr[data-id="unknown"]').click()
    expect(frame.locator(".command-explanation")).to_have_attribute("data-recognized", "false")
    expect(frame.locator(".command-explanation")).to_contain_text(
        "无法确定" if language == "zh" else "cannot be determined"
    )
    frame.locator("#search").fill("运行测试" if language == "zh" else "Run tests")
    expect(frame.locator('tr[data-id="tests"]')).to_be_visible()
    expect(frame.locator('tr[data-id="read"]')).to_have_count(0)


@pytest.mark.parametrize("language", ["en", "zh"])
def test_command_full_details_and_live_summaries_keep_their_privacy_boundary(
    page: Page, harness_url: str, command_trajectory: dict[str, Any], language: str
) -> None:
    page.goto(f"{harness_url}/{language}-native")
    frame = viewer(page)
    expect(frame.locator("#refresh")).to_be_visible()
    # Already-projected generic summaries also get a useful, explicit fallback.
    command_trajectory["records"][-1]["summary"] = "Command · complete"
    page.evaluate("payload => { trajectories['session-alpha'] = payload; }", command_trajectory)
    frame.locator("#refresh").click()
    frame.locator('tr[data-id="unknown"]').click()
    expect(frame.locator(".command-explanation")).to_have_attribute("data-recognized", "false")
    expect(frame.locator(".command-exit")).to_have_count(0)
    assert "private" not in frame.locator("body").inner_text()
    frame.locator('tr[data-id="read"]').click()
    frame.locator("#loadFull").click()
    frame.locator("#confirmFull").click()
    expect(frame.locator("#inspector pre").first).to_contain_text("/private/source.md")
    expect(frame.locator("#inspector img")).to_have_count(0)
    frame.locator("#openPip").click()
    expect(frame.locator("#liveDock")).to_be_visible()
    expect(frame.locator('.dock-record[data-index="1"] .dock-record-summary')).to_contain_text(
        "读取文件" if language == "zh" else "Read files"
    )
    expect(frame.locator('.dock-record[data-index="1"] .dock-record-state')).to_contain_text(
        "已结束" if language == "zh" else "Ended"
    )
    assert "private" not in frame.locator("#liveDock").inner_text()
    assert "private" not in frame.locator("#liveDock").inner_html()


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("legacy_zero", [False, True])
def test_recorded_timing_precision_in_ledger_inspector_and_live_view(
    page: Page,
    harness_url: str,
    timing_trajectory: dict[str, Any],
    language: str,
    native: bool,
    legacy_zero: bool,
) -> None:
    route = f"{language}-native" if native else f"{language}-dock"
    page.goto(f"{harness_url}/{route}")
    frame = viewer(page)
    expect(frame.locator("#refresh")).to_be_visible()
    if legacy_zero:
        # Older hosts may still send zero rather than null for these records.
        timing_trajectory["records"][0]["durationMs"] = 0
        timing_trajectory["records"][2]["durationMs"] = 0
    page.evaluate("payload => { trajectories['session-alpha'] = payload; }", timing_trajectory)
    frame.locator("#refresh").click()
    cells = frame.locator("tr.record td.duration")
    expected = ["-", "1 ms", "-", "1.25 s", "-", "-", "-"]
    expect(cells).to_have_text(expected)
    note = cells.first.get_attribute("title")
    assert note and ("日志记录" in note if language == "zh" else "Recorded" in note)
    frame.locator('tr.record[data-index="1"]').click()
    expect(frame.locator("#inspector .inspect-item").first.locator("strong")).to_have_text("-")
    frame.locator("#openPip").click()
    expect(frame.locator("#liveDock")).to_be_visible()
    expect(frame.locator(".dock-record-state")).to_have_text(
        [f"complete · {value}" for value in expected]
    )


@pytest.mark.parametrize("missing", ["all", "partial", "zero"])
def test_missing_turn_durations_do_not_fabricate_an_overall_duration(
    page: Page, harness_url: str, missing: str
) -> None:
    page.goto(f"{harness_url}/en")
    frame = viewer(page)
    expect(frame.locator("#refresh")).to_be_visible()
    page.evaluate(
        """missing => {
          trajectories['session-alpha'].turns.forEach((turn, index) => {
            if (missing === 'zero' && index === 0) turn.durationMs = 0;
            else if (missing === 'all' || index === 0) turn.durationMs = null;
          });
        }""",
        missing,
    )
    frame.locator("#refresh").click()
    expect(frame.locator(".stat").filter(has_text="Duration").locator(".stat-value")).to_have_text(
        "-"
    )


@pytest.mark.parametrize("width", [400, 600])
def test_narrow_view_keeps_controls_within_viewport(
    page: Page, harness_url: str, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    try:
        page.goto(f"{harness_url}/en")
        frame = viewer(page)
        expect(frame.locator("#refresh")).to_be_visible()
        for selector in ("#refresh", "#openPip", "#sessionSelect", "#loadFull"):
            assert frame.locator(selector).evaluate(
                "element => { const r=element.getBoundingClientRect(); "
                "return r.left >= 0 && r.right <= innerWidth; }"
            )
        timeline = frame.locator("#timeline")
        timeline.focus()
        before = frame.locator("#tickEnd").inner_text()
        timeline.press("+")
        assert frame.locator("#tickEnd").inner_text() != before
        timeline.press("Escape")
        expect(frame.locator("#tickEnd")).to_have_text(before)
    finally:
        page.set_viewport_size({"width": 1280, "height": 900})


def test_domain_and_history_merge_stay_bounded_for_large_sessions(page: Page) -> None:
    # Exercise the actual private projection helpers with a test-only seam;
    # avoid materializing 160,000 DOM elements merely to test the domain scan.
    script = ui_html().split("<script>", 1)[1].split("</script>", 1)[0]
    script = script.replace(
        "      function computeDomain() {",
        "      window.__audit = {computeDomain, mergeEarlierPage, "
        "setData: value => {data=value}};\n      function computeDomain() {",
    )
    page.goto("about:blank")
    page.set_content('<div id="app"></div>')
    page.evaluate(script)
    result = page.evaluate("""() => {
      const records = Array.from({length:160000}, (_, i) => ({index:i+1,turn:1,
        startedAt:new Date(i*1000).toISOString(),
        completedAt:new Date(i*1000+100).toISOString()}));
      __audit.setData({records});
      const domain = __audit.computeDomain();
      const base = {session:{id:'audit'},detailLevel:'summary',stats:{records:160000,turns:1},
        turns:[{index:1}],warnings:[]};
      const merged = __audit.mergeEarlierPage({...base,records:records.slice(1000,6000)},
        {...base,records:records.slice(0,1000)});
      return {domain, count:merged.records.length, first:merged.records[0].index,
        last:merged.records.at(-1).index, hasLater:merged.pagination.hasLater};
    }""")
    assert result == {
        "domain": {"start": 0, "end": 159999100},
        "count": 5000,
        "first": 1,
        "last": 5000,
        "hasLater": True,
    }


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
        == "#f4f6f9"
    )

    root.evaluate("() => window.__setOpenAITheme('dark')")
    expect(root).to_have_attribute("data-codex-theme", "dark")


def test_recent_sessions_load_after_the_initial_trajectory(page: Page, harness_url: str) -> None:
    page.goto(f"{harness_url}/en")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()

    page.wait_for_function("window.__trajectoryToolNames.includes('list_codex_sessions')")

    expect(frame.locator("#sessionSelect option")).to_have_count(9)
    assert (
        page.evaluate(
            "window.__trajectoryToolNames.filter(name => name === 'list_codex_sessions').length"
        )
        == 1
    )


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
    assert "Estimated cost ≈$0.000832" in turn_toggles.first.inner_text()
    assert "Estimated cost ≈$0.000656" in turn_toggles.last.inner_text()
    turn_groups = frame.locator("tbody.turn-group")
    assert turn_groups.count() == 2
    assert turn_groups.first.locator(".turn-token-label").all_inner_texts() == [
        "Uncached input",
        "Cache reads",
        "Output",
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
        "cells => cells.every(cell => cell.scrollWidth <= cell.clientWidth + 1)"
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
    assert (
        token_panel.locator('[data-token-metric="cost"] .token-metric-value').inner_text()
        == "≈$0.001488"
    )
    assert (
        frame.locator(".stat").filter(has_text="Estimated cost").locator(".stat-value").inner_text()
        == "≈$0.001488"
    )
    assert token_panel.locator(".token-metric[title]").count() == 0
    assert frame.locator(".stat[title]").count() == 0
    assert "cache is part of input and reasoning is part of output" in token_panel.inner_text()
    assert "Cache hit 75%" in token_panel.locator(".token-badges").inner_text()
    assert "Pricing coverage Complete" in token_panel.locator(".token-badges").inner_text()
    assert "not a Codex subscription bill" in token_panel.locator(".cost-note").inner_text()
    token_turns = token_panel.locator("details.token-turns")
    token_table = token_panel.locator('[role="table"]')
    expect(token_table).to_have_attribute("aria-labelledby", "tokenTurnsSummary")
    assert token_table.locator('[role="rowgroup"]').count() == 2
    assert token_table.locator('[role="columnheader"]').count() == 8
    assert token_turns.evaluate("element => element.open") is False
    assert token_panel.locator(".token-turn-row").count() == 0
    token_turns.locator("summary").click()
    token_panel.locator(".token-turn-row").first.wait_for()
    assert token_panel.locator(".token-turn-row").count() == 2
    assert token_panel.locator(".token-turn-row").first.is_visible()
    assert token_panel.locator(".token-turn-row").first.locator(".token-cell").count() == 8
    assert token_panel.locator(".token-turn-row").first.get_by_role("cell").count() == 8
    assert token_panel.locator('.token-turn-row [data-token-column="cost"]').all_inner_texts() == [
        "≈$0.000832",
        "≈$0.000656",
    ]
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
    assert turn_groups.first.locator(".turn-column-row th").all_inner_texts() == [
        "INDEX",
        "STEP",
        "EVENT",
        "CONTENT",
        "TIME",
    ]
    event_header = frame.locator(".turn-column-row th").nth(2)
    content_header = frame.locator(".turn-column-row th").nth(3)
    event_width = event_header.evaluate("element => element.getBoundingClientRect().width")
    content_width = content_header.evaluate("element => element.getBoundingClientRect().width")
    assert content_width > event_width
    assert frame.locator(".ledger-wrap").evaluate(
        "element => element.scrollWidth === element.clientWidth"
    )
    long_event = frame.locator('tr[data-id="record-2-7"] .event-name')
    assert long_event.evaluate("element => element.scrollWidth <= element.clientWidth + 1")
    long_event.hover()
    assert tooltip.is_visible()
    assert tooltip.inner_text() == "Subagent activity from the long-running reviewer worker"
    long_content = frame.locator('tr[data-id="record-2-7"] .summary')
    assert long_content.evaluate("element => element.scrollWidth <= element.clientWidth + 1")
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
    assert frame.locator(".record-token").count() == 0
    assert frame.locator('tr[data-id="record-1-4"] td').count() == 5
    assert frame.locator('tr[data-id="record-1-3"] td').count() == 5
    detail_labels = frame.locator("details summary").all_inner_texts()
    assert "Input" not in detail_labels
    assert "Output" not in detail_labels
    assert "Metadata" not in detail_labels
    assert "ESTIMATED COST" not in frame.locator("#inspector").inner_text()
    assert "Token usage" not in frame.locator("#inspector").inner_text()

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


def test_partial_and_unavailable_cost_coverage_are_explicit(page: Page, harness_url: str) -> None:
    page.goto(f"{harness_url}/en")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()

    frame.locator("#sessionSelect").select_option("session-partial-cost")
    frame.get_by_role("heading", name="Inspect mixed-model pricing").wait_for()
    token_panel = frame.locator("#tokenDetails")
    partial_badge = token_panel.locator(".token-badge.partial")
    expect(partial_badge).to_have_text("Pricing coverage Partial")
    partial_value = token_panel.locator(
        '[data-token-metric="cost"] .token-metric-value'
    ).inner_text()
    assert partial_value.startswith("≥$")
    assert partial_value != "—"
    assert partial_value in frame.locator(".turn-toggle").inner_text()
    assert "ESTIMATED COST" not in frame.locator("#inspector").inner_text()
    token_panel.locator("details.token-turns summary").click()
    expect(token_panel.locator('[data-token-column="cost"]').last).to_have_text(partial_value)

    frame.locator("#sessionSelect").select_option("session-unavailable-cost")
    frame.get_by_role("heading", name="Inspect unavailable pricing").wait_for()
    token_panel = frame.locator("#tokenDetails")
    expect(token_panel.locator(".token-badge.unavailable")).to_have_text(
        "Pricing coverage Unavailable"
    )
    expect(token_panel.locator('[data-token-metric="cost"] .token-metric-value')).to_have_text("—")
    expect(
        frame.locator(".stat").filter(has_text="Estimated cost").locator(".stat-value")
    ).to_have_text("—")
    assert "Estimated cost —" in frame.locator(".turn-toggle").inner_text()
    assert "ESTIMATED COST" not in frame.locator("#inspector").inner_text()


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
    frame.locator("#rememberFull").uncheck()
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
    assert payload["costs"] == {
        "total": "≈$0.001488",
        "turn": "≈$0.000656",
    }
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

    page.reload()
    frame.get_by_text("Safe summary", exact=True).wait_for()
    assert "set_codex_trajectory_preferences" not in page.evaluate("window.__trajectoryToolNames")


@pytest.mark.parametrize("route", ["en", "en-native", "en-dock"])
def test_full_detail_default_survives_task_switch_and_reopening(
    page: Page, harness_url: str, route: str
) -> None:
    page.goto(f"{harness_url}/{route}?preferencesKey=full-default-{route}")
    frame = viewer(page)
    frame.locator("#loadFull").click()
    expect(
        frame.get_by_label("Default to full details for all tasks on this device")
    ).to_be_checked()
    frame.locator("#confirmFull").click()
    expect(frame.locator(".privacy-copy")).to_contain_text("Remembered:")
    frame.get_by_text("Full details", exact=True).wait_for()

    names = page.evaluate("window.__trajectoryToolNames")
    calls = page.evaluate("window.__trajectoryCalls")
    assert calls[names.index("set_codex_trajectory_preferences")] == {"defaultFullDetails": True}
    frame.locator("#sessionSelect").select_option("session-beta")
    expect(frame.locator("#sessionSelect")).to_have_value("session-beta")
    frame.get_by_text("Full details", exact=True).wait_for()

    # Recreate the sandboxed iframe; browser storage in it is unavailable.
    page.reload()
    frame.get_by_text("Full details", exact=True).wait_for()
    assert page.locator("#viewer").get_attribute("sandbox") == "allow-scripts"
    assert frame.locator("html").evaluate(
        "() => { try { window.localStorage; return false; } catch { return true; } }"
    )
    assert "set_codex_trajectory_preferences" not in page.evaluate("window.__trajectoryToolNames")
    assert not any(
        call.get("detailLevel") == "full" for call in page.evaluate("window.__trajectoryCalls")
    )
    expect(frame.locator("#inspector")).to_contain_text("have not been loaded")
    frame.locator("tr.record").last.click()
    page.wait_for_function("window.__trajectoryCalls.some(call => call.detailLevel === 'full')")
    full_call = page.evaluate("window.__trajectoryCalls.find(call => call.detailLevel === 'full')")
    assert full_call["maxRecords"] == 50
    assert full_call["beforeRecord"] == 10

    frame.locator("#openPip").click()
    if route == "en":
        expect(frame.locator("#pipVideo")).to_have_attribute("data-active", "true")
        payload = frame.locator("#pipCanvas").get_attribute("data-payload") or "{}"
        assert json.loads(payload)["detailLevel"] == "summary"
        assert "uv run pytest" not in payload
        frame.locator("#openPip").click()
    else:
        expect(frame.locator("#liveDock")).to_have_attribute("data-detail-level", "summary")
        assert "uv run pytest" not in frame.locator("#liveDock").inner_text()
        frame.locator("#closeDock").click()
    frame.get_by_text("Full details", exact=True).wait_for()

    frame.get_by_role("button", name="Restore safe summary default").click()
    frame.get_by_text("Safe summary", exact=True).wait_for()
    frame.locator("#sessionSelect").select_option("session-beta")
    expect(frame.locator("#sessionSelect")).to_have_value("session-beta")
    frame.get_by_text("Safe summary", exact=True).wait_for()
    page.reload()
    frame.get_by_text("Safe summary", exact=True).wait_for()
    assert not any(
        call.get("detailLevel") == "full" for call in page.evaluate("window.__trajectoryCalls")
    )


def test_lazy_details_retry_share_pending_page_and_reset_on_refresh(
    page: Page, harness_url: str
) -> None:
    page.goto(f"{harness_url}/en-native?preferencesKey=lazy-details-retry")
    frame = viewer(page)
    frame.locator("#loadFull").click()
    frame.locator("#confirmFull").click()
    frame.get_by_text("Full details", exact=True).wait_for()
    page.reload()
    frame.get_by_text("Full details", exact=True).wait_for()
    assert not any(
        call.get("detailLevel") == "full" for call in page.evaluate("window.__trajectoryCalls")
    )

    page.evaluate("window.__failDetailReads = true")
    frame.locator('tr[data-index="9"]').click()
    expect(frame.locator(".detail-load-state")).to_have_text("Detail read failed")
    expect(frame.locator("#refresh")).to_be_enabled()
    page.evaluate("window.__failDetailReads = false; window.__fullDetailDelayMs = 800")
    frame.locator("#loadRecordDetails").click()
    frame.locator('tr[data-index="8"]').click()
    expect(frame.locator(".detail-load-state")).to_have_count(0)
    count_full_reads = "window.__trajectoryCalls.filter(call => call.detailLevel === 'full').length"
    assert page.evaluate(count_full_reads) == 2
    frame.locator('tr[data-index="9"]').click()
    assert page.evaluate(count_full_reads) == 2

    frame.locator("#refresh").click()
    expect(frame.locator(".detail-load-state")).to_contain_text("have not been loaded")
    assert page.evaluate(count_full_reads) == 2
    frame.locator('tr[data-index="9"]').click()
    page.wait_for_function(f"{count_full_reads} === 3")
    frame.locator("#sessionSelect").select_option("session-beta")
    frame.get_by_role("heading", name="Review the documentation").wait_for()
    page.wait_for_timeout(900)
    expect(frame.locator(".detail-load-state")).to_contain_text("have not been loaded")
    assert page.evaluate(count_full_reads) == 3
    assert "secret" not in frame.locator("#inspector").inner_text()


def test_history_stays_summary_after_full_detail_consent(page: Page, harness_url: str) -> None:
    page.goto(f"{harness_url}/en-native?preferencesKey=lazy-details-history")
    frame = viewer(page)
    frame.locator("#loadFull").click()
    frame.locator("#confirmFull").click()
    frame.get_by_text("Full details", exact=True).wait_for()
    page.reload()
    frame.get_by_text("Full details", exact=True).wait_for()
    frame.locator("#sessionSelect").select_option("session-paged")
    frame.locator("#loadEarlier").click()
    page.wait_for_function("window.__trajectoryCalls.some(call => call.beforeRecord)")
    assert not any(
        call.get("detailLevel") == "full" for call in page.evaluate("window.__trajectoryCalls")
    )
    assert all(
        call["detailLevel"] == "summary"
        for call in page.evaluate("window.__trajectoryCalls")
        if call.get("beforeRecord")
    )


def test_revoking_default_in_another_panel_applies_on_task_switch(
    page: Page, harness_url: str
) -> None:
    url = f"{harness_url}/en-native?preferencesKey=full-default-shared"
    page.goto(url)
    frame = viewer(page)
    frame.locator("#loadFull").click()
    frame.locator("#confirmFull").click()
    frame.get_by_text("Full details", exact=True).wait_for()
    other_page = page.context.new_page()
    try:
        other_page.goto(url)
        other_frame = viewer(other_page)
        other_frame.get_by_text("Full details", exact=True).wait_for()
        other_frame.locator("#useSummary").click()
        other_frame.get_by_text("Safe summary", exact=True).wait_for()
        frame.locator("#sessionSelect").select_option("session-beta")
        frame.get_by_text("Safe summary", exact=True).wait_for()
        assert page.evaluate("window.__trajectoryCalls.at(-1).detailLevel") == "summary"
    finally:
        other_page.close()


def test_full_detail_default_failure_is_visible_and_can_retry(page: Page, harness_url: str) -> None:
    page.goto(f"{harness_url}/en-native")
    frame = viewer(page)
    page.evaluate("window.__failPreferenceWrites = true")
    frame.locator("#loadFull").click()
    frame.locator("#confirmFull").click()
    frame.get_by_text("Full details", exact=True).wait_for()
    expect(frame.locator(".privacy-copy")).to_contain_text("Could not save the default.")
    assert "Remembered:" not in frame.locator(".privacy-copy").inner_text()
    frame.locator("#sessionSelect").select_option("session-beta")
    frame.get_by_text("Safe summary", exact=True).wait_for()

    page.evaluate("window.__failPreferenceWrites = false")
    frame.locator("#loadFull").click()
    frame.locator("#confirmFull").click()
    frame.get_by_text("Full details", exact=True).wait_for()
    expect(frame.locator(".privacy-copy")).to_contain_text("Remembered:")

    page.evaluate("window.__failPreferenceWrites = true")
    frame.locator("#useSummary").click()
    expect(frame.locator(".privacy-copy")).to_contain_text("Could not save the default.")
    frame.get_by_text("Full details", exact=True).wait_for()
    page.evaluate("window.__failPreferenceWrites = false")
    frame.locator("#useSummary").click()
    frame.get_by_text("Safe summary", exact=True).wait_for()
    assert "Could not save the default." not in frame.locator(".privacy-copy").inner_text()


def test_full_detail_default_chinese_confirmation_fits_narrow_panel(
    page: Page, harness_url: str
) -> None:
    page.set_viewport_size({"width": 400, "height": 900})
    try:
        page.goto(f"{harness_url}/zh-native?preferencesKey=full-default-zh")
        frame = viewer(page)
        frame.get_by_role("button", name="加载完整详情").click()
        expect(frame.get_by_label("以后默认加载所有任务的完整详情")).to_be_checked()
        expect(frame.locator("#fullDetailsWarning")).to_contain_text("敏感信息")
        assert frame.locator(".privacy-bar").evaluate(
            "element => element.scrollWidth <= element.clientWidth"
        )
        frame.get_by_role("button", name="继续加载").click()
        expect(frame.locator(".privacy-copy")).to_contain_text("已记住")
        page.reload()
        frame.get_by_text("完整详情", exact=True).wait_for()
        frame.get_by_role("button", name="恢复安全摘要默认值").click()
        frame.get_by_text("安全摘要", exact=True).wait_for()
    finally:
        page.set_viewport_size({"width": 1280, "height": 900})


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
    expect(frame.locator("#pipCanvas")).to_have_attribute("data-total-cost", "≈$0.001488")
    expect(frame.locator("#pipCanvas")).to_have_attribute("data-turn-cost", "≈$0.000656")
    assert frame.locator("#pipCanvas").get_attribute("data-record-cost") is None
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
    expect(frame.locator("#pipCanvas")).to_have_attribute("data-total-cost", "≈$0.001672")
    expect(frame.locator("#pipCanvas")).to_have_attribute("data-turn-cost", "≈$0.000184")
    assert frame.locator("#pipCanvas").get_attribute("data-record-cost") is None
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


@pytest.mark.parametrize("route", ["en-native", "en-dock", "en-pip-unavailable", "zh-native"])
def test_live_records_expand_and_keep_details_through_refresh(
    page: Page, harness_url: str, route: str
) -> None:
    page.goto(f"{harness_url}/{route}")
    frame = viewer(page)
    frame.locator("#openPip").click()
    stream = frame.locator("#liveRecordStream")
    expect(stream).to_be_visible()
    page.wait_for_function("window.__trajectoryToolNames.includes('get_codex_trajectory_update')")
    details = frame.locator('.dock-record[data-index="3"] .dock-record-expand')
    toggle = details.locator("summary")
    expect(details).not_to_have_attribute("open", "")
    toggle.click()
    expect(details).to_have_attribute("open", "")
    expect(details.locator(".dock-record-details")).to_contain_text("call-checks")
    expect(details.locator(".dock-record-details")).to_contain_text("1.45 s")
    expect(details.locator(".dock-record-detail-content")).to_have_text("Run the focused checks")
    expect(stream).to_have_attribute("data-follow-latest", "false")
    scroll_top = stream.evaluate("element => element.scrollTop")

    page.evaluate("window.__advanceTrajectoryLive()")
    expect(frame.locator('.dock-record[data-index="10"]')).to_be_attached(timeout=6_000)
    expect(details).to_have_attribute("open", "")
    expect(toggle).to_be_focused()
    assert abs(stream.evaluate("element => element.scrollTop") - scroll_top) <= 2
    assert '"cmd"' not in stream.inner_text()
    assert "29 passed in 0.31s" not in stream.inner_text()
    assert not page.evaluate("window.__trajectoryCalls.some(args => args.detailLevel === 'full')")

    toggle.press("Enter")
    expect(details).not_to_have_attribute("open", "")
    toggle.press("Space")
    expect(details).to_have_attribute("open", "")
    frame.locator("#closeDock").click()
    frame.locator("#openPip").click()
    expect(details).not_to_have_attribute("open", "")


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
    expect(dock).to_have_attribute("data-total-cost", "≈$0.001488")
    expect(dock).to_have_attribute("data-turn-cost", "≈$0.000656")
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
    assert frame.locator("#dockTotalCost").inner_text() == "≈$0.001488"
    assert "not a Codex subscription bill" in (
        frame.locator("#dockTotalCostMetric").get_attribute("title") or ""
    )
    assert frame.locator("#dockCurrentTurnCost").inner_text() == "≈$0.000656"
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
    turn_summaries = frame.locator(".dock-turn-summary")
    assert turn_summaries.count() == 2
    assert turn_summaries.first.get_attribute("data-turn-cost") == "≈$0.000832"
    assert turn_summaries.last.get_attribute("data-turn-cost") == "≈$0.000656"
    expect(frame.locator('.dock-record[data-index="8"] .dock-record-state')).to_have_text(
        "complete · -"
    )
    latest_record = frame.locator(".dock-record.latest")
    expect(latest_record).to_have_attribute("data-index", "9")
    assert latest_record.get_attribute("data-record-tokens") is None
    assert latest_record.get_attribute("data-record-cost") is None
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
    assert frame.locator(".dock-record-usage").count() == 0
    assert "TOTAL TOKENS" not in latest_record.inner_text()
    assert "ESTIMATED COST" not in latest_record.inner_text()
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
    expect(dock).to_have_attribute("data-total-cost", "≈$0.001672")
    expect(dock).to_have_attribute("data-turn-cost", "≈$0.000184")
    expect(dock).to_have_attribute(
        "data-quota",
        "primary:68:2026-08-14T02:00:00Z|secondary:44:2026-08-21T00:00:00Z",
    )
    expect(dock).to_have_attribute("data-record-count", "10")
    expect(frame.locator(".dock-total-value")).to_have_text("704")
    expect(frame.locator("#dockTotalCost")).to_have_text("≈$0.001672")
    expect(frame.locator("#dockCurrentTurnCost")).to_have_text("≈$0.000184")
    expect(turn_summaries).to_have_count(3)
    expect(turn_summaries.last).to_have_attribute("data-turn-cost", "≈$0.000184")
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
    assert latest_record.get_attribute("data-record-tokens") is None
    assert latest_record.get_attribute("data-record-cost") is None
    assert frame.locator(".dock-record-usage").count() == 0
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


def test_tool_error_is_reported_without_locking_controls(page: Page, harness_url: str) -> None:
    page.goto(f"{harness_url}/en")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()
    frame.locator("#sessionSelect").select_option("session-missing")

    alert = frame.get_by_role("alert")
    assert "Task disappeared" in alert.inner_text()
    assert frame.get_by_role("button", name="Refresh").is_enabled()
    expect(frame.locator("#sessionSelect")).to_have_value("session-alpha")

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


@pytest.mark.parametrize(
    ("initial_revision", "history_revision", "merges"),
    [
        (None, "current", False),
        ("current", None, False),
        ("current", "other", False),
        (None, None, True),
        ("current", "current", True),
    ],
)
def test_history_requires_matching_revisions_or_legacy_pair(
    page: Page,
    harness_url: str,
    initial_revision: str | None,
    history_revision: str | None,
    merges: bool,
) -> None:
    page.goto(f"{harness_url}/en-native")
    frame = viewer(page)
    frame.locator("#sessionSelect").select_option("session-paged")
    frame.get_by_role("heading", name="Inspect a 1,205-record task").wait_for()
    page.evaluate(
        """({initial, history}) => {
      const revision = value => value === 'current' ? currentLiveRevision()
        : value === 'other' ? 'f'.repeat(64) : null;
      window.__historyRevisionOverride = revision(history);
      notify(trajectory('session-paged', 'summary', 500), revision(initial));
    }""",
        {"initial": initial_revision, "history": history_revision},
    )
    frame.locator("#loadEarlier").click()
    if merges:
        expect(frame.locator(".pagination-bar")).to_contain_text("1,000 / 1,205")
        expect(frame.locator(".notice.error")).to_have_count(0)
    else:
        expect(frame.locator(".notice.error")).to_contain_text("Refresh")
        expect(frame.locator(".pagination-bar")).to_contain_text("500 / 1,205")
        assert frame.locator("tbody.turn-group").first.get_attribute("data-turn") == "142"


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
    assert frame.locator(".token-metric").count() == 7
    assert frame.locator('[data-token-metric="cost"] .token-metric-value').inner_text() == (
        "≈$23,593.27"
    )
    assert (
        frame.locator('[data-token-metric="total"] .token-metric-value').evaluate(
            "element => getComputedStyle(element).textOverflow"
        )
        == "clip"
    )
    frame.locator("details.token-turns summary").click()
    numeric_cells = frame.locator(".token-turn-row .token-cell.numeric")
    numeric_cells.first.wait_for()
    assert numeric_cells.all_inner_texts() == [
        "≈$23,593.27",
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

    frame.get_by_role("button", name="Live window").click()
    expect(frame.locator("#pipCanvas")).to_have_attribute(
        "data-tokens",
        "119234337188,3035000000,115665200000,534137188,400000000",
    )
    total_draws = frame.locator("#pipCanvas").evaluate(
        """() => window.__trajectoryCanvasTexts.filter(
          item => item.text === '119,234,337,188'
        )"""
    )
    assert total_draws
    font_size = float(total_draws[-1]["font"].split()[1].removesuffix("px"))
    assert font_size < 28
    assert not frame.locator("#pipCanvas").evaluate(
        """() => window.__trajectoryCanvasTexts.some(
          item => item.text.startsWith('119,234') && item.text.includes('…')
        )"""
    )
    frame.get_by_role("button", name="Close live window").click()


def test_timeline_selection_native_wheel_zoom_and_reset(page: Page, harness_url: str) -> None:
    page.goto(f"{harness_url}/en")
    frame = viewer(page)
    timeline = frame.locator("#timeline")
    expect(timeline).to_have_attribute("role", "group")
    assert frame.get_by_role("application").count() == 0
    timeline.scroll_into_view_if_needed()
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


def test_english_and_chinese_desktop_layout(page: Page, harness_url: str) -> None:
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(f"{harness_url}/en")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()
    content_columns = frame.locator(".content").evaluate(
        "element => getComputedStyle(element).gridTemplateColumns"
    )
    assert "340px" not in content_columns

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
    assert "估算花费" in frame.locator(".turn-toggle").last.inner_text()
    assert "0.000656" in frame.locator(".turn-toggle").last.inner_text()
    assert frame.locator('[data-token-metric="cost"] .token-metric-label').inner_text() == (
        "估算花费"
    )
    assert "不是 Codex 订阅账单" in frame.locator(".cost-note").inner_text()
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
    assert frame.locator('tr[data-id="record-2-8"] td.duration').inner_text() == "-"
    assert turn_columns.count() == 5
    assert frame.locator(".record-token").count() == 0
    assert "估算花费" not in frame.locator("#inspector").inner_text()
    assert turn_columns.last.is_visible()
    assert (
        turn_columns.nth(1).evaluate("element => getComputedStyle(element).display") == "table-cell"
    )
    assert turn_columns.nth(2).evaluate(
        "element => element.getBoundingClientRect().width"
    ) < turn_columns.nth(3).evaluate("element => element.getBoundingClientRect().width")
    ledger_wrap = frame.locator(".ledger-wrap")
    assert ledger_wrap.evaluate("element => element.scrollWidth === element.clientWidth")
    assert ledger_wrap.evaluate("element => getComputedStyle(element).overflowX") == "hidden"
    assert turn_columns.nth(2).evaluate("element => element.getBoundingClientRect().width") > 100
    assert turn_columns.last.evaluate(
        "element => element.getBoundingClientRect().right"
    ) <= ledger_wrap.evaluate("element => element.getBoundingClientRect().right")
    assert "340px" not in frame.locator(".content").evaluate(
        "element => getComputedStyle(element).gridTemplateColumns"
    )


@pytest.mark.parametrize("language", ["en", "zh-CN"])
@pytest.mark.parametrize("remember_full", [False, True])
def test_legacy_preloaded_result_initializes_once(
    page: Page, language: str, remember_full: bool
) -> None:
    snapshot = demo_trajectories()["session-alpha"]
    snapshot["detailLevel"] = "summary"
    snapshot.pop("recentSessions", None)
    for record in snapshot["records"]:
        record.update(input=None, output=None, metadata={})
    encoded = json.dumps(snapshot).replace("<", "\\u003c")
    mock = f"""<script>
      window.__legacyToolCalls = [];
      window.__legacyInitialReads = 0;
      const initialTrajectory = {encoded};
      window.openai = {{
        theme: "dark",
        locale: {json.dumps(language)},
        get toolOutput() {{
          window.__legacyInitialReads += 1;
          return initialTrajectory;
        }},
        async callTool(name, args) {{
          window.__legacyToolCalls.push({{name, args}});
          if (name === "get_codex_trajectory_preferences") {{
            return {{structuredContent: {{defaultFullDetails: {str(remember_full).lower()}}}}};
          }}
          if (name === "list_codex_sessions") {{
            return {{structuredContent: {{sessions: [initialTrajectory.session]}}}};
          }}
          throw new Error("Unexpected tool: " + name);
        }},
      }};
    </script>"""
    page.goto("about:blank")
    page.set_content(ui_html().replace("<body>", f"<body>{mock}", 1))
    expect(page.locator("#refresh")).to_be_visible()
    expect(page.locator("#sessionSelect")).to_be_enabled()
    expect(page.locator("#sessionSelect")).to_have_value("session-alpha")
    expect(page.locator("#useSummary" if remember_full else "#loadFull")).to_be_visible()
    expect(page.locator("html")).to_have_attribute("lang", language)
    expect(page.locator("html")).to_have_attribute("data-codex-theme", "dark")
    assert page.get_by_role("alert").count() == 0
    # Each read of the preloaded result occurs only during the one initialization.
    assert page.evaluate("window.__legacyInitialReads") == 2
    names = page.evaluate("window.__legacyToolCalls.map(call => call.name)")
    assert sorted(names) == ["get_codex_trajectory_preferences", "list_codex_sessions"]


def test_native_panel_initializes_and_refreshes_without_task_controls(
    page: Page, harness_url: str
) -> None:
    page.goto(f"{harness_url}/en-native")
    frame = viewer(page)
    expect(frame.locator("#refresh")).to_be_visible()
    assert page.evaluate("window.__trajectoryProtocol.slice(0, 2)") == [
        "ui/initialize",
        "ui/notifications/initialized",
    ]
    assert frame.locator("#requestStop, #autoStopEnabled, #cdpToolbarEnabled").count() == 0
    frame.locator("#openPip").click()
    dock = frame.locator("#liveDock")
    expect(dock).to_have_attribute("data-session-id", "session-alpha")
    expect(dock).to_have_attribute("data-detail-level", "summary")
    page.evaluate("window.__advanceTrajectoryLive()")
    expect(dock).to_have_attribute("data-record-count", "10", timeout=5000)
    expect(dock).to_have_attribute("data-total-cost", "≈$0.001672")
    expect(frame.locator("#dockQuota")).to_contain_text("68%")
    frame.locator("#closeDock").click()
    expect(frame.locator("#refresh")).to_be_visible()
    assert page.evaluate("window.__trajectoryDisplayModes") == ["fullscreen", "inline"]
    assert set(page.evaluate("window.__trajectoryToolNames")) <= {
        "list_codex_sessions",
        "get_codex_trajectory",
        "get_codex_trajectory_update",
        "get_codex_trajectory_preferences",
    }
    assert "ui/message" not in page.evaluate("window.__trajectoryProtocol")


def test_native_missing_context_requires_explicit_selection(page: Page, harness_url: str) -> None:
    page.goto(f"{harness_url}/en-native-selector")
    frame = viewer(page)
    select = frame.locator("#sessionSelect")
    expect(select.locator('option[value="session-beta"]')).to_have_count(1)
    expect(select).to_have_value("")
    assert frame.locator("#ledger").count() == 0
    assert set(page.evaluate("window.__trajectoryToolNames")) == {
        "list_codex_sessions",
        "get_codex_trajectory_preferences",
    }
    select.select_option("session-beta")
    expect(frame.locator("#sessionSelect")).to_have_value("session-beta")
    expect(frame.locator("#refresh")).to_be_visible()
    calls = page.evaluate("window.__trajectoryCalls")
    assert any(
        call.get("sessionId") == "session-beta" and call.get("detailLevel") == "summary"
        for call in calls
    )


def test_native_unavailable_task_can_retry_and_select(page: Page, harness_url: str) -> None:
    page.goto(f"{harness_url}/en-native-missing")
    frame = viewer(page)
    expect(frame.get_by_role("status")).to_contain_text("local trajectory is unavailable")
    expect(frame.locator('#sessionSelect option[value="session-alpha"]')).to_have_count(1)
    frame.locator("#retryOpen").click()
    page.wait_for_function("window.__trajectoryToolNames.includes('open_codex_trajectory')")
    expect(frame.locator("#sessionSelect")).to_have_value("")
    frame.locator("#sessionSelect").select_option("session-alpha")
    expect(frame.locator("#refresh")).to_be_visible()


@pytest.mark.parametrize("language", ["en", "zh"])
def test_sidebar_fullscreen_selects_a_task_and_returns_from_live_view(
    page: Page, harness_url: str, language: str
) -> None:
    page.goto(f"{harness_url}/{language}-native-sidebar")
    frame = viewer(page)
    select = frame.locator("#sessionSelect")
    expect(select.locator('option[value="session-beta"]')).to_have_count(1)
    expect(select).to_have_value("")
    assert frame.locator("#ledger").count() == 0
    assert set(page.evaluate("window.__trajectoryToolNames")) == {
        "list_codex_sessions",
        "get_codex_trajectory_preferences",
    }

    select.select_option("session-beta")
    expect(frame.locator("#refresh")).to_be_visible()
    expect(select).to_have_value("session-beta")
    calls = page.evaluate("window.__trajectoryCalls")
    assert any(
        call.get("sessionId") == "session-beta" and call.get("detailLevel") == "summary"
        for call in calls
    )
    frame.locator("#openPip").click()
    expect(frame.locator("#liveDock")).to_have_attribute("data-session-id", "session-beta")
    frame.locator("#closeDock").click()
    expect(frame.locator("#refresh")).to_be_visible()
    expect(select).to_have_value("session-beta")
    assert page.evaluate("window.__trajectoryDisplayModes") == []
    assert "ui/message" not in page.evaluate("window.__trajectoryProtocol")
    assert frame.get_by_role("alert").count() == 0


def test_native_host_context_controls_theme_locale_and_colors(page: Page, harness_url: str) -> None:
    page.goto(f"{harness_url}/zh-native")
    frame = viewer(page)
    expect(frame.locator("html")).to_have_attribute("lang", "zh-CN")
    expect(frame.locator("html")).to_have_attribute("data-codex-theme", "dark")
    expect(frame.get_by_role("button", name="刷新", exact=True)).to_be_visible()
    page.evaluate(
        """window.__setHostContext({theme:'light', locale:'en', styles:{variables:{
          '--color-background-primary':'#fafafa', '--color-text-primary':'#222222'
        }}})"""
    )
    expect(frame.locator("html")).to_have_attribute("data-codex-theme", "light")
    expect(frame.locator("html")).to_have_attribute("lang", "en")
    expect(frame.get_by_role("button", name="Refresh", exact=True)).to_be_visible()
    assert (
        frame.locator("body").evaluate("element => getComputedStyle(element).backgroundColor")
        == "rgb(250, 250, 250)"
    )
    page.evaluate("window.__setHostContext({theme:'dark', styles:{variables:{}}})")
    expect(frame.locator("html")).to_have_attribute("data-codex-theme", "dark")
    assert (
        frame.locator("body").evaluate("element => getComputedStyle(element).backgroundColor")
        == "rgb(20, 20, 20)"
    )


def test_native_bridge_ignores_non_host_results_and_teardown_stops_polling(
    page: Page, harness_url: str
) -> None:
    page.goto(f"{harness_url}/en-native")
    frame = viewer(page)
    expect(frame.locator("#refresh")).to_be_visible()
    frame.locator("body").evaluate(
        """() => window.postMessage({jsonrpc:'2.0',method:'ui/notifications/tool-result',
          params:{structuredContent:{viewerState:'select-session'}}}, '*')"""
    )
    expect(frame.locator("#refresh")).to_be_visible()
    frame.locator("#openPip").click()
    expect(frame.locator("#liveDock")).to_be_visible()
    page.wait_for_function("window.__trajectoryToolNames.includes('get_codex_trajectory_update')")
    page.evaluate(
        """document.getElementById('viewer').contentWindow.postMessage({jsonrpc:'2.0',
          id:'teardown',method:'ui/resource-teardown',params:{}}, '*')"""
    )
    page.wait_for_timeout(150)
    before = page.evaluate("window.__trajectoryToolNames.length")
    page.wait_for_timeout(1300)
    assert page.evaluate("window.__trajectoryToolNames.length") == before


def test_native_fullscreen_entry_returns_to_viewer_without_inline_mode(
    page: Page, harness_url: str
) -> None:
    page.goto(f"{harness_url}/en-native-fullscreen")
    frame = viewer(page)
    expect(frame.locator("#refresh")).to_be_visible()
    frame.locator("#openPip").click()
    expect(frame.locator("#liveDock")).to_be_visible()
    page.wait_for_function("window.__trajectoryToolNames.includes('get_codex_trajectory_update')")
    frame.locator("#closeDock").click()
    expect(frame.locator("#refresh")).to_be_visible()
    assert page.evaluate("window.__trajectoryDisplayModes") == []
    assert frame.get_by_role("alert").count() == 0


@pytest.mark.parametrize("language", ["en", "zh"])
@pytest.mark.parametrize("width", [400, 1280])
def test_large_log_error_requires_user_choice_then_refreshes_with_task_budget(
    page: Page, harness_url: str, language: str, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    try:
        page.goto(f"{harness_url}/{language}-native-read-limit")
        frame = viewer(page)
        prompt = frame.locator("#readLimitPrompt")
        expect(prompt).to_be_visible()
        expect(prompt.get_by_role("status")).to_contain_text("2.84 GB")
        expect(prompt.get_by_role("status")).to_contain_text("512 MiB")
        expect(frame.locator("#readLimitGb")).to_have_value("3")
        expect(frame.locator("#enableReadLimit")).to_have_text(
            "启用并重试" if language == "zh" else "Enable and retry"
        )
        page.wait_for_function("window.__trajectoryToolNames.includes('list_codex_sessions')")
        assert "get_codex_trajectory" not in page.evaluate("window.__trajectoryToolNames")
        for selector in ("#readLimitGb", "#enableReadLimit"):
            assert frame.locator(selector).evaluate(
                "element => { const r=element.getBoundingClientRect(); "
                "return r.left >= 0 && r.right <= innerWidth; }"
            )

        # A budget smaller than the shown size does not start a read.
        frame.locator("#readLimitGb").fill("2")
        frame.locator("#enableReadLimit").click()
        assert "get_codex_trajectory" not in page.evaluate("window.__trajectoryToolNames")
        frame.locator("#readLimitGb").fill("3")
        frame.locator("html").evaluate(
            """() => {
              window.__requestTimeouts = [];
              const original = window.setTimeout;
              window.setTimeout = (callback, milliseconds, ...args) => {
                window.__requestTimeouts.push(milliseconds);
                return original(callback, milliseconds, ...args);
              };
            }"""
        )
        if width == 400 and language == "zh":
            frame.locator("#readLimitGb").press("Enter")
        else:
            frame.locator("#enableReadLimit").click()
        expect(frame.locator("#refresh")).to_be_visible()
        assert frame.locator("#readLimitPrompt").count() == 0
        calls = page.evaluate("window.__trajectoryCalls")
        chosen = next(call for call in calls if call.get("maxReadBytes"))
        assert chosen["sessionId"] == "session-alpha"
        assert chosen["maxReadBytes"] == 3_000_000_000
        assert chosen["detailLevel"] == "summary"
        assert max(frame.locator("html").evaluate("window.__requestTimeouts")) >= 180_000

        frame.locator("#refresh").click()
        page.wait_for_function(
            "window.__trajectoryCalls.filter(call => call.maxReadBytes === 3000000000).length >= 2"
        )
        frame.locator("#openPip").click()
        expect(frame.locator("#liveDock")).to_be_visible()
        page.wait_for_function(
            "window.__trajectoryToolNames.includes('get_codex_trajectory_update')"
        )
        names = page.evaluate("window.__trajectoryToolNames")
        calls = page.evaluate("window.__trajectoryCalls")
        assert calls[names.index("get_codex_trajectory_update")]["maxReadBytes"] == 3_000_000_000
        frame.locator("#closeDock").click()
        expect(frame.locator("#refresh")).to_be_visible()

        # Selecting another task leaves that task at its default budget.
        frame.locator("#sessionSelect").select_option("session-beta")
        page.wait_for_function("window.__trajectoryCalls.at(-1).sessionId === 'session-beta'")
        assert "maxReadBytes" not in page.evaluate("window.__trajectoryCalls.at(-1)")
    finally:
        page.set_viewport_size({"width": 1280, "height": 900})


def test_growing_large_log_offers_new_budget_from_live_error(page: Page, harness_url: str) -> None:
    page.goto(f"{harness_url}/en-native-read-limit")
    frame = viewer(page)
    frame.locator("#enableReadLimit").click()
    expect(frame.locator("#refresh")).to_be_visible()
    frame.locator("#openPip").click()
    expect(frame.locator("#liveDock")).to_be_visible()
    page.evaluate("window.__setRequiredReadBytes(3500000000)")
    expect(frame.locator("#readLimitPrompt")).to_be_visible()
    expect(frame.locator("#readLimitGb")).to_have_value("4")
    expect(frame.locator("#readLimitPrompt").get_by_role("status")).to_contain_text("3.5 GB")
    expect(frame.locator("#readLimitPrompt").get_by_role("status")).to_contain_text("3 GB")
    frame.locator("#enableReadLimit").click()
    expect(frame.locator("#readLimitPrompt")).not_to_be_visible()
    expect(frame.locator("#refresh")).to_be_visible()
    assert any(
        call.get("maxReadBytes") == 4_000_000_000
        for call in page.evaluate("window.__trajectoryCalls")
    )


def test_raised_read_budget_survives_history_and_full_detail_requests(
    page: Page, harness_url: str
) -> None:
    page.goto(f"{harness_url}/en-native-read-limit")
    frame = viewer(page)
    frame.locator("#enableReadLimit").click()
    expect(frame.locator("#refresh")).to_be_visible()
    # Use the host's existing large synthetic history with the same selected task ID.
    page.evaluate(
        """() => {
          const source = structuredClone(trajectories['session-paged']);
          source.session.id = 'session-alpha';
          trajectories['session-alpha'] = source;
        }"""
    )
    frame.locator("#refresh").click()
    expect(frame.locator("#loadEarlier")).to_be_visible()
    frame.locator("#loadEarlier").click()
    page.wait_for_function("window.__trajectoryCalls.some(call => call.beforeRecord)")
    history = page.evaluate("window.__trajectoryCalls.find(call => call.beforeRecord)")
    assert history["maxReadBytes"] == 3_000_000_000
    frame.locator("#loadFull").click()
    frame.locator("#confirmFull").click()
    page.wait_for_function("window.__trajectoryCalls.some(call => call.detailLevel === 'full')")
    full = page.evaluate("window.__trajectoryCalls.find(call => call.detailLevel === 'full')")
    assert full["maxReadBytes"] == 3_000_000_000


def test_read_limit_prompt_follows_host_locale_changes(page: Page, harness_url: str) -> None:
    page.goto(f"{harness_url}/zh-native-read-limit")
    frame = viewer(page)
    expect(frame.locator("#enableReadLimit")).to_have_text("启用并重试")
    page.evaluate("window.__setHostContext({locale:'en'})")
    expect(frame.locator("#enableReadLimit")).to_have_text("Enable and retry")
    assert "get_codex_trajectory" not in page.evaluate("window.__trajectoryToolNames")


def assert_no_horizontal_overflow(frame: FrameLocator) -> None:
    """Every visible content surface fits without masking a wider inner layout."""
    overflowing = frame.locator("body").evaluate(
        """body => [...body.querySelectorAll('*')].filter(element => {
          const style = getComputedStyle(element);
          return element.clientWidth > 0 && style.display !== 'none'
            && !['absolute', 'fixed'].includes(style.position)
            && element.scrollWidth > element.clientWidth + 1;
        }).map(element => ({tag: element.tagName, id: element.id,
          className: element.className, width: element.clientWidth,
          contentWidth: element.scrollWidth}))"""
    )
    assert overflowing == []
    assert frame.locator("html").evaluate("element => element.scrollWidth <= window.innerWidth + 1")


@pytest.mark.parametrize("width", [320, 400, 800, 1280])
@pytest.mark.parametrize("language", ["en", "zh"])
def test_responsive_viewer_wraps_all_information_and_returns_to_record(
    page: Page, harness_url: str, width: int, language: str
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    try:
        page.goto(f"{harness_url}/{language}-native")
        frame = viewer(page)
        frame.locator("#refresh").wait_for()
        page.evaluate(
            """() => {
              const source = trajectories['session-big-tokens'];
              source.session.title = 'Responsive task ' + 'LongTitle'.repeat(30);
              source.session.model = 'model-' + 'long'.repeat(40);
              const record = source.records.at(-1);
              record.event = 'LongEvent'.repeat(30);
              record.summary = 'LongSummary'.repeat(100);
              record.callId = 'long-call-id-'.repeat(50);
              record.input = 'UnbrokenInput'.repeat(120);
              record.output = 'UnbrokenOutput'.repeat(120);
            }"""
        )
        frame.locator('#sessionSelect option[value="session-big-tokens"]').wait_for(
            state="attached"
        )
        frame.locator("#sessionSelect").select_option("session-big-tokens")
        expect(frame.locator("h1")).to_contain_text("Responsive task")
        frame.locator("#loadFull").click()
        frame.locator("#rememberFull").uncheck()
        frame.locator("#confirmFull").click()
        expect(frame.locator("#useSummary")).to_be_visible()
        frame.locator("details.token-turns summary").click()
        expect(frame.locator(".token-turn-row")).to_have_count(1)
        assert frame.locator(".token-turn-row .token-cell[data-label]").count() == 8
        assert_no_horizontal_overflow(frame)
        record = frame.locator('tr[data-index="2"]')
        record.click()
        expect(frame.locator("#inspector")).to_contain_text("UnbrokenOutput" * 120)
        assert_no_horizontal_overflow(frame)
        controls = frame.locator("button:not(.span), input:not([type=checkbox]), select")
        assert controls.evaluate_all(
            """elements => elements.filter(element => element.getClientRects().length)
              .every(element => element.getBoundingClientRect().height >= 44)"""
        )
        if width <= 1380:
            expect(frame.locator("#inspector")).to_be_focused()
            frame.locator("#backToRecords").click()
            expect(record).to_be_focused()
        if width <= 700:
            assert record.evaluate("element => getComputedStyle(element).display") == "grid"
            assert record.locator("td[data-label]").count() == 3
    finally:
        page.set_viewport_size({"width": 1280, "height": 900})


@pytest.mark.parametrize("width,height", [(320, 480), (400, 900), (800, 600), (1280, 900)])
@pytest.mark.parametrize("language", ["en", "zh"])
def test_live_view_wraps_long_content_and_keeps_stream_usable(
    page: Page, harness_url: str, width: int, height: int, language: str
) -> None:
    page.set_viewport_size({"width": width, "height": height})
    try:
        page.goto(f"{harness_url}/{language}-native")
        frame = viewer(page)
        frame.locator("#refresh").wait_for()
        page.evaluate(
            """() => {
              const source = trajectories['session-alpha'];
              source.session.title = 'LiveTitle'.repeat(30);
              source.stats.tokens = trajectories['session-big-tokens'].stats.tokens;
              source.records.at(-1).event = 'LiveEvent'.repeat(30);
              source.records.at(-1).summary = 'LiveSummary'.repeat(100);
            }"""
        )
        frame.locator("#refresh").click()
        expect(frame.locator("h1")).to_contain_text("LiveTitle")
        frame.locator("#openPip").click()
        expect(frame.locator("#liveDock")).to_be_visible()
        assert_no_horizontal_overflow(frame)
        expect(frame.locator(".dock-record.latest .dock-record-summary")).to_have_text(
            "LiveSummary" * 100
        )
        assert frame.locator(".dock-record.latest .dock-record-summary").evaluate(
            "element => element.scrollHeight <= element.clientHeight + 1"
        )
        assert frame.locator("#liveRecordStream").evaluate(
            "element => element.getBoundingClientRect().height >= 140"
        )
        assert frame.locator("#closeDock").evaluate(
            "element => element.getBoundingClientRect().height >= 44"
        )
        frame.locator("#closeDock").click()
        expect(frame.locator("#refresh")).to_be_visible()
    finally:
        page.set_viewport_size({"width": 1280, "height": 900})


@pytest.mark.parametrize("language,width", [("en", 1280), ("zh", 400)])
def test_manual_fast_cost_mode_updates_all_surfaces_and_is_task_scoped(
    page: Page, harness_url: str, language: str, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    try:
        page.goto(f"{harness_url}/{language}-native")
        frame = viewer(page)
        metric = frame.locator('[data-token-metric="cost"] .token-metric-value')
        expect(metric).to_have_text("≈$0.001488")
        frame.locator("details.token-turns summary").click()
        frame.locator("#costMode").select_option("fast")
        expect(metric).to_have_text("≈$0.003720")
        assert frame.locator('.token-turn-row [data-token-column="cost"]').all_inner_texts() == [
            "≈$0.002080",
            "≈$0.001640",
        ]
        expect(frame.locator(".turn-toggle").last).to_contain_text("≈$0.001640")
        assert (
            frame.locator('[data-token-metric="total"] .token-metric-value').inner_text() == "640"
        )
        assert_no_horizontal_overflow(frame)
        assert page.evaluate("trajectories['session-alpha'].stats.cost.totalUsd") == 0.001488
        frame.locator("#refresh").click()
        expect(metric).to_have_text("≈$0.003720")
        expect(frame.locator("#costMode")).to_have_value("fast")
        frame.locator('#sessionSelect option[value="session-beta"]').wait_for(state="attached")
        frame.locator("#sessionSelect").select_option("session-beta")
        expect(frame.locator("h1")).to_have_text("Review the documentation")
        expect(frame.locator("#costMode")).to_have_value("auto")
        frame.locator("#sessionSelect").select_option("session-alpha")
        expect(metric).to_have_text("≈$0.003720")
        frame.locator("#openPip").click()
        expect(frame.locator("#liveDock")).to_have_attribute("data-total-cost", "≈$0.003720")
        expect(frame.locator("#liveDock")).to_have_attribute("data-turn-cost", "≈$0.001640")
        expect(frame.locator("#liveDock")).to_have_attribute("data-tokens", "640,128,384,128,40")
        assert frame.locator(".dock-turn-cost").all_inner_texts() == [
            ("估算花费 " if language == "zh" else "Estimated cost ") + "≈$0.002080",
            ("估算花费 " if language == "zh" else "Estimated cost ") + "≈$0.001640",
        ]
        page.evaluate("window.__advanceTrajectoryLive()")
        expect(frame.locator("#liveDock")).to_have_attribute("data-total-cost", "≈$0.004180")
        expect(frame.locator("#liveDock")).to_have_attribute("data-turn-cost", "≈$0.000460")
        frame.locator("#closeDock").click()
        frame.locator("#costMode").select_option("auto")
        expect(frame.locator("#costMode")).to_have_value("auto")
        expect(metric).to_have_text("≈$0.001672")
    finally:
        page.set_viewport_size({"width": 1280, "height": 900})


def test_logged_fast_costs_override_manual_mode_without_double_multiplication(
    page: Page, harness_url: str
) -> None:
    page.goto(f"{harness_url}/en-native")
    frame = viewer(page)
    frame.locator("#costMode").select_option("fast")
    page.evaluate(
        """() => {
          const source = trajectories['session-alpha'];
          for (const cost of [source.stats.cost, ...source.turns.map(turn => turn.cost)]) {
            if (!cost) continue;
            for (const name of ['totalUsd', 'uncachedInputUsd', 'cachedInputUsd', 'outputUsd']) {
              if (typeof cost[name] === 'number') cost[name] *= 2.5;
            }
          }
          source.warnings = [{code: 'codex_fast_cost_multiplier', line: 1,
            message: 'Fast x2.5 included-usage estimate applied.'}];
        }"""
    )
    frame.locator("#refresh").click()
    expect(frame.locator("#costMode")).to_be_disabled()
    expect(frame.locator("#costMode")).to_have_value("auto")
    expect(frame.locator('[data-token-metric="cost"] .token-metric-value')).to_have_text(
        "≈$0.003720"
    )
    expect(frame.locator("#costModeNote")).to_contain_text("already applied")
    frame.locator("#openPip").click()
    expect(frame.locator("#liveDock")).to_have_attribute("data-total-cost", "≈$0.003720")
    expect(frame.locator("#liveDock")).to_have_attribute("data-turn-cost", "≈$0.001640")
    frame.locator("#closeDock").click()


def test_native_video_pip_keeps_fast_multiplier_and_original_token_counts(
    page: Page, harness_url: str
) -> None:
    page.goto(f"{harness_url}/en")
    frame = viewer(page)
    frame.locator("#costMode").select_option("fast")
    frame.locator("#openPip").click()
    expect(frame.locator("#pipCanvas")).to_have_attribute("data-total-cost", "≈$0.003720")
    expect(frame.locator("#pipCanvas")).to_have_attribute("data-turn-cost", "≈$0.001640")
    expect(frame.locator("#pipCanvas")).to_have_attribute("data-tokens", "640,128,384,128,40")
    frame.get_by_role("button", name="Close live window").click()


def test_account_quota_updates_while_task_revision_is_unchanged(
    page: Page, harness_url: str
) -> None:
    page.goto(f"{harness_url}/en-dock")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()
    frame.get_by_role("button", name="Live window").click()
    quota = frame.locator("#dockQuota")
    expect(quota.locator('[data-quota-window="primary"] strong')).to_have_text("68.5%")
    page.wait_for_function("window.__trajectoryCalls.some(args => args.revision)")
    page.evaluate("""window.__setAccountQuota({
      rateLimits: {primary: {usedPercent: 93, windowMinutes: 10080, resetsAt: null}},
      sampledAt: '2026-10-01T00:00:00Z',
    })""")
    expect(quota.locator('[data-quota-window="primary"] strong')).to_have_text("7%")
    assert quota.locator(".dock-quota-window span").all_inner_texts() == ["Weekly"]
    assert "Latest local log sample" in quota.get_attribute("title")
    expect(frame.locator("#liveDock")).to_have_attribute("data-record-count", "9")
    page.evaluate("window.__setAccountQuota({rateLimits: null, sampledAt: null})")
    expect(quota).to_be_hidden()


def test_quota_window_tooltip_updates_when_only_sample_time_changes(
    page: Page, harness_url: str
) -> None:
    page.goto(f"{harness_url}/en-dock")
    frame = viewer(page)
    frame.get_by_text("Safe summary", exact=True).wait_for()
    frame.get_by_role("button", name="Live window").click()
    quota = frame.locator("#dockQuota")
    window = quota.locator('[data-quota-window="primary"]')
    old_title = window.get_attribute("title")
    assert old_title is not None
    page.evaluate("""() => {
      window.__setAccountQuota({
        rateLimits: structuredClone(trajectories['session-alpha'].stats.rateLimits),
        sampledAt: '2026-10-01T10:00:00Z',
      });
    }""")
    expect(window).not_to_have_attribute("title", old_title)
    sample_note = quota.get_attribute("title").split("Latest local log sample: ")[-1]
    assert sample_note in window.get_attribute("title")
    expect(window.locator("strong")).to_have_text("68.5%")


@pytest.mark.parametrize("result_kind", ["trajectory", "read-limit"])
def test_large_log_retry_rejects_changed_task_identity(
    page: Page, harness_url: str, result_kind: str
) -> None:
    page.goto(f"{harness_url}/en-native-read-limit")
    frame = viewer(page)
    expect(frame.locator("#readLimitPrompt")).to_be_visible()
    override = (
        "__summaryIdentityOverride"
        if result_kind == "trajectory"
        else "__retryReadLimitIdentityOverride"
    )
    page.evaluate("key => { window[key] = 'session-beta'; }", override)
    frame.locator("#enableReadLimit").click()
    expect(frame.locator('[data-viewer-state="select-session"]')).to_be_visible()
    expect(frame.locator("#timeline")).to_have_count(0)
    expect(frame.locator("#readLimitPrompt")).to_have_count(0)


def test_repeated_message_ids_load_details_across_page_sizes(
    page: Page, harness_url: str, tmp_path: Path
) -> None:
    events: list[dict[str, Any]] = [{"type": "session_meta", "payload": {"id": "session-alpha"}}]
    for number in range(1, 62):
        events.append(
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "id": "repeat" if number in (1, 61) else f"message-{number}",
                    "content": f"message {number} " + "detail body " * 40,
                },
            }
        )
    path = write_rollout(tmp_path / "repeated.jsonl", events)
    summary = parse_session(path, 500)
    full = parse_session(path, 50, "full", 62)
    assert summary["records"][-1]["id"] != full["records"][-1]["id"]
    page.goto(f"{harness_url}/en-native")
    frame = viewer(page)
    frame.locator("#refresh").wait_for()
    page.evaluate(
        """({summary, full}) => {
      window.__fullDetailOverride = full;
      notify(summary);
    }""",
        {"summary": summary, "full": full},
    )
    frame.locator('tr[data-index="61"]').wait_for()
    frame.locator("#loadFull").click()
    frame.locator("#rememberFull").uncheck()
    frame.locator("#confirmFull").click()
    expect(frame.locator("#inspector")).to_contain_text(full["records"][-1]["output"])
    expect(frame.locator(".detail-load-state")).to_have_count(0)


def test_detail_revision_change_rejects_loaded_fields(page: Page, harness_url: str) -> None:
    page.goto(f"{harness_url}/en-native")
    frame = viewer(page)
    frame.locator("#loadFull").click()
    frame.locator("#rememberFull").uncheck()
    page.evaluate("window.__detailRevisionOverride = 'f'.repeat(64)")
    frame.locator("#confirmFull").click()
    expect(frame.locator(".detail-load-state")).to_contain_text("Refresh")
    expect(frame.locator("#inspector pre")).to_have_count(0)


def test_search_uses_loaded_authorized_details_only(page: Page, harness_url: str) -> None:
    page.goto(f"{harness_url}/en-native")
    frame = viewer(page)
    frame.locator("#loadFull").click()
    frame.locator("#rememberFull").uncheck()
    frame.locator("#confirmFull").click()
    frame.locator('tr[data-index="6"]').click()
    expect(frame.locator("#inspector")).to_contain_text("example --fail")
    reads = len(page.evaluate("window.__trajectoryCalls"))
    for needle in ("example --fail", "exit code 2"):
        frame.locator("#search").fill(needle)
        expect(frame.locator('tr[data-index="6"]')).to_be_visible()
    assert len(page.evaluate("window.__trajectoryCalls")) == reads
    frame.locator("#search").fill("")
    frame.locator("#useSummary").click()
    frame.get_by_text("Safe summary", exact=True).wait_for()
    frame.locator("#search").fill("example --fail")
    expect(frame.locator('tr[data-index="6"]')).to_have_count(0)


def test_live_locale_change_refreshes_labels_and_preserves_reading(
    page: Page, harness_url: str
) -> None:
    page.goto(f"{harness_url}/en-native")
    frame = viewer(page)
    frame.locator("#openPip").click()
    details = frame.locator('.dock-record[data-index="2"] .dock-record-expand')
    details.locator("summary").click()
    expect(details).to_have_attribute("open", "")
    stream = frame.locator("#liveRecordStream")
    expect(stream).to_have_attribute("data-follow-latest", "false")
    page.evaluate("window.__setHostContext({locale:'zh-CN'})")
    expect(frame.locator(".dock-kicker")).to_have_text("实时轨迹")
    expect(frame.locator(".dock-section-label")).to_have_text("任务总计")
    expect(frame.locator(".dock-total-label").first).to_have_text("总 Token")
    expect(frame.locator(".dock-stream-title")).to_have_text("实时输出")
    expect(frame.locator("#dockQuota .dock-quota-label")).to_have_text("Codex 额度")
    expect(details.locator("summary")).to_contain_text("分析与决策")
    expect(details).to_have_attribute("open", "")
    expect(stream).to_have_attribute("data-follow-latest", "false")
    page.evaluate("window.__setHostContext({locale:'en'})")
    expect(frame.locator(".dock-kicker")).to_have_text("Live trajectory")
    expect(details).to_have_attribute("open", "")


def test_selector_locale_change_preserves_selector_and_updates_labels(
    page: Page, harness_url: str
) -> None:
    page.goto(f"{harness_url}/en-native-selector")
    frame = viewer(page)
    expect(frame.locator("#retryOpen")).to_have_text("Retry")
    page.evaluate("window.__setHostContext({locale:'zh-CN'})")
    expect(frame.locator("#retryOpen")).to_have_text("重试")
    expect(frame.locator("#sessionSelect")).to_have_attribute("aria-label", "选择任务")
    expect(frame.locator(".notice")).to_contain_text("本地任务")


@pytest.mark.parametrize("width", [400, 600, 760, 1000, 1280, 1440])
@pytest.mark.parametrize("scale", [1, 2])
def test_readable_report_and_live_panel_at_desktop_widths(
    page: Page, harness_url: str, width: int, scale: int
) -> None:
    """Small panels retain readable text, reachable controls and a usable event stream."""
    browser = page.context.browser
    assert browser is not None
    context = browser.new_context(
        viewport={"width": width, "height": 700}, device_scale_factor=scale
    )
    try:
        probe = context.new_page()
        probe.goto(f"{harness_url}/en-dock")
        frame = viewer(probe)
        frame.locator("tr.record").first.wait_for()
        assert frame.locator("body").evaluate("e => parseFloat(getComputedStyle(e).fontSize) >= 15")
        for selector in (".stat-label", "th", ".token-metric-label"):
            assert frame.locator(selector).first.evaluate(
                "e => parseFloat(getComputedStyle(e).fontSize) >= 12"
            )
        for selector in ("#refresh", "#openPip", "#loadFull"):
            assert frame.locator(selector).evaluate(
                "e => {const r=e.getBoundingClientRect(); "
                "return r.left >= 0 && r.right <= innerWidth;}"
            )
        assert frame.locator("body").evaluate("e => e.scrollWidth <= innerWidth")
        assert frame.locator(".ledger-wrap").evaluate("e => e.scrollWidth <= e.clientWidth")
        if width <= 1380:
            assert frame.locator("#inspector").evaluate(
                "e => e.getBoundingClientRect().top >= "
                "document.querySelector('.ledger-wrap').getBoundingClientRect().bottom - 1"
            )
        if width >= 1440:
            assert "340px" in frame.locator(".content").evaluate(
                "e => getComputedStyle(e).gridTemplateColumns"
            )
        frame.locator("#openPip").click()
        frame.locator(".dock-record").first.wait_for()
        for selector in (
            ".dock-record-event",
            ".dock-record-summary",
            ".dock-token-label",
            ".dock-quota-window span",
        ):
            assert frame.locator(selector).first.evaluate(
                "e => parseFloat(getComputedStyle(e).fontSize) >= 12"
            )
        assert frame.locator("#liveRecordStream").evaluate(
            "e => e.clientHeight >= 150 && e.scrollWidth <= e.clientWidth"
        )
        for selector in ("#closeDock", ".dock-quota"):
            assert frame.locator(selector).evaluate(
                "e => {const r=e.getBoundingClientRect(); "
                "return r.left >= 0 && r.right <= innerWidth;}"
            )
    finally:
        context.close()

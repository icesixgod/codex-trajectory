"""Browser acceptance coverage for the trajectory app resource."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from playwright.sync_api import FrameLocator, Page, sync_playwright
from ui_harness import start_server

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
        yield context.new_page()
        context.close()
        browser.close()


def viewer(page: Page) -> FrameLocator:
    """Return the app-resource iframe locator."""
    return page.frame_locator("#viewer")


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


def test_full_details_refresh_and_task_switch_safety(page: Page) -> None:
    frame = viewer(page)
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
    frame.locator('tr[data-id="record-1-3"]').click()
    assert "uv run pytest" in frame.locator("#inspector").inner_text()

    frame.get_by_role("button", name="Refresh").click()
    frame.get_by_text("Full details", exact=True).wait_for()
    assert frame.get_by_text("Full details", exact=True).is_visible()

    frame.locator("#sessionSelect").select_option("session-beta")
    frame.get_by_text("Safe summary", exact=True).wait_for()
    assert frame.locator("#sessionSelect").input_value() == "session-beta"
    assert frame.locator("tr.record").count() == 3
    assert frame.locator(".turn-toggle").count() == 1
    assert frame.locator(".turn-toggle").get_attribute("aria-expanded") == "true"


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

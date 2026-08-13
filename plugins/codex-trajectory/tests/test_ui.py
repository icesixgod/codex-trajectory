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
    assert frame.locator("tr.record").count() == 9
    assert frame.get_by_text("Tool input, output, and raw metadata are hidden.").is_visible()
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
    page.once("dialog", lambda dialog: dialog.accept())
    frame.get_by_role("button", name="Load full details").click()
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


def test_timeline_selection_native_wheel_zoom_and_reset(page: Page) -> None:
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
    assert frame.get_by_role("button", name="加载完整详情").is_visible()
    assert (
        frame.locator("thead th").nth(1).evaluate("element => getComputedStyle(element).display")
        == "none"
    )
    assert (
        frame.locator(".content").evaluate(
            "element => getComputedStyle(element).gridTemplateColumns"
        )
        == "600px"
    )

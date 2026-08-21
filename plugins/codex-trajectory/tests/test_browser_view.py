"""Coverage for the tokenized loopback in-app Browser view."""

from __future__ import annotations

import http.client
import json
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

import pytest
from codex_trajectory.browser_view import (
    MAX_ASSET_BYTES,
    MAX_REQUEST_BYTES,
    THEME_COLOR_KEYS,
    BrowserViewServer,
    injection_source,
)


def sample_provider(name: str, arguments: dict[str, Any]) -> dict[str, object]:
    return {"structuredContent": {"name": name, "arguments": arguments}}


def raising_provider(error: BaseException) -> Any:
    def provider(_name: str, _arguments: dict[str, Any]) -> dict[str, Any]:
        raise error

    return provider


def sample_theme(scheme: str = "dark") -> dict[str, object]:
    return {
        "scheme": scheme,
        "colors": {key: "#181818" for key in THEME_COLOR_KEYS},
    }


def post_tool(
    server: BrowserViewServer,
    name: str,
    arguments: object,
    *,
    content_type: str = "application/json",
) -> tuple[int, dict[str, Any]]:
    body = json.dumps({"name": name, "arguments": arguments}).encode()
    request = Request(
        urljoin(server.url, "api/tool"),
        data=body,
        headers={"Content-Type": content_type},
        method="POST",
    )
    with urlopen(request, timeout=2) as response:
        return response.status, json.loads(response.read())


def post_stop(
    server: BrowserViewServer,
    value: object,
) -> tuple[int, dict[str, Any]]:
    request = Request(
        urljoin(server.url, "api/stop"),
        data=json.dumps(value).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=2) as response:
        return response.status, json.loads(response.read())


def get_task_state(
    server: BrowserViewServer,
    session_id: str,
    turn_id: str | None = None,
) -> tuple[int, dict[str, Any]]:
    url = urljoin(server.url, f"api/task-state?sessionId={session_id}")
    if turn_id is not None:
        url = f"{url}&turnId={turn_id}"
    with urlopen(url, timeout=2) as response:
        return response.status, json.loads(response.read())


def test_injection_source_builds_user_clicked_browser_link() -> None:
    source = injection_source("http://127.0.0.1:43123/private-token/")

    assert "const VERSION = 7" in source
    assert 'link.target = "_blank"' in source
    assert "sessionId" in source
    assert "data-above-composer-conversation-id" in source
    assert "client-new-thread:" in source
    assert 'searchParams.set("lang"' in source
    assert "openDrawer" not in source
    assert "Send message" not in source


def test_browser_server_serves_full_viewer_bridge_and_head() -> None:
    requested: list[tuple[str, dict[str, Any]]] = []

    def provider(name: str, arguments: dict[str, Any]) -> dict[str, object]:
        requested.append((name, arguments))
        return sample_provider(name, arguments)

    server = BrowserViewServer(provider)
    server.start()
    try:
        with urlopen(server.url, timeout=2) as response:
            html = response.read().decode("utf-8")
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
        assert '<iframe id="viewer"' in html

        for asset, marker in (
            ("trajectory-browser.css", "#viewer"),
            ("trajectory-browser.js", "api/tool"),
        ):
            with urlopen(urljoin(server.url, asset), timeout=2) as response:
                asset_body = response.read().decode("utf-8")
                assert marker in asset_body
                if asset == "trajectory-browser.js":
                    assert "const stopBridgeEnabled = false" in asset_body
                    assert "api/theme" in asset_body
                    assert "api/task-state" in asset_body

        with urlopen(urljoin(server.url, "trajectory.html?lang=en"), timeout=2) as response:
            viewer = response.read().decode("utf-8")
            assert "frame-ancestors 'self'" in response.headers["Content-Security-Policy"]
        assert "Local task trajectory" in viewer
        assert "__WHALE_MINING_SPRITE_DATA_URI__" not in viewer
        assert "data:image/png;base64," in viewer

        status, payload = post_tool(
            server,
            "get_codex_trajectory",
            {"sessionId": "session-alpha", "detailLevel": "summary"},
            content_type="application/json; charset=utf-8",
        )
        assert status == 200
        assert payload["structuredContent"]["arguments"]["sessionId"] == "session-alpha"
        assert requested == [
            (
                "get_codex_trajectory",
                {"sessionId": "session-alpha", "detailLevel": "summary"},
            )
        ]

        request = Request(server.url, method="HEAD")
        with urlopen(request, timeout=2) as response:
            assert response.status == 200
            assert response.read() == b""
            assert int(response.headers["Content-Length"]) > 0
    finally:
        server.close()


def test_browser_server_exposes_only_validated_codex_theme_colors() -> None:
    server = BrowserViewServer(sample_provider, theme_provider=sample_theme)
    server.start()
    try:
        with urlopen(urljoin(server.url, "api/theme"), timeout=2) as response:
            assert response.status == 200
            assert json.loads(response.read()) == sample_theme()
    finally:
        server.close()

    for invalid in (
        lambda: {"scheme": "system", "colors": sample_theme()["colors"]},
        lambda: {"scheme": "dark", "colors": {"bg": "url(file:///tmp/private)"}},
        lambda: {"scheme": "dark", "colors": sample_theme()["colors"], "extra": True},
    ):
        invalid_server = BrowserViewServer(sample_provider, theme_provider=invalid)
        invalid_server.start()
        try:
            with pytest.raises(HTTPError) as error:
                urlopen(urljoin(invalid_server.url, "api/theme"), timeout=2)
            assert error.value.code == 500
            assert json.loads(error.value.read()) == {"error": "theme unavailable"}
        finally:
            invalid_server.close()


def test_browser_server_exposes_only_validated_stop_intents_when_configured() -> None:
    requested: list[dict[str, Any]] = []

    def stop_provider(value: dict[str, Any]) -> dict[str, Any]:
        requested.append(value)
        return {"sent": True}

    server = BrowserViewServer(sample_provider, stop_provider)
    server.start()
    try:
        with urlopen(urljoin(server.url, "trajectory-browser.js"), timeout=2) as response:
            script = response.read().decode("utf-8")
        assert "const stopBridgeEnabled = true" in script
        assert "stopBridge" in script

        status, result = post_stop(
            server,
            {
                "sessionId": "01a01a1b-93ed-7791-a028-85cd1dd37f91",
                "turnId": "01a01a1b-93ed-7791-a028-85cd1dd37f92",
                "source": "auto",
                "threshold": 10,
                "language": "zh",
            },
        )
        assert status == 200
        assert result == {"sent": True}
        assert requested == [
            {
                "sessionId": "01a01a1b-93ed-7791-a028-85cd1dd37f91",
                "turnId": "01a01a1b-93ed-7791-a028-85cd1dd37f92",
                "source": "auto",
                "threshold": 10,
                "language": "zh",
            }
        ]

        for invalid in (
            {},
            {
                "sessionId": "../other",
                "turnId": "turn",
                "source": "manual",
                "threshold": 10,
                "language": "en",
            },
            {
                "sessionId": "session",
                "turnId": "../other",
                "source": "manual",
                "threshold": 10,
                "language": "en",
            },
            {
                "sessionId": "session",
                "turnId": "turn",
                "source": "other",
                "threshold": 10,
                "language": "en",
            },
            {
                "sessionId": "session",
                "turnId": "turn",
                "source": "manual",
                "threshold": 0,
                "language": "en",
            },
            {
                "sessionId": "session",
                "turnId": "turn",
                "source": "manual",
                "threshold": True,
                "language": "en",
            },
            {
                "sessionId": "session",
                "turnId": "turn",
                "source": "manual",
                "threshold": 10,
                "language": "en",
                "prompt": "arbitrary text",
            },
        ):
            with pytest.raises(HTTPError) as error:
                post_stop(server, invalid)
            assert error.value.code == 400
        assert len(requested) == 1
    finally:
        server.close()


def test_browser_server_hides_stop_provider_failures_and_absence() -> None:
    value = {
        "sessionId": "session-alpha",
        "turnId": "turn-active",
        "source": "manual",
        "threshold": 10,
        "language": "en",
    }
    server = BrowserViewServer(sample_provider)
    server.start()
    try:
        with pytest.raises(HTTPError) as error:
            post_stop(server, value)
        assert error.value.code == 404
    finally:
        server.close()

    idle_server = BrowserViewServer(sample_provider, lambda _value: {"sent": False, "idle": True})
    idle_server.start()
    try:
        assert post_stop(idle_server, value) == (200, {"sent": False, "idle": True})
    finally:
        idle_server.close()

    stale_server = BrowserViewServer(
        sample_provider,
        lambda _value: {"sent": False, "stale": True, "error": "refresh turn"},
    )
    stale_server.start()
    try:
        assert post_stop(stale_server, value) == (
            200,
            {"sent": False, "stale": True, "error": "refresh turn"},
        )
    finally:
        stale_server.close()

    invalid_server = BrowserViewServer(sample_provider, lambda _value: {"value": "invalid"})
    invalid_server.start()
    try:
        with pytest.raises(HTTPError) as error:
            post_stop(invalid_server, value)
        assert error.value.code == 500
        assert json.loads(error.value.read()) == {"error": "stop unavailable"}
    finally:
        invalid_server.close()

    def failed_stop(_value: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("CDP unavailable")

    failed_server = BrowserViewServer(sample_provider, failed_stop)
    failed_server.start()
    try:
        with pytest.raises(HTTPError) as error:
            post_stop(failed_server, value)
        assert error.value.code == 500
        assert json.loads(error.value.read()) == {"error": "stop unavailable"}
    finally:
        failed_server.close()


def test_browser_server_exposes_only_validated_task_state() -> None:
    requested: list[tuple[str, str | None]] = []

    def task_state_provider(session_id: str, turn_id: str | None) -> dict[str, Any]:
        requested.append((session_id, turn_id))
        return {"running": True, "turnId": "turn-active"}

    server = BrowserViewServer(
        sample_provider,
        task_state_provider=task_state_provider,
    )
    server.start()
    try:
        assert get_task_state(server, "session-alpha", "turn-candidate") == (
            200,
            {"running": True, "turnId": "turn-active"},
        )
        assert requested == [("session-alpha", "turn-candidate")]

        for suffix in ("", "?sessionId=", "?sessionId=../other", "?sessionId=a&extra=1"):
            with pytest.raises(HTTPError) as error:
                urlopen(urljoin(server.url, f"api/task-state{suffix}"), timeout=2)
            assert error.value.code == 400
        assert requested == [("session-alpha", "turn-candidate")]
    finally:
        server.close()

    invalid_server = BrowserViewServer(
        sample_provider,
        task_state_provider=lambda _session_id, _turn_id: {
            "running": False,
            "turnId": "stale",
        },
    )
    invalid_server.start()
    try:
        with pytest.raises(HTTPError) as error:
            get_task_state(invalid_server, "session-alpha")
        assert error.value.code == 500
    finally:
        invalid_server.close()


def test_browser_server_rejects_invalid_routes_hosts_and_bodies() -> None:
    server = BrowserViewServer(sample_provider)
    server.start()
    try:
        for target in (
            urljoin(server.url, "missing"),
            urljoin(server.url, "api/tool"),
        ):
            with pytest.raises(HTTPError) as error:
                urlopen(target, timeout=2)
            assert error.value.code == 404

        parsed = urlparse(server.url)
        wrong_token = f"http://127.0.0.1:{parsed.port}/wrong-token/"
        with pytest.raises(HTTPError) as error:
            urlopen(wrong_token, timeout=2)
        assert error.value.code == 404

        connection = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=2)
        connection.request("GET", parsed.path, headers={"Host": "example.invalid"})
        response = connection.getresponse()
        assert response.status == 400
        assert response.read() == b"Bad request"
        connection.close()

        invalid_requests = (
            (b"{}", "text/plain", 415),
            (b"not json", "application/json", 400),
            (
                b'{"name":"get_codex_trajectory","name":"get_codex_trajectory"}',
                "application/json",
                400,
            ),
            (
                b'{"name":"get_codex_trajectory","arguments":{"maxRecords":NaN}}',
                "application/json",
                400,
            ),
            (b"[]", "application/json", 400),
            (json.dumps({"name": "show_codex_trajectory"}).encode(), "application/json", 400),
            (
                json.dumps({"name": "get_codex_trajectory", "arguments": []}).encode(),
                "application/json",
                400,
            ),
        )
        for body, content_type, expected in invalid_requests:
            request = Request(
                urljoin(server.url, "api/tool"),
                data=body,
                headers={"Content-Type": content_type},
                method="POST",
            )
            with pytest.raises(HTTPError) as error:
                urlopen(request, timeout=2)
            assert error.value.code == expected

        oversized = Request(
            urljoin(server.url, "api/tool"),
            data=b"x" * (MAX_REQUEST_BYTES + 1),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as error:
            urlopen(oversized, timeout=2)
        assert error.value.code == 413

        connection = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=2)
        connection.putrequest("POST", f"{parsed.path}api/tool")
        connection.putheader("Content-Type", "application/json")
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 400
        response.read()
        connection.close()

        connection = http.client.HTTPConnection("127.0.0.1", parsed.port, timeout=2)
        connection.putrequest("POST", f"{parsed.path}api/tool")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Transfer-Encoding", "chunked")
        connection.endheaders()
        response = connection.getresponse()
        assert response.status == 400
        response.read()
        connection.close()
    finally:
        server.close()


def test_browser_server_hides_provider_failures() -> None:
    server = BrowserViewServer(sample_provider)
    server.start()
    try:
        for invalid_result in (OSError("gone"), [], {"value": float("nan")}):
            if isinstance(invalid_result, BaseException):
                server._tool_provider = raising_provider(invalid_result)
            else:
                server._tool_provider = lambda _name, _arguments, result=invalid_result: result
            with pytest.raises(HTTPError) as error:
                post_tool(server, "get_codex_trajectory", {})
            assert error.value.code == 500
            assert json.loads(error.value.read()) == {"error": "tool unavailable"}
    finally:
        server.close()


def test_browser_server_rejects_oversized_or_invalid_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "read_bytes", lambda _path: b"x" * (MAX_ASSET_BYTES + 1))
    with pytest.raises(OSError, match="too large"):
        BrowserViewServer._read_assets()

    def no_marker(path: Path) -> bytes:
        return b"<html></html>" if path.name == "trajectory.html" else b"x"

    monkeypatch.setattr(Path, "read_bytes", no_marker)
    with pytest.raises(OSError, match="marker"):
        BrowserViewServer._read_assets()

    def oversized_sprite(path: Path) -> bytes:
        if path.name == "trajectory.html":
            return b"__WHALE_MINING_SPRITE_DATA_URI__"
        if path.name.endswith(".png"):
            return b"x" * (MAX_ASSET_BYTES + 1)
        return b"x"

    monkeypatch.setattr(Path, "read_bytes", oversized_sprite)
    with pytest.raises(OSError, match="whale-girl"):
        BrowserViewServer._read_assets()

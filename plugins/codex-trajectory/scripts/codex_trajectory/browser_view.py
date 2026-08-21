"""Loopback-only browser view for the optional CDP shortcut."""

from __future__ import annotations

import base64
import json
import re
import secrets
import threading
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from .json_support import strict_json_loads

MAX_ASSET_BYTES = 2 * 1024 * 1024
MAX_REQUEST_BYTES = 64 * 1024
ALLOWED_TOOL_NAMES = frozenset(
    {
        "get_codex_trajectory",
        "get_codex_trajectory_update",
        "get_codex_toolbar_injection_status",
        "set_codex_toolbar_injection",
    }
)

ToolProvider = Callable[[str, dict[str, Any]], dict[str, Any]]
StopProvider = Callable[[dict[str, Any]], dict[str, Any]]
ThemeProvider = Callable[[], dict[str, Any]]
TaskStateProvider = Callable[[str, str | None], dict[str, Any]]
SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
STOP_SOURCES = frozenset({"manual", "auto"})
THEME_SCHEMES = frozenset({"light", "dark"})
THEME_COLOR_KEYS = frozenset(
    {
        "accent",
        "accentSoft",
        "assistant",
        "bg",
        "compaction",
        "danger",
        "line",
        "lineStrong",
        "muted",
        "panel",
        "panel2",
        "reasoning",
        "subagent",
        "success",
        "text",
        "tokenCached",
        "tokenNew",
        "tokenReasoning",
        "tokenVisible",
        "tool",
        "user",
    }
)
CSS_COLOR_PATTERN = re.compile(
    r"^(?:#[0-9A-Fa-f]{3,8}|rgba?\([0-9.,%+\-/ ]+\)|hsla?\([0-9.,%+\-/ ]+\))$"
)

PARENT_CSP = (
    "default-src 'self'; connect-src 'self'; img-src 'self' data:; "
    "script-src 'self'; style-src 'self'; frame-src 'self'; base-uri 'none'; "
    "form-action 'none'; frame-ancestors 'none'"
)
VIEWER_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data:; connect-src 'none'; font-src 'none'; object-src 'none'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'self'"
)


def injection_source(viewer_url: str) -> str:
    """Return the idempotent Codex-shell injection for an in-app Browser link."""
    encoded_url = json.dumps(viewer_url, ensure_ascii=True, allow_nan=False)
    return rf"""
(() => {{
  const GLOBAL = "__codexTrajectoryToolbarV1";
  const BUTTON_ID = "codex-trajectory-toolbar-entry";
  const STYLE_ID = "codex-trajectory-toolbar-style";
  const VERSION = 7;
  const VIEWER_URL = {encoded_url};
  const existing = window[GLOBAL];
  if (existing?.version === VERSION) {{
    existing.setViewerUrl(VIEWER_URL);
    existing.ensure();
    return {{
      installed: true,
      reused: true,
      visible: Boolean(document.getElementById(BUTTON_ID)),
    }};
  }}
  existing?.dispose?.();

  const normalize = value => String(value || "").replace(/\s+/g, " ").trim().toLowerCase();
  const visible = element => {{
    if (!(element instanceof HTMLElement)) return false;
    const rect = element.getBoundingClientRect();
    const style = getComputedStyle(element);
    return rect.width > 0
      && rect.height > 0
      && style.display !== "none"
      && style.visibility !== "hidden";
  }};
  const accessLabels = new Set(["full access", "完全访问", "完全访问权限"]);
  const elementLabels = element => [
    element.getAttribute("aria-label"),
    element.getAttribute("title"),
    element.textContent,
  ].map(normalize).filter(Boolean);
  const findAccessButton = () => Array.from(document.querySelectorAll("button,[role='button']"))
    .find(element => visible(element)
      && elementLabels(element).some(label => accessLabels.has(label)));
  const normalizeSessionId = value => {{
    const normalized = String(value || "");
    const withoutHost = normalized.startsWith("local:") ? normalized.slice(6) : normalized;
    return /^[A-Za-z0-9][A-Za-z0-9._:-]{{0,127}}$/.test(withoutHost)
      ? withoutHost
      : null;
  }};
  const currentSessionId = () => {{
    const selected = document.querySelector(
      '[data-app-action-sidebar-thread-active="true"][data-app-action-sidebar-thread-id], '
        + '[data-app-action-sidebar-thread-selected="true"]'
        + '[data-app-action-sidebar-thread-id]'
    );
    const selectedId = normalizeSessionId(
      selected?.getAttribute("data-app-action-sidebar-thread-id")
    );
    if (!selectedId || !selectedId.startsWith("client-new-thread:")) return selectedId;

    // Codex keeps the client-generated sidebar key after App Server materializes
    // the task. The conversation surface exposes the canonical UUID used by
    // thread/read and turn/interrupt, so never send the temporary key to them.
    const composerId = normalizeSessionId(
      document.querySelector("[data-above-composer-conversation-id]")
        ?.getAttribute("data-above-composer-conversation-id")
    );
    if (composerId && !composerId.startsWith("client-new-thread:")) return composerId;
    const annotations = Array.from(
      document.querySelectorAll("[data-response-annotation-conversation]")
    );
    const annotatedId = normalizeSessionId(
      annotations.at(-1)?.getAttribute("data-response-annotation-conversation")
    );
    return annotatedId && !annotatedId.startsWith("client-new-thread:")
      ? annotatedId
      : null;
  }};

  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = `
    #${{BUTTON_ID}} {{
      appearance: none;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      min-height: 30px;
      margin-inline-start: 4px;
      padding: 4px 8px;
      border: 0;
      border-radius: 7px;
      color: inherit;
      background: transparent;
      font: inherit;
      font-size: 12px;
      line-height: 1;
      text-decoration: none;
      white-space: nowrap;
      cursor: pointer;
      opacity: .82;
    }}
    #${{BUTTON_ID}}:hover {{
      color: inherit;
      background: color-mix(in srgb, currentColor 10%, transparent);
      opacity: 1;
    }}
    #${{BUTTON_ID}}:focus-visible {{ outline: 2px solid #8d99ff; outline-offset: 2px; }}
    #${{BUTTON_ID}} svg {{ width: 15px; height: 15px; flex: none; }}
  `;
  document.head?.append(style);

  let observer = null;
  let activeViewerUrl = VIEWER_URL;
  const prepareLink = link => {{
    const sessionId = currentSessionId();
    const url = new URL(activeViewerUrl);
    url.searchParams.delete("sessionId");
    if (sessionId) url.searchParams.set("sessionId", sessionId);
    const language = document.documentElement.lang || navigator.language;
    if (language) url.searchParams.set("lang", language);
    link.href = url.toString();
    link.setAttribute("aria-disabled", String(!sessionId));
    link.dataset.sessionReady = String(Boolean(sessionId));
    return Boolean(sessionId);
  }};
  const ensure = () => {{
    const access = findAccessButton();
    if (!access) return false;
    let link = document.getElementById(BUTTON_ID);
    if (!link) {{
      link = document.createElement("a");
      link.id = BUTTON_ID;
      link.target = "_blank";
      link.rel = "noopener";
      link.title = "在 Codex 内置浏览器打开当前任务轨迹";
      link.setAttribute("aria-label", "查看轨迹");
      link.innerHTML = `
        <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path d="M4 17.5l4.5-5 3.5 3 5.5-7L20 10" stroke="currentColor"
            stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M4 4v16h16" stroke="currentColor" stroke-width="1.8"
            stroke-linecap="round"/>
        </svg><span>查看轨迹</span>`;
      link.addEventListener("pointerdown", () => prepareLink(link));
      link.addEventListener("click", event => {{
        if (!prepareLink(link)) event.preventDefault();
      }});
    }}
    prepareLink(link);
    if (link.previousElementSibling !== access) access.insertAdjacentElement("afterend", link);
    return true;
  }};
  const setViewerUrl = value => {{
    activeViewerUrl = String(value || VIEWER_URL);
    const link = document.getElementById(BUTTON_ID);
    if (link) prepareLink(link);
  }};
  const dispose = () => {{
    observer?.disconnect();
    observer = null;
    document.getElementById(BUTTON_ID)?.remove();
    document.getElementById(STYLE_ID)?.remove();
    document.getElementById("codex-trajectory-cdp-drawer")?.remove();
    delete window[GLOBAL];
  }};

  observer = new MutationObserver(() => ensure());
  observer.observe(document.documentElement, {{ childList: true, subtree: true }});
  window[GLOBAL] = {{ version: VERSION, ensure, dispose, currentSessionId, setViewerUrl }};
  const inserted = ensure();
  return {{ installed: true, reused: false, visible: inserted }};
}})()
"""


class _LoopbackServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False


class BrowserViewServer:
    """Serve the full trajectory viewer from an unguessable loopback URL."""

    def __init__(
        self,
        tool_provider: ToolProvider,
        stop_provider: StopProvider | None = None,
        theme_provider: ThemeProvider | None = None,
        task_state_provider: TaskStateProvider | None = None,
    ) -> None:
        self._tool_provider = tool_provider
        self._stop_provider = stop_provider
        self._theme_provider = theme_provider
        self._task_state_provider = task_state_provider
        self._token = secrets.token_urlsafe(32)
        self._assets = self._read_assets()
        content_type, browser_script = self._assets["trajectory-browser.js"]
        stop_marker = b'"__CODEX_TRAJECTORY_STOP_BRIDGE__"'
        if stop_marker not in browser_script:
            raise OSError("Browser stop-bridge marker is missing.")
        self._assets["trajectory-browser.js"] = (
            content_type,
            browser_script.replace(
                stop_marker,
                b"true" if stop_provider is not None else b"false",
            ),
        )
        self._server = _LoopbackServer(("127.0.0.1", 0), self._handler_type())
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="codex-trajectory-browser-view",
            daemon=True,
        )

    @staticmethod
    def _read_assets() -> dict[str, tuple[str, bytes]]:
        asset_root = Path(__file__).resolve().parent.parent.parent / "assets"
        assets: dict[str, tuple[str, bytes]] = {}
        for name, content_type in (
            ("trajectory-browser.html", "text/html; charset=utf-8"),
            ("trajectory-browser.css", "text/css; charset=utf-8"),
            ("trajectory-browser.js", "text/javascript; charset=utf-8"),
        ):
            content = (asset_root / name).read_bytes()
            if len(content) > MAX_ASSET_BYTES:
                raise OSError(f"Browser asset is too large: {name}")
            assets[name] = (content_type, content)
        viewer = (asset_root / "trajectory.html").read_bytes()
        sprite = (asset_root / "whale-girl-mining-32f.png").read_bytes()
        if len(viewer) > MAX_ASSET_BYTES:
            raise OSError("Browser asset is too large: trajectory.html")
        if len(sprite) > MAX_ASSET_BYTES:
            raise OSError("Browser asset is too large: whale-girl-mining-32f.png")
        marker = b"__WHALE_MINING_SPRITE_DATA_URI__"
        if marker not in viewer:
            raise OSError("Browser viewer sprite marker is missing.")
        sprite_uri = b"data:image/png;base64," + base64.b64encode(sprite)
        assets["trajectory.html"] = (
            "text/html; charset=utf-8",
            viewer.replace(marker, sprite_uri),
        )
        return assets

    @property
    def url(self) -> str:
        port = int(self._server.server_address[1])
        token = quote(self._token, safe="")
        return f"http://127.0.0.1:{port}/{token}/"

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2.0)

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                owner._handle(self, head_only=False)

            def do_HEAD(self) -> None:
                owner._handle(self, head_only=True)

            def do_POST(self) -> None:
                owner._handle_post(self)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        return Handler

    def _route(self, request: BaseHTTPRequestHandler) -> str | None:
        expected_host = f"127.0.0.1:{self._server.server_address[1]}"
        if request.headers.get("Host") != expected_host:
            self._send(request, HTTPStatus.BAD_REQUEST, "text/plain", b"Bad request", False)
            return None
        parsed = urlparse(request.path)
        prefix = f"/{self._token}/"
        if not parsed.path.startswith(prefix):
            self._send(request, HTTPStatus.NOT_FOUND, "text/plain", b"Not found", False)
            return None
        return parsed.path[len(prefix) :]

    def _handle(self, request: BaseHTTPRequestHandler, *, head_only: bool) -> None:
        route = self._route(request)
        if route is None:
            return
        if route in ("", "index.html"):
            content_type, body = self._assets["trajectory-browser.html"]
            self._send(request, HTTPStatus.OK, content_type, body, head_only)
            return
        if route in ("trajectory-browser.css", "trajectory-browser.js", "trajectory.html"):
            content_type, body = self._assets[route]
            csp = VIEWER_CSP if route == "trajectory.html" else PARENT_CSP
            self._send(request, HTTPStatus.OK, content_type, body, head_only, csp=csp)
            return
        if route == "api/theme":
            self._handle_theme(request, head_only=head_only)
            return
        if route == "api/task-state":
            self._handle_task_state(request, head_only=head_only)
            return
        self._send(request, HTTPStatus.NOT_FOUND, "text/plain", b"Not found", head_only)

    def _handle_theme(
        self,
        request: BaseHTTPRequestHandler,
        *,
        head_only: bool,
    ) -> None:
        if self._theme_provider is None:
            self._send(request, HTTPStatus.NOT_FOUND, "text/plain", b"Not found", head_only)
            return
        try:
            payload = self._theme_provider()
            if not isinstance(payload, dict) or set(payload) != {"scheme", "colors"}:
                raise ValueError("invalid theme result")
            colors = payload.get("colors")
            if (
                payload.get("scheme") not in THEME_SCHEMES
                or not isinstance(colors, dict)
                or set(colors) != THEME_COLOR_KEYS
                or any(
                    not isinstance(value, str)
                    or len(value) > 96
                    or CSS_COLOR_PATTERN.fullmatch(value) is None
                    for value in colors.values()
                )
            ):
                raise ValueError("invalid theme result")
            body = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (OSError, RuntimeError, ValueError):
            self._send_json_error(request, HTTPStatus.INTERNAL_SERVER_ERROR, "theme unavailable")
            return
        self._send(request, HTTPStatus.OK, "application/json", body, head_only)

    def _handle_task_state(
        self,
        request: BaseHTTPRequestHandler,
        *,
        head_only: bool,
    ) -> None:
        if self._task_state_provider is None:
            self._send(request, HTTPStatus.NOT_FOUND, "text/plain", b"Not found", head_only)
            return
        query = parse_qs(urlparse(request.path).query, keep_blank_values=True)
        session_ids = query.get("sessionId")
        turn_ids = query.get("turnId")
        if (
            not {"sessionId"} <= set(query) <= {"sessionId", "turnId"}
            or not isinstance(session_ids, list)
            or len(session_ids) != 1
            or SESSION_ID_PATTERN.fullmatch(session_ids[0]) is None
            or (
                turn_ids is not None
                and (len(turn_ids) != 1 or SESSION_ID_PATTERN.fullmatch(turn_ids[0]) is None)
            )
        ):
            self._send_json_error(request, HTTPStatus.BAD_REQUEST, "invalid task state request")
            return
        try:
            payload = self._task_state_provider(
                session_ids[0],
                turn_ids[0] if turn_ids is not None else None,
            )
            if (
                not isinstance(payload, dict)
                or set(payload) != {"running", "turnId"}
                or not isinstance(payload.get("running"), bool)
                or (
                    payload["running"] is True
                    and (
                        not isinstance(payload.get("turnId"), str)
                        or SESSION_ID_PATTERN.fullmatch(payload["turnId"]) is None
                    )
                )
                or (payload["running"] is False and payload.get("turnId") is not None)
            ):
                raise ValueError("invalid task state result")
            body = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (OSError, RuntimeError, ValueError):
            self._send_json_error(
                request,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "task state unavailable",
            )
            return
        self._send(request, HTTPStatus.OK, "application/json", body, head_only)

    def _handle_post(self, request: BaseHTTPRequestHandler) -> None:
        route = self._route(request)
        if route is None:
            return
        if route not in {"api/tool", "api/stop"}:
            self._send(request, HTTPStatus.NOT_FOUND, "text/plain", b"Not found", False)
            return
        if request.headers.get("Transfer-Encoding"):
            self._send_json_error(request, HTTPStatus.BAD_REQUEST, "invalid request")
            return
        content_type = request.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            self._send_json_error(request, HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "JSON required")
            return
        try:
            length = int(request.headers.get("Content-Length", ""))
        except ValueError:
            self._send_json_error(request, HTTPStatus.BAD_REQUEST, "invalid request")
            return
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._send_json_error(request, HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "invalid request")
            return
        try:
            value = strict_json_loads(request.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json_error(request, HTTPStatus.BAD_REQUEST, "invalid JSON")
            return
        if not isinstance(value, dict):
            self._send_json_error(request, HTTPStatus.BAD_REQUEST, "invalid request")
            return
        if route == "api/stop":
            self._handle_stop(request, value)
            return
        name = value.get("name")
        arguments = value.get("arguments", {})
        if name not in ALLOWED_TOOL_NAMES or not isinstance(arguments, dict):
            self._send_json_error(request, HTTPStatus.BAD_REQUEST, "invalid tool call")
            return
        try:
            payload = self._tool_provider(name, arguments)
            if not isinstance(payload, dict):
                raise ValueError("invalid tool result")
            body = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (OSError, RuntimeError, ValueError):
            self._send_json_error(request, HTTPStatus.INTERNAL_SERVER_ERROR, "tool unavailable")
            return
        self._send(request, HTTPStatus.OK, "application/json", body, False)

    def _handle_stop(
        self,
        request: BaseHTTPRequestHandler,
        value: dict[str, Any],
    ) -> None:
        if self._stop_provider is None:
            self._send_json_error(request, HTTPStatus.NOT_FOUND, "stop bridge unavailable")
            return
        session_id = value.get("sessionId")
        turn_id = value.get("turnId")
        source = value.get("source")
        threshold = value.get("threshold")
        language = value.get("language")
        if (
            not isinstance(session_id, str)
            or SESSION_ID_PATTERN.fullmatch(session_id) is None
            or not isinstance(turn_id, str)
            or SESSION_ID_PATTERN.fullmatch(turn_id) is None
            or source not in STOP_SOURCES
            or isinstance(threshold, bool)
            or not isinstance(threshold, int)
            or not 1 <= threshold <= 100
            or language not in {"en", "zh"}
            or set(value) != {"sessionId", "turnId", "source", "threshold", "language"}
        ):
            self._send_json_error(request, HTTPStatus.BAD_REQUEST, "invalid stop request")
            return
        try:
            payload = self._stop_provider(
                {
                    "sessionId": session_id,
                    "turnId": turn_id,
                    "source": source,
                    "threshold": threshold,
                    "language": language,
                }
            )
            if (
                not isinstance(payload, dict)
                or not isinstance(payload.get("sent"), bool)
                or set(payload) - {"sent", "error", "idle", "stale"}
                or ("error" in payload and not isinstance(payload["error"], str))
                or ("idle" in payload and not isinstance(payload["idle"], bool))
                or ("stale" in payload and not isinstance(payload["stale"], bool))
                or (
                    payload.get("sent") is True
                    and (payload.get("idle") is True or payload.get("stale") is True)
                )
                or (payload.get("idle") is True and payload.get("stale") is True)
                or (
                    payload.get("sent") is False
                    and payload.get("idle") is not True
                    and payload.get("stale") is not True
                    and "error" not in payload
                )
                or (payload.get("stale") is True and "error" not in payload)
            ):
                raise ValueError("invalid stop result")
            body = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (OSError, RuntimeError, ValueError):
            self._send_json_error(request, HTTPStatus.INTERNAL_SERVER_ERROR, "stop unavailable")
            return
        self._send(request, HTTPStatus.OK, "application/json", body, False)

    def _send_json_error(
        self,
        request: BaseHTTPRequestHandler,
        status: HTTPStatus,
        message: str,
    ) -> None:
        body = json.dumps({"error": message}, separators=(",", ":")).encode("utf-8")
        self._send(request, status, "application/json", body, False)

    @staticmethod
    def _send(
        request: BaseHTTPRequestHandler,
        status: HTTPStatus,
        content_type: str,
        body: bytes,
        head_only: bool,
        *,
        csp: str = PARENT_CSP,
    ) -> None:
        request.send_response(status)
        request.send_header("Content-Type", content_type)
        request.send_header("Content-Length", str(len(body)))
        request.send_header("Cache-Control", "no-store")
        request.send_header("X-Content-Type-Options", "nosniff")
        request.send_header("Referrer-Policy", "no-referrer")
        request.send_header("Cross-Origin-Resource-Policy", "same-origin")
        request.send_header("Content-Security-Policy", csp)
        request.end_headers()
        if not head_only:
            request.wfile.write(body)

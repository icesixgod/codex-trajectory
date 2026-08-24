#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["zstandard==0.25.0; python_version < '3.14'"]
# ///
"""Inject the optional Codex Trajectory toolbar entry over loopback CDP."""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib
import json
import math
import os
import re
import secrets
import socket
import stat
import struct
import sys
import time
from contextlib import suppress
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import urlparse

from codex_trajectory.browser_view import BrowserViewServer, injection_source
from codex_trajectory.cdp_peer import (
    HostIdentity,
    PeerAuthenticationError,
    authenticate_connected_peer,
    host_identity_alive,
)
from codex_trajectory.cdp_settings import (
    MCP_STARTED_AT_ENV,
    daemon_runtime_id,
    lock_path,
    read_settings,
    write_daemon_status,
)
from codex_trajectory.json_support import strict_json_loads

POLL_SECONDS = 1.0
CONNECT_TIMEOUT_SECONDS = 0.75
APP_SERVER_COMMAND_TIMEOUT_SECONDS = 10.0
STOP_APP_SERVER_RPC_TIMEOUT_MS = 8_000
STOP_APP_SERVER_COMMAND_TIMEOUT_SECONDS = 34.0
THEME_OPERATION_TIMEOUT_SECONDS = 4.0
TASK_STATE_OPERATION_TIMEOUT_SECONDS = 22.0
STOP_OPERATION_TIMEOUT_SECONDS = 44.0
INJECT_OPERATION_TIMEOUT_SECONDS = 8.0
HANDOFF_LOCK_TIMEOUT_SECONDS = 8.0
HANDOFF_SETTINGS_TIMEOUT_SECONDS = 5.0
MAX_HTTP_BYTES = 512 * 1024
MAX_HTTP_HEADER_BYTES = 64 * 1024
MAX_WEBSOCKET_MESSAGE_BYTES = 8 * 1024 * 1024
HANDOFF_READY_NAME = re.compile(r"\.cdp-handoff-[0-9a-f]{32}\.ready\Z")

REMOVE_SOURCE = r"""
(() => {
  window.__codexTrajectoryToolbarV1?.dispose?.();
  document.getElementById("codex-trajectory-toolbar-entry")?.remove();
  document.getElementById("codex-trajectory-toolbar-style")?.remove();
  document.getElementById("codex-trajectory-cdp-drawer")?.remove();
  return true;
})()
"""


class CdpError(RuntimeError):
    """A bounded local CDP transport failure."""


def _bounded_deadline(deadline: float | None, timeout_seconds: float) -> float:
    local_deadline = time.monotonic() + timeout_seconds
    return min(deadline, local_deadline) if deadline is not None else local_deadline


def _remaining_seconds(deadline: float, message: str) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CdpError(message)
    return remaining


def _set_socket_deadline(sock: socket.socket, deadline: float, message: str) -> None:
    sock.settimeout(_remaining_seconds(deadline, message))


def _recv_until(
    sock: socket.socket,
    marker: bytes,
    limit: int,
    deadline: float,
    *,
    timeout_message: str,
    closed_message: str,
    limit_message: str,
) -> bytes:
    value = bytearray()
    while marker not in value:
        _set_socket_deadline(sock, deadline, timeout_message)
        try:
            chunk = sock.recv(4096)
        except (OSError, TimeoutError) as error:
            raise CdpError(timeout_message) from error
        if not chunk:
            raise CdpError(closed_message)
        value.extend(chunk)
        if len(value) > limit:
            raise CdpError(limit_message)
    return bytes(value)


class WebSocketConnection:
    """Small dependency-free WebSocket client sufficient for local CDP JSON-RPC."""

    def __init__(
        self,
        url: str,
        *,
        expected_port: int | None = None,
        deadline: float | None = None,
        host_identity: HostIdentity | None = None,
    ) -> None:
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as error:
            raise CdpError("CDP returned an invalid WebSocket URL.") from error
        if parsed.scheme != "ws" or hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise CdpError("CDP WebSocket was not loopback-only.")
        if hostname == "::1":
            raise CdpError("CDP WebSocket did not match the IPv4 discovery endpoint.")
        if port is None:
            raise CdpError("CDP WebSocket did not specify a port.")
        if expected_port is not None and port != expected_port:
            raise CdpError("CDP WebSocket did not match the discovery endpoint.")
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or "\r" in parsed.path
            or "\n" in parsed.path
            or "\r" in parsed.query
            or "\n" in parsed.query
        ):
            raise CdpError("CDP returned an invalid WebSocket URL.")
        self._deadline = deadline
        connect_deadline = _bounded_deadline(deadline, CONNECT_TIMEOUT_SECONDS)
        connect_host = "127.0.0.1" if hostname == "localhost" else hostname
        self._socket = socket.create_connection(
            (connect_host, port),
            timeout=_remaining_seconds(connect_deadline, "CDP connection timed out."),
        )
        self._next_id = 1
        try:
            if host_identity is not None:
                try:
                    authenticate_connected_peer(self._socket, host_identity)
                except PeerAuthenticationError as error:
                    raise CdpError("CDP peer authentication failed.") from error
            self._handshake(
                hostname,
                port,
                parsed.path or "/",
                parsed.query,
                connect_deadline,
            )
        except BaseException:
            self._socket.close()
            raise

    def _handshake(
        self,
        host: str,
        port: int,
        path: str,
        query: str,
        deadline: float,
    ) -> None:
        request_path = f"{path}?{query}" if query else path
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        header_host = f"[{host}]" if ":" in host else host
        request = (
            f"GET {request_path} HTTP/1.1\r\n"
            f"Host: {header_host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        _set_socket_deadline(self._socket, deadline, "CDP handshake timed out.")
        self._socket.sendall(request)
        response = self._read_until(b"\r\n\r\n", MAX_HTTP_HEADER_BYTES, deadline)
        header = response.decode("latin-1")
        if not header.startswith("HTTP/1.1 101"):
            raise CdpError("CDP rejected the WebSocket upgrade.")
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        headers = {
            line.split(":", 1)[0].strip().casefold(): line.split(":", 1)[1].strip()
            for line in header.split("\r\n")[1:]
            if ":" in line
        }
        if headers.get("sec-websocket-accept") != expected:
            raise CdpError("CDP returned an invalid WebSocket handshake.")

    def _read_until(self, marker: bytes, limit: int, deadline: float) -> bytes:
        return _recv_until(
            self._socket,
            marker,
            limit,
            deadline,
            timeout_message="CDP handshake timed out.",
            closed_message="CDP closed the connection.",
            limit_message="CDP handshake exceeded the size limit.",
        )

    def _read_exact(self, length: int, deadline: float | None = None) -> bytes:
        value = bytearray()
        while len(value) < length:
            if deadline is not None:
                _set_socket_deadline(self._socket, deadline, "CDP command timed out.")
            try:
                chunk = self._socket.recv(length - len(value))
            except (OSError, TimeoutError) as error:
                raise CdpError("CDP command timed out.") from error
            if not chunk:
                raise CdpError("CDP closed the connection.")
            value.extend(chunk)
        return bytes(value)

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if len(payload) > MAX_WEBSOCKET_MESSAGE_BYTES:
            raise CdpError("CDP request exceeded the size limit.")
        mask = secrets.token_bytes(4)
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", 0x80 | opcode, 0x80 | length)
        elif length <= 0xFFFF:
            header = struct.pack("!BBH", 0x80 | opcode, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", 0x80 | opcode, 0x80 | 127, length)
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self._socket.sendall(header + mask + masked)

    def _receive_message(self, deadline: float | None = None) -> bytes:
        message = bytearray()
        initial_opcode: int | None = None
        while True:
            first, second = self._read_exact(2, deadline)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2, deadline))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8, deadline))[0]
            if length > MAX_WEBSOCKET_MESSAGE_BYTES:
                raise CdpError("CDP response exceeded the size limit.")
            mask = self._read_exact(4, deadline) if masked else b""
            payload = self._read_exact(length, deadline)
            if masked:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            if opcode == 0x8:
                raise CdpError("CDP closed the connection.")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in {0x1, 0x2}:
                initial_opcode = opcode
                message.clear()
            elif opcode != 0x0 or initial_opcode is None:
                raise CdpError("CDP returned an unsupported WebSocket frame.")
            message.extend(payload)
            if len(message) > MAX_WEBSOCKET_MESSAGE_BYTES:
                raise CdpError("CDP response exceeded the size limit.")
            if final:
                if initial_opcode != 0x1:
                    raise CdpError("CDP returned a non-text message.")
                return bytes(message)

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout_seconds: float = CONNECT_TIMEOUT_SECONDS,
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        payload = json.dumps(
            {"id": request_id, "method": method, "params": params or {}},
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        previous_timeout = self._socket.gettimeout()
        deadline = _bounded_deadline(self._deadline, timeout_seconds)
        self._socket.settimeout(_remaining_seconds(deadline, "CDP command timed out."))
        try:
            self._send_frame(0x1, payload)
            while True:
                try:
                    message = strict_json_loads(self._receive_message(deadline).decode("utf-8"))
                except (ValueError, UnicodeDecodeError) as error:
                    raise CdpError("CDP returned invalid JSON.") from error
                if not isinstance(message, dict) or message.get("id") != request_id:
                    continue
                if isinstance(message.get("error"), dict):
                    error_message = str(message["error"].get("message") or "CDP command failed.")
                    raise CdpError(error_message[:500])
                result = message.get("result")
                return result if isinstance(result, dict) else {}
        finally:
            self._socket.settimeout(previous_timeout)

    def close(self) -> None:
        with suppress(CdpError, OSError):
            if self._deadline is not None:
                _set_socket_deadline(
                    self._socket,
                    self._deadline,
                    "CDP connection deadline elapsed.",
                )
            self._send_frame(0x8, b"")
        self._socket.close()

    def __enter__(self) -> WebSocketConnection:
        return self

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


def _http_json(port: int, route: str, *, deadline: float | None = None) -> Any:
    request_deadline = _bounded_deadline(deadline, CONNECT_TIMEOUT_SECONDS)
    sock: socket.socket | None = None
    try:
        sock = socket.create_connection(
            ("127.0.0.1", port),
            timeout=_remaining_seconds(request_deadline, "CDP discovery timed out."),
        )
        _set_socket_deadline(sock, request_deadline, "CDP discovery timed out.")
        request = (
            f"GET {route} HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Accept: application/json\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii")
        sock.sendall(request)
        response = _recv_until(
            sock,
            b"\r\n\r\n",
            MAX_HTTP_HEADER_BYTES,
            request_deadline,
            timeout_message="CDP discovery timed out.",
            closed_message="CDP closed the HTTP connection.",
            limit_message="CDP HTTP headers exceeded the size limit.",
        )
        raw_header, body_prefix = response.split(b"\r\n\r\n", 1)
        lines = raw_header.decode("latin-1").split("\r\n")
        status_parts = lines[0].split(" ", 2)
        if (
            len(status_parts) < 2
            or status_parts[0] not in {"HTTP/1.0", "HTTP/1.1"}
            or status_parts[1] != "200"
        ):
            raise CdpError("Codex CDP endpoint is unavailable.")
        headers: dict[str, list[str]] = {}
        for line in lines[1:]:
            if ":" not in line:
                raise CdpError("CDP returned an invalid HTTP response.")
            name, value = line.split(":", 1)
            normalized_name = name.strip().casefold()
            if not normalized_name:
                raise CdpError("CDP returned an invalid HTTP response.")
            headers.setdefault(normalized_name, []).append(value.strip())
        if headers.get("transfer-encoding"):
            raise CdpError("CDP returned an unsupported HTTP response.")
        content_lengths = headers.get("content-length", [])
        if len(content_lengths) != 1:
            raise CdpError("CDP returned an invalid HTTP response.")
        try:
            body_length = int(content_lengths[0], 10)
        except ValueError as error:
            raise CdpError("CDP returned an invalid HTTP response.") from error
        if body_length < 0:
            raise CdpError("CDP returned an invalid HTTP response.")
        if body_length > MAX_HTTP_BYTES:
            raise CdpError("CDP target list exceeded the size limit.")
        body_buffer = bytearray(body_prefix)
        while len(body_buffer) < body_length:
            _set_socket_deadline(sock, request_deadline, "CDP discovery timed out.")
            try:
                chunk = sock.recv(min(64 * 1024, body_length - len(body_buffer)))
            except (OSError, TimeoutError) as error:
                raise CdpError("CDP discovery timed out.") from error
            if not chunk:
                raise CdpError("CDP closed the HTTP connection.")
            body_buffer.extend(chunk)
        body = bytes(body_buffer[:body_length])
    except CdpError:
        raise
    except (OSError, TimeoutError, UnicodeError, ValueError) as error:
        raise CdpError("Codex CDP endpoint is unavailable.") from error
    finally:
        if sock is not None:
            sock.close()
    try:
        return strict_json_loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise CdpError("CDP endpoint returned invalid JSON.") from error


def _targets(port: int, *, deadline: float | None = None) -> list[dict[str, Any]]:
    value = _http_json(port, "/json/list", deadline=deadline)
    if not isinstance(value, list):
        raise CdpError("CDP target list was invalid.")
    targets: list[dict[str, Any]] = []
    for item in value[:100]:
        if not isinstance(item, dict):
            continue
        websocket = item.get("webSocketDebuggerUrl")
        target_type = item.get("type")
        if isinstance(websocket, str) and target_type in {"page", "iframe", "webview"}:
            targets.append(item)
    return targets


def _is_codex_shell_target(target: dict[str, Any]) -> bool:
    """Limit injected secrets and app control to Codex-owned renderer targets."""
    url = target.get("url")
    return isinstance(url, str) and (
        url == "app://-/index.html" or url.startswith("app://-/index.html?")
    )


def _valid_cdp_identifier(value: Any) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        return False
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    allowed = f"{alphabet}._:-"
    return value[0] in alphabet and all(character in allowed for character in value)


def _runtime_value(result: dict[str, Any]) -> Any:
    remote = result.get("result")
    return remote.get("value") if isinstance(remote, dict) else None


def _evaluate(
    connection: WebSocketConnection,
    expression: str,
    *,
    user_gesture: bool = False,
    timeout_seconds: float = CONNECT_TIMEOUT_SECONDS,
) -> Any:
    result = connection.call(
        "Runtime.evaluate",
        {
            "expression": expression,
            "awaitPromise": True,
            "returnByValue": True,
            "userGesture": user_gesture,
        },
        timeout_seconds=timeout_seconds,
    )
    return _runtime_value(result)


def _browser_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Dispatch one allowlisted full-view call through the local Browser bridge."""
    from codex_trajectory.projection import call_tool

    return call_tool(name, arguments)


CODEX_THEME_SOURCE = r"""
(() => {
  const root = document.documentElement;
  const styles = getComputedStyle(root);
  const scheme = root.classList.contains("electron-light")
    ? "light"
    : root.classList.contains("electron-dark")
      ? "dark"
      : styles.colorScheme.split(/\s+/).includes("dark")
        ? "dark"
        : "light";
  const fallback = scheme === "dark" ? {
    bg: "#141414", panel: "#181818", panel2: "rgb(40, 40, 40)",
    line: "rgba(255, 255, 255, 0.084)", lineStrong: "rgba(255, 255, 255, 0.156)",
    text: "#dfdfdf", muted: "rgba(255, 255, 255, 0.498)",
    accent: "rgb(131, 195, 255)", accentSoft: "#0d273f",
    danger: "#ff6764", success: "#40c977", user: "#339cff",
    assistant: "#ad7bf9", reasoning: "#ad7bf9", tool: "#ff8549",
    subagent: "#40c977", compaction: "rgba(255, 255, 255, 0.498)",
    tokenNew: "#339cff", tokenCached: "#40c977",
    tokenVisible: "#ad7bf9", tokenReasoning: "#ad7bf9",
  } : {
    bg: "#f7f7f7", panel: "#ffffff", panel2: "rgb(242, 242, 242)",
    line: "rgba(0, 0, 0, 0.084)", lineStrong: "rgba(0, 0, 0, 0.156)",
    text: "#202020", muted: "rgba(0, 0, 0, 0.55)",
    accent: "rgb(0, 102, 204)", accentSoft: "#e6f2ff",
    danger: "#d93025", success: "#16833b", user: "#006acc",
    assistant: "#7847c7", reasoning: "#7847c7", tool: "#b65d00",
    subagent: "#16833b", compaction: "rgba(0, 0, 0, 0.55)",
    tokenNew: "#006acc", tokenCached: "#16833b",
    tokenVisible: "#7847c7", tokenReasoning: "#7847c7",
  };
  const properties = {
    bg: "--color-token-bg-primary",
    panel: "--color-token-main-surface-primary",
    panel2: "--color-background-editor-opaque",
    line: "--color-border",
    lineStrong: "--color-border-heavy",
    text: "--color-token-text-primary",
    muted: "--color-text-foreground-tertiary",
    accent: "--color-text-accent",
    accentSoft: "--color-background-accent",
    danger: "--color-accent-red",
    success: "--color-accent-green",
    user: "--color-accent-blue",
    assistant: "--color-accent-purple",
    reasoning: "--color-accent-purple",
    tool: "--color-icon-warning",
    subagent: "--color-accent-green",
    compaction: "--color-text-foreground-tertiary",
    tokenNew: "--color-accent-blue",
    tokenCached: "--color-accent-green",
    tokenVisible: "--color-accent-purple",
    tokenReasoning: "--color-accent-purple",
  };
  const colors = Object.fromEntries(Object.entries(properties).map(([key, property]) => [
    key,
    styles.getPropertyValue(property).trim() || fallback[key],
  ]));
  return {scheme, colors};
})()
"""


def _read_codex_theme(port: int) -> dict[str, Any]:
    """Read the effective Codex palette from the selected app renderer."""
    deadline = time.monotonic() + THEME_OPERATION_TIMEOUT_SECONDS
    for target in _targets(port, deadline=deadline):
        if time.monotonic() >= deadline:
            break
        if not _is_codex_shell_target(target):
            continue
        websocket = target.get("webSocketDebuggerUrl")
        if not isinstance(websocket, str):
            continue
        try:
            with WebSocketConnection(
                websocket,
                expected_port=port,
                deadline=deadline,
            ) as connection:
                value = _evaluate(connection, CODEX_THEME_SOURCE)
        except (CdpError, OSError):
            continue
        if isinstance(value, dict):
            return value
    raise CdpError("Could not read the active Codex theme.")


def _browser_theme() -> dict[str, Any]:
    settings = read_settings()
    return _read_codex_theme(int(settings["port"]))


def _task_state_source(session_id: str, candidate_turn_id: str | None = None) -> str:
    """Build a read-only App Server query for the bound task's active turn."""
    encoded_session_id = json.dumps(session_id, ensure_ascii=True, allow_nan=False)
    encoded_turn_id = json.dumps(candidate_turn_id, ensure_ascii=True, allow_nan=False)
    return rf"""
(async () => {{
  const EXPECTED_SESSION_ID = {encoded_session_id};
  const CANDIDATE_TURN_ID = {encoded_turn_id};
  const HOST_ID = "local";
  const bridge = window.electronBridge;
  if (typeof bridge?.sendMessageFromView !== "function") {{
    return {{ matched: true, reason: "bridge-unavailable" }};
  }}
  const callAppServer = (method, params) => new Promise((resolve, reject) => {{
    const requestId = "codex-trajectory-state-"
      + `${{Date.now()}}-${{Math.random().toString(36).slice(2)}}`;
    const timeoutMs = 3000;
    const cleanup = () => {{
      clearTimeout(timeout);
      window.removeEventListener("message", onMessage);
    }};
    const onMessage = event => {{
      const value = event.data;
      if (
        value?.type !== "mcp-response"
        || value?.hostId !== HOST_ID
        || value?.message?.id !== requestId
      ) return;
      cleanup();
      if (value.message.error) {{
        reject(new Error("app-server-error"));
        return;
      }}
      resolve(value.message.result);
    }};
    const timeout = setTimeout(() => {{
      cleanup();
      reject(new Error("app-server-timeout"));
    }}, timeoutMs);
    window.addEventListener("message", onMessage);
    bridge.sendMessageFromView({{
      type: "mcp-request",
      hostId: HOST_ID,
      request: {{ id: requestId, method, params }},
      priority: "critical",
      source: "thread",
      timeoutMs,
      expiresAtMs: Date.now() + timeoutMs,
    }}).catch(error => {{
      cleanup();
      reject(error);
    }});
  }});
  try {{
    const read = await callAppServer("thread/read", {{
      threadId: EXPECTED_SESSION_ID,
      includeTurns: false,
    }});
    if (read?.thread?.id !== EXPECTED_SESSION_ID) {{
      return {{ matched: true, reason: "thread-mismatch" }};
    }}
    if (read.thread.status?.type !== "active") {{
      return {{ matched: true, running: false, turnId: null }};
    }}
    const activeTurn = CANDIDATE_TURN_ID;
    return {{
      matched: true,
      running: Boolean(activeTurn),
      turnId: activeTurn || null,
    }};
  }} catch {{
    return {{ matched: true, reason: "app-server-error" }};
  }}
}})()
"""


def _read_active_task_state(
    port: int,
    session_id: str,
    candidate_turn_id: str | None = None,
) -> dict[str, Any]:
    """Return the active App Server turn state for the bound Codex task."""
    if candidate_turn_id is not None and not _valid_cdp_identifier(candidate_turn_id):
        raise CdpError("The bound Codex turn candidate was invalid.")
    bound_candidate = candidate_turn_id or f"codex-trajectory-probe-{secrets.token_hex(16)}"
    source = _task_state_source(session_id, bound_candidate)
    deadline = time.monotonic() + TASK_STATE_OPERATION_TIMEOUT_SECONDS
    matched_reason: str | None = None
    for target in _targets(port, deadline=deadline):
        if time.monotonic() >= deadline:
            break
        if not _is_codex_shell_target(target):
            continue
        websocket = target.get("webSocketDebuggerUrl")
        if not isinstance(websocket, str):
            continue
        try:
            with WebSocketConnection(
                websocket,
                expected_port=port,
                deadline=deadline,
            ) as connection:
                value = _evaluate(
                    connection,
                    source,
                    timeout_seconds=APP_SERVER_COMMAND_TIMEOUT_SECONDS,
                )
        except (CdpError, OSError):
            continue
        if not isinstance(value, dict):
            continue
        if value.get("matched") is not True:
            continue
        if isinstance(value.get("running"), bool):
            turn_id = value.get("turnId")
            if value["running"] is True and not isinstance(turn_id, str):
                continue
            if value["running"] is False:
                turn_id = None
            return {"running": value["running"], "turnId": turn_id}
        # A Codex process can expose more than one page/iframe/webview target.
        # An auxiliary renderer may lack the App Server bridge even when a later
        # shell target is healthy, so retain the best error and keep searching.
        if matched_reason is None:
            matched_reason = str(value.get("reason") or "task-state-unavailable")
    if matched_reason is not None:
        raise CdpError(f"Could not read the bound Codex task state: {matched_reason}.")
    raise CdpError("Could not reach the bound Codex task.")


def _browser_task_state(
    session_id: str,
    candidate_turn_id: str | None = None,
) -> dict[str, Any]:
    settings = read_settings()
    return _read_active_task_state(int(settings["port"]), session_id, candidate_turn_id)


def _stop_request_source(request: dict[str, Any]) -> str:
    session_id = request["sessionId"]
    encoded_session_id = json.dumps(session_id, ensure_ascii=True, allow_nan=False)
    return rf"""
(async () => {{
  const EXPECTED_SESSION_ID = {encoded_session_id};
  const EXPECTED_TURN_ID = {json.dumps(request["turnId"], ensure_ascii=True, allow_nan=False)};
  const HOST_ID = "local";
  const bridge = window.electronBridge;
  if (typeof bridge?.sendMessageFromView !== "function") {{
    return {{ matched: true, sent: false, reason: "bridge-unavailable" }};
  }}
  const callAppServer = (method, params) => new Promise((resolve, reject) => {{
    const requestId = `codex-trajectory-${{Date.now()}}-${{Math.random().toString(36).slice(2)}}`;
    const timeoutMs = {STOP_APP_SERVER_RPC_TIMEOUT_MS};
    const cleanup = () => {{
      clearTimeout(timeout);
      window.removeEventListener("message", onMessage);
    }};
    const onMessage = event => {{
      const value = event.data;
      if (
        value?.type !== "mcp-response"
        || value?.hostId !== HOST_ID
        || value?.message?.id !== requestId
      ) return;
      cleanup();
      if (value.message.error) {{
        const rpcError = value.message.error;
        const error = new Error(
          typeof rpcError?.message === "string" ? rpcError.message : "app-server-error"
        );
        error.code = rpcError?.code;
        error.method = method;
        reject(error);
        return;
      }}
      resolve(value.message.result);
    }};
    const timeout = setTimeout(() => {{
      cleanup();
      reject(new Error("app-server-timeout"));
    }}, timeoutMs);
    window.addEventListener("message", onMessage);
    bridge.sendMessageFromView({{
      type: "mcp-request",
      hostId: HOST_ID,
      request: {{ id: requestId, method, params }},
      priority: "critical",
      source: "thread",
      timeoutMs,
      expiresAtMs: Date.now() + timeoutMs,
    }}).catch(error => {{
      cleanup();
      reject(error);
    }});
  }});
  const failureReason = (error, stage) => {{
    const detail = `${{error?.code || ""}} ${{error?.message || error || ""}}`.toLowerCase();
    if (detail.includes("app-server-timeout")) return "app-server-timeout";
    if (
      (stage === "read" || stage === "goal-read")
      && (detail.includes("thread not found") || detail.includes("session not found"))
    ) return "thread-mismatch";
    if (
      stage === "interrupt"
      && (
        detail.includes("no active turn")
        || detail.includes("expected active turn id")
        || detail.includes("expected turn mismatch")
        || detail.includes("turn not found")
        || detail.includes("already completed")
      )
    ) return "task-idle";
    if (stage === "goal-read") return "goal-state-error";
    if (stage === "goal-set") return "goal-pause-error";
    return "app-server-error";
  }};
  const activeTurnIdFromError = error => {{
    const message = String(error?.message || error || "");
    const match = message.match(/ but found `?([^`]+)`?$/i);
    const value = match?.[1];
    return typeof value === "string"
      && value.length >= 1
      && value.length <= 128
      && /^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(value)
      ? value
      : null;
  }};
  const pauseActiveGoal = async () => {{
    let response;
    try {{
      response = await callAppServer("thread/goal/get", {{
        threadId: EXPECTED_SESSION_ID,
      }});
    }} catch (error) {{
      const detail = `${{error?.code || ""}} ${{error?.message || error || ""}}`.toLowerCase();
      if (
        detail.includes("method not found")
        || detail.includes("unknown method")
        || detail.includes("not implemented")
      ) return {{ paused: false, reason: null }};
      return {{ paused: false, reason: failureReason(error, "goal-read") }};
    }}
    const goal = response?.goal;
    if (goal == null) return {{ paused: false, reason: null }};
    if (goal.threadId !== EXPECTED_SESSION_ID) {{
      return {{ paused: false, reason: "thread-mismatch" }};
    }}
    if (goal.status !== "active") return {{ paused: false, reason: null }};
    try {{
      const updated = await callAppServer("thread/goal/set", {{
        threadId: EXPECTED_SESSION_ID,
        status: "paused",
      }});
      if (
        updated?.goal?.threadId !== EXPECTED_SESSION_ID
        || updated?.goal?.status !== "paused"
      ) return {{ paused: false, reason: "goal-pause-error" }};
      return {{ paused: true, reason: null }};
    }} catch (error) {{
      return {{ paused: false, reason: failureReason(error, "goal-set") }};
    }}
  }};
  const readTaskStatus = async () => {{
    const read = await callAppServer("thread/read", {{
      threadId: EXPECTED_SESSION_ID,
      includeTurns: false,
    }});
    if (read?.thread?.id !== EXPECTED_SESSION_ID) {{
      return {{ reason: "thread-mismatch", running: false }};
    }}
    return {{ reason: null, running: read.thread.status?.type === "active" }};
  }};
  const goal = await pauseActiveGoal();
  if (goal.reason) {{
    return {{ matched: true, sent: false, reason: goal.reason }};
  }}
  try {{
    await callAppServer("turn/interrupt", {{
      threadId: EXPECTED_SESSION_ID,
      turnId: EXPECTED_TURN_ID,
    }});
    return {{ matched: true, sent: true, goalPaused: goal.paused }};
  }} catch (error) {{
    const interruptReason = failureReason(error, "interrupt");
    const activeTurnId = activeTurnIdFromError(error);
    if (interruptReason === "task-idle" && activeTurnId) {{
      return {{
        matched: true,
        sent: false,
        reason: "turn-stale",
        activeTurnId,
      }};
    }}
    try {{
      const after = await readTaskStatus();
      if (after.reason === "thread-mismatch") {{
        return {{ matched: true, sent: false, reason: "thread-mismatch" }};
      }}
      if (!after.running) {{
        return {{ matched: true, sent: false, reason: "task-idle" }};
      }}
      if (interruptReason === "task-idle") {{
        return {{ matched: true, sent: false, reason: "turn-stale" }};
      }}
    }} catch {{
      // Preserve the original interrupt failure when verification is unavailable.
    }}
    return {{ matched: true, sent: false, reason: interruptReason }};
  }}
}})()
"""


def _request_active_task_stop(port: int, request: dict[str, Any]) -> dict[str, Any]:
    """Pause any active Goal, then interrupt the bound Codex turn over loopback CDP."""
    deadline = time.monotonic() + STOP_OPERATION_TIMEOUT_SECONDS
    active_request = request
    rebound = False
    matched_reason: str | None = None
    for target in _targets(port, deadline=deadline):
        if time.monotonic() >= deadline:
            break
        if not _is_codex_shell_target(target):
            continue
        websocket = target.get("webSocketDebuggerUrl")
        if not isinstance(websocket, str):
            continue
        try:
            connection = WebSocketConnection(
                websocket,
                expected_port=port,
                deadline=deadline,
            )
        except (CdpError, OSError):
            continue
        try:
            while time.monotonic() < deadline:
                try:
                    value = _evaluate(
                        connection,
                        _stop_request_source(active_request),
                        user_gesture=True,
                        timeout_seconds=STOP_APP_SERVER_COMMAND_TIMEOUT_SECONDS,
                    )
                except (CdpError, OSError):
                    break
                if not isinstance(value, dict):
                    break
                if value.get("sent") is True:
                    return {"sent": True}
                reason = str(value.get("reason") or "stop-unavailable")
                next_turn_id = value.get("activeTurnId")
                if (
                    reason == "turn-stale"
                    and not rebound
                    and _valid_cdp_identifier(next_turn_id)
                    and next_turn_id != active_request["turnId"]
                ):
                    active_request = {**active_request, "turnId": next_turn_id}
                    rebound = True
                    continue
                if value.get("matched") is True and (
                    matched_reason is None or reason == "task-idle"
                ):
                    # An idle result from a healthy renderer is more actionable
                    # than an earlier auxiliary-renderer bridge error.
                    matched_reason = reason
                break
        finally:
            connection.close()
    if matched_reason == "task-idle":
        return {"sent": False, "idle": True}
    errors = {
        "bridge-unavailable": "The Codex App Server bridge is unavailable.",
        "thread-mismatch": "The bound Codex task could not be verified.",
        "turn-stale": "The task advanced to a newer turn; refresh and retry stopping it.",
        "goal-state-error": "The Codex App Server could not read this task's Goal state.",
        "goal-pause-error": "The Codex App Server could not pause the active Goal.",
        "app-server-timeout": "The Codex App Server timed out while stopping this task.",
        "app-server-error": "The Codex App Server could not interrupt the active turn.",
    }
    if matched_reason is not None:
        result: dict[str, Any] = {
            "sent": False,
            "error": errors.get(matched_reason, "The Codex stop bridge is unavailable."),
        }
        if matched_reason == "turn-stale":
            # No safe replacement turn id was present in the App Server error.
            result["stale"] = True
        return result
    return {"sent": False, "error": "Could not reach the bound Codex task."}


def _browser_stop(request: dict[str, Any]) -> dict[str, Any]:
    settings = read_settings()
    if settings["enabled"] is not True:
        return {
            "sent": False,
            "error": "Direct stop requires the experimental loopback CDP integration.",
        }
    return _request_active_task_stop(int(settings["port"]), request)


def request_task_stop(request: dict[str, Any]) -> dict[str, Any]:
    """Stop one bound task for the app-only MCP bridge, rebinding one stale turn."""
    settings = read_settings()
    if settings["enabled"] is not True:
        return {
            "sent": False,
            "error": "Direct stop requires the experimental loopback CDP integration.",
        }
    port = int(settings["port"])
    candidate = request.get("turnId")
    if not isinstance(candidate, str):
        try:
            state = _read_active_task_state(port, request["sessionId"])
        except (CdpError, OSError):
            return {"sent": False, "error": "Could not reach the bound Codex task."}
        if state.get("running") is not True:
            return {"sent": False, "idle": True}
        candidate = state.get("turnId")
        if not isinstance(candidate, str):
            return {"sent": False, "error": "Could not identify the active Codex turn."}
        request = {**request, "turnId": candidate}
    try:
        return _request_active_task_stop(port, request)
    except (CdpError, OSError):
        return {"sent": False, "error": "Could not reach the bound Codex task."}


def _inject_cycle(
    port: int,
    enabled: bool,
    viewer_url: str | None = None,
    host_identity: HostIdentity | None = None,
    *,
    fast: bool = False,
) -> tuple[bool, bool]:
    """Inject only after binding the token-bearing connection to the Codex host."""
    connected = False
    injected = False
    shell_target_seen = False
    last_shell_error: Exception | None = None
    if enabled and (not viewer_url or host_identity is None):
        raise ValueError("An authenticated host and viewer URL are required for the shortcut.")
    source = injection_source(viewer_url or "") if enabled else REMOVE_SOURCE
    deadline = time.monotonic() + INJECT_OPERATION_TIMEOUT_SECONDS
    target_items = sorted(
        _targets(port, deadline=deadline),
        key=lambda target: 0 if _is_codex_shell_target(target) else 1,
    )
    for target in target_items:
        if time.monotonic() >= deadline:
            break
        websocket = target.get("webSocketDebuggerUrl")
        if not isinstance(websocket, str):
            continue
        codex_shell = _is_codex_shell_target(target)
        shell_target_seen = shell_target_seen or codex_shell
        target_source = source if not enabled or codex_shell else REMOVE_SOURCE
        try:
            connection = WebSocketConnection(
                websocket,
                expected_port=port,
                deadline=deadline,
                host_identity=host_identity if enabled and codex_shell else None,
            )
        except (CdpError, OSError) as error:
            if enabled and codex_shell:
                last_shell_error = error
            continue
        try:
            if codex_shell:
                connected = True
            value = _evaluate(connection, target_source)
            if enabled and codex_shell:
                injected = injected or (
                    isinstance(value, dict)
                    and value.get("installed") is True
                    and value.get("visible") is True
                )
                if fast and injected:
                    # Make the authenticated shortcut visible first. Legacy
                    # cleanup in auxiliary renderers runs on the next cycle.
                    break
        except CdpError as error:
            if enabled and codex_shell:
                last_shell_error = error
            continue
        finally:
            connection.close()
    if enabled and shell_target_seen and not connected and last_shell_error is not None:
        raise CdpError("Could not authenticate a Codex shell target.") from last_shell_error
    return connected, injected if enabled else False


def _mcp_started_at() -> float | None:
    """Read one inherited, non-secret process timestamp for local diagnostics."""
    raw = os.environ.get(MCP_STARTED_AT_ENV)
    try:
        value = float(raw) if raw is not None else float("nan")
    except ValueError:
        return None
    now = time.time()
    return value if math.isfinite(value) and 0 <= value <= now + 60 else None


def _acquire_process_lock(path: Path) -> Any:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        return None
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        return None
    try:
        opened = os.fstat(descriptor)
        linked = os.lstat(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(linked.st_mode)
            or opened.st_dev != linked.st_dev
            or opened.st_ino != linked.st_ino
            or opened.st_nlink != 1
        ):
            raise OSError("Refusing an unsafe CDP watcher lock file.")
        fchmod: Any = vars(os).get("fchmod")
        if callable(fchmod):
            with suppress(OSError):
                fchmod(descriptor, 0o600)
        stream = os.fdopen(descriptor, "r+b")
        descriptor = -1
        if os.name == "nt":
            msvcrt: Any = importlib.import_module("msvcrt")

            stream.seek(0)
            if stream.read(1) == b"":
                stream.seek(0)
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl: Any = importlib.import_module("fcntl")

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        else:
            stream.close()
        return None
    stream.seek(0)
    stream.truncate()
    # Byte zero remains the cross-process lock. Keeping the PID after it lets
    # Windows supervisors read and verify the owner without touching the
    # byte range protected by msvcrt.locking.
    stream.write(b"0" + str(os.getpid()).encode("ascii"))
    stream.flush()
    return stream


def _wait_for_process_lock(path: Path) -> Any:
    """Wait briefly for an outdated watcher to release the shared lock."""
    deadline = time.monotonic() + HANDOFF_LOCK_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        lock = _acquire_process_lock(path)
        if lock is not None:
            return lock
        time.sleep(0.05)
    return None


def _validated_handoff_ready_path(raw: str) -> Path:
    """Constrain a supervisor-provided marker to the private plugin state dir."""
    candidate = Path(raw)
    expected_parent = lock_path().parent.resolve()
    if candidate.parent.resolve() != expected_parent or not HANDOFF_READY_NAME.fullmatch(
        candidate.name
    ):
        raise ValueError("Invalid CDP watcher handoff marker.")
    return candidate


def _write_handoff_ready(raw: str) -> None:
    """Signal viewer readiness without persisting its bearer-token URL."""
    path = _validated_handoff_ready_path(raw)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise OSError("Refusing an unsafe CDP watcher state directory.")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        opened = os.fstat(descriptor)
        linked = os.lstat(path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(linked.st_mode)
            or opened.st_dev != linked.st_dev
            or opened.st_ino != linked.st_ino
            or opened.st_nlink != 1
        ):
            raise OSError("Refusing an unsafe CDP watcher handoff marker.")
        payload = daemon_runtime_id().encode("ascii")
        if os.write(descriptor, payload) != len(payload):
            raise OSError("Could not write the CDP watcher handoff marker.")
        os.fsync(descriptor)
    except BaseException:
        with suppress(OSError):
            path.unlink()
        raise
    finally:
        os.close(descriptor)


def _wait_for_restored_settings() -> None:
    """Avoid observing the supervisor's momentary disable during a handoff."""
    deadline = time.monotonic() + HANDOFF_SETTINGS_TIMEOUT_SECONDS
    while read_settings()["enabled"] is not True and time.monotonic() < deadline:
        time.sleep(0.05)


def watch(
    host_identity: HostIdentity | None = None,
    *,
    wait_for_lock: bool = False,
    handoff_ready: str | None = None,
) -> int:
    """Apply the configured injection until the user disables it."""
    watcher_started_at = time.time()
    mcp_started_at = _mcp_started_at()
    last_error: str | None = None
    viewer_server: BrowserViewServer | None = None
    viewer_ready_at: float | None = None
    injected_at: float | None = None
    injection_duration_ms: float | None = None
    applied_port: int | None = None
    lock = None
    try:
        if handoff_ready is not None:
            settings = read_settings()
            if settings["enabled"] is not True or host_identity is None:
                return 0
            if not host_identity_alive(host_identity):
                return 0
            viewer_server = BrowserViewServer(
                _browser_tool,
                _browser_stop,
                _browser_theme,
                _browser_task_state,
            )
            viewer_server.start()
            viewer_ready_at = time.time()
            _write_handoff_ready(handoff_ready)

        lock = (
            _wait_for_process_lock(lock_path())
            if wait_for_lock
            else _acquire_process_lock(lock_path())
        )
        if lock is None:
            return 0
        if handoff_ready is not None:
            _wait_for_restored_settings()

        while True:
            settings = read_settings()
            enabled = settings["enabled"] is True
            port = int(settings["port"])
            if host_identity is not None and not host_identity_alive(host_identity):
                return 0
            connected = False
            injected = False
            try:
                if applied_port is not None and applied_port != port:
                    with suppress(CdpError, OSError, ValueError):
                        _inject_cycle(applied_port, False)
                    applied_port = None
                if enabled and host_identity is not None and viewer_server is None:
                    viewer_server = BrowserViewServer(
                        _browser_tool,
                        _browser_stop,
                        _browser_theme,
                        _browser_task_state,
                    )
                    viewer_server.start()
                    viewer_ready_at = time.time()
                viewer_url = viewer_server.url if viewer_server is not None else None
                injection_started = time.perf_counter()
                try:
                    connected, injected = _inject_cycle(
                        port,
                        enabled,
                        viewer_url,
                        host_identity,
                        fast=enabled and injected_at is None,
                    )
                finally:
                    injection_duration_ms = round(
                        (time.perf_counter() - injection_started) * 1000,
                        3,
                    )
                if injected and injected_at is None:
                    injected_at = time.time()
                applied_port = port if enabled else None
                last_error = None
            except (CdpError, OSError, ValueError) as error:
                last_error = str(error)[:500]
            write_daemon_status(
                {
                    "pid": os.getpid(),
                    "connected": connected,
                    "injected": injected,
                    "viewerServing": enabled and viewer_server is not None,
                    "lastError": last_error,
                    "runtimeId": daemon_runtime_id(),
                    "mcpStartedAt": mcp_started_at,
                    "watcherStartedAt": watcher_started_at,
                    "viewerReadyAt": viewer_ready_at,
                    "injectedAt": injected_at,
                    "injectionDurationMs": injection_duration_ms,
                }
            )
            if not enabled:
                return 0
            time.sleep(POLL_SECONDS)
    finally:
        if applied_port is not None:
            with suppress(CdpError, OSError, ValueError):
                _inject_cycle(applied_port, False)
        if viewer_server is not None:
            viewer_server.close()
        if lock is not None:
            lock.close()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--watch", action="store_true", help="Watch settings and keep injection live."
    )
    parser.add_argument("--host-identity", help=argparse.SUPPRESS)
    parser.add_argument("--wait-for-lock", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--handoff-ready", help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if not args.watch:
        raise SystemExit("Use --watch; the trajectory page manages this process.")
    try:
        host_identity = (
            HostIdentity.decode(args.host_identity) if args.host_identity is not None else None
        )
    except ValueError as error:
        raise SystemExit("Invalid Codex host identity.") from error
    handoff_requested = args.handoff_ready is not None
    if args.wait_for_lock != handoff_requested:
        raise SystemExit("Invalid CDP watcher handoff arguments.")
    if args.handoff_ready is not None and host_identity is None:
        raise SystemExit("A Codex host identity is required for watcher handoff.")
    return watch(
        host_identity,
        wait_for_lock=args.wait_for_lock,
        handoff_ready=args.handoff_ready,
    )


if __name__ == "__main__":
    raise SystemExit(main())

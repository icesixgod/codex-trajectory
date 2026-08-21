"""Deterministic HTTP harness for the trajectory app resource."""

from __future__ import annotations

import argparse
import json
import threading
from base64 import b64encode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

ASSET_DIRECTORY = Path(__file__).parents[1] / "assets"


def app_resource_html() -> str:
    """Inline the production sprite asset exactly as the MCP resource does."""
    content = (ASSET_DIRECTORY / "trajectory.html").read_text(encoding="utf-8")
    sprite = b64encode((ASSET_DIRECTORY / "whale-girl-mining-32f.png").read_bytes()).decode("ascii")
    return content.replace(
        "__WHALE_MINING_SPRITE_DATA_URI__",
        f"data:image/png;base64,{sprite}",
    )


def _record(
    index: int,
    turn: int,
    kind: str,
    event: str,
    summary: str,
    offset_ms: int,
    duration_ms: int | None = 0,
    *,
    status: str = "complete",
    call_id: str | None = None,
    input_detail: str | None = None,
    output_detail: str | None = None,
    usage_detail: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Build one stable demo record."""
    base_ms = 1_786_665_601_000
    started_ms = base_ms + offset_ms
    usage = (
        usage_detail
        if usage_detail is not None
        else (
            {"input_tokens": 64, "output_tokens": 12, "total_tokens": 76}
            if kind == "assistant"
            else None
        )
    )
    return {
        "index": index,
        "id": f"record-{turn}-{index}",
        "turn": turn,
        "step": max(1, index - (turn - 1) * 5),
        "kind": kind,
        "event": event,
        "summary": summary,
        "status": status,
        "callId": call_id,
        "startedAt": _iso(started_ms),
        "completedAt": _iso(started_ms + duration_ms)
        if duration_ms is not None
        else _iso(started_ms),
        "durationMs": duration_ms,
        "input": input_detail,
        "output": output_detail,
        "error": "Expected command failure" if status == "error" else None,
        "usage": usage,
        "metadata": {"protocolType": "function_call"} if kind == "tool" else {},
    }


def _iso(milliseconds: int) -> str:
    """Format a fixed 2026-08-14 UTC fixture timestamp."""
    seconds, millis = divmod(milliseconds, 1000)
    from datetime import datetime, timezone

    value = datetime.fromtimestamp(seconds, timezone.utc)
    return f"{value:%Y-%m-%dT%H:%M:%S}.{millis:03d}Z"


def _aggregate_usage(items: list[dict[str, int]]) -> dict[str, int] | None:
    """Sum the flat numeric usage counters used by the UI fixture."""
    if not items:
        return None
    result: dict[str, int] = {}
    for item in items:
        for key, value in item.items():
            result[key] = result.get(key, 0) + value
    return result


def demo_trajectories() -> dict[str, dict[str, Any]]:
    """Return synthetic full-detail trajectories for UI acceptance tests."""
    hostile = '<img src=x onerror="window.__trajectoryXss=true">'
    first_records = [
        _record(1, 1, "user", "User message", "Inspect the latest task", 0),
        _record(2, 1, "reasoning", "Reasoning summary", "Plan the inspection", 300, 500),
        _record(
            3,
            1,
            "tool",
            "exec",
            "Run the focused checks",
            900,
            1_450,
            call_id="call-checks",
            input_detail='{"cmd":"uv run pytest -q"}',
            output_detail="29 passed in 0.31s",
        ),
        _record(
            4,
            1,
            "assistant",
            "Assistant message",
            "The focused checks passed",
            2_500,
            400,
            usage_detail={
                "input_tokens": 320,
                "cached_input_tokens": 256,
                "output_tokens": 72,
                "reasoning_output_tokens": 24,
                "total_tokens": 392,
            },
        ),
        _record(5, 2, "user", "User message", "Inspect the failed command", 3_600),
        _record(
            6,
            2,
            "tool",
            "exec",
            "Run a command that reports an expected failure",
            4_000,
            720,
            status="error",
            call_id="call-failure",
            input_detail='{"cmd":"example --fail"}',
            output_detail="exit code 2: expected failure",
        ),
        _record(
            7,
            2,
            "subagent",
            "Subagent activity from the long-running reviewer worker",
            "Reviewer completed after checking the full implementation and focused regressions",
            4_900,
            300,
        ),
        _record(
            8,
            2,
            "compaction",
            "Context compacted",
            "Conversation context compacted",
            5_400,
            None,
        ),
        _record(
            9,
            2,
            "assistant",
            "Assistant message",
            "Failure isolated and explained",
            5_700,
            650,
            usage_detail={
                "input_tokens": 192,
                "cached_input_tokens": 128,
                "output_tokens": 56,
                "reasoning_output_tokens": 16,
                "total_tokens": 248,
            },
        ),
    ]
    second_records = [
        _record(1, 1, "user", "User message", "Review the documentation", 0),
        _record(2, 1, "tool", "read_file", "Read the interface guide", 400, 600),
        _record(
            3,
            1,
            "assistant",
            "Assistant message",
            "Documentation review complete",
            1_200,
            300,
            usage_detail={
                "input_tokens": 64,
                "cached_input_tokens": 32,
                "output_tokens": 12,
                "reasoning_output_tokens": 4,
                "total_tokens": 76,
            },
        ),
    ]
    large_records = [
        _record(
            (turn - 1) * 5 + offset,
            turn,
            "assistant" if offset == 5 else "reasoning",
            "Assistant message" if offset == 5 else "Reasoning summary",
            f"Large task turn {turn} record {offset}",
            (turn - 1) * 5_000 + offset * 500,
        )
        for turn in range(1, 101)
        for offset in range(1, 6)
    ]
    paged_records = [
        _record(
            (turn - 1) * 5 + offset,
            turn,
            "assistant" if offset == 5 else "reasoning",
            "Assistant message" if offset == 5 else "Reasoning summary",
            f"Paged task turn {turn} record {offset}",
            (turn - 1) * 5_000 + offset * 500,
        )
        for turn in range(1, 242)
        for offset in range(1, 6)
    ]
    big_token_records = [
        _record(1, 1, "user", "User message", "Inspect a very large token total", 0),
        _record(
            2,
            1,
            "assistant",
            "Assistant message",
            "Very large task complete",
            500,
            1_000,
            usage_detail={
                "input_tokens": 118_700_200_000,
                "cached_input_tokens": 115_665_200_000,
                "output_tokens": 534_137_188,
                "reasoning_output_tokens": 400_000_000,
                "total_tokens": 119_234_337_188,
            },
        ),
    ]
    hostile_records = [
        _record(
            1,
            1,
            "tool",
            hostile,
            f"</td><script>window.__trajectoryXss=true</script>{hostile}",
            0,
            10,
            call_id='bad-id" onmouseover="window.__trajectoryXss=true',
            input_detail=hostile,
            output_detail="<svg onload=window.__trajectoryXss=true>",
        )
    ]
    sessions = [
        {
            "id": "session-alpha",
            "title": "Inspect the latest task",
            "cwd": "~/work/codex-trajectory",
            "model": "gpt-5",
        },
        {
            "id": "session-beta",
            "title": "Review the documentation",
            "cwd": "~/work/docs",
            "model": "gpt-5",
        },
        {
            "id": "session-missing",
            "title": "Unavailable task",
            "cwd": "~/work/missing",
            "model": "gpt-5",
        },
        {
            "id": "session-large",
            "title": "Inspect a 500-record task",
            "cwd": "~/work/large-task",
            "model": "gpt-5",
        },
        {
            "id": "session-paged",
            "title": "Inspect a 1,205-record task",
            "cwd": "~/work/paged-task",
            "model": "gpt-5",
        },
        {
            "id": "session-big-tokens",
            "title": "Inspect a 119-billion-token task",
            "cwd": "~/work/big-token-task",
            "model": "gpt-5",
        },
        {
            "id": "session-xss",
            "title": hostile,
            "cwd": hostile,
            "model": hostile,
        },
    ]
    return {
        "session-alpha": _trajectory(sessions[0], sessions, first_records, 2),
        "session-beta": _trajectory(sessions[1], sessions, second_records, 1),
        "session-large": _trajectory(sessions[3], sessions, large_records, 100),
        "session-paged": _trajectory(sessions[4], sessions, paged_records, 241),
        "session-big-tokens": _trajectory(sessions[5], sessions, big_token_records, 1),
        "session-xss": _trajectory(sessions[6], sessions, hostile_records, 1),
    }


def _trajectory(
    session: dict[str, Any],
    recent_sessions: list[dict[str, Any]],
    records: list[dict[str, Any]],
    turn_count: int,
) -> dict[str, Any]:
    """Build one complete UI payload."""
    turns = []
    for index in range(1, turn_count + 1):
        members = [record for record in records if record["turn"] == index]
        usage_items = [record["usage"] for record in members if record["usage"] is not None]
        turns.append(
            {
                "index": index,
                "id": f"turn-{index}",
                "startedAt": members[0]["startedAt"],
                "completedAt": members[-1]["completedAt"],
                "durationMs": 2_900 if index == 1 else 2_750,
                "timeToFirstTokenMs": 300,
                "status": "complete",
                "error": None,
                "records": len(members),
                "steps": max(record["step"] for record in members),
                "model": session["model"],
                "modelCalls": len(usage_items),
                "usage": _aggregate_usage(usage_items),
            }
        )
    tool_records = [record for record in records if record["kind"] == "tool"]
    usage_items = [record["usage"] for record in records if record["usage"] is not None]
    return {
        "schemaVersion": 1,
        "detailLevel": "full",
        "session": session,
        "recentSessions": recent_sessions,
        "pagination": {
            "firstRecord": records[0]["index"] if records else None,
            "lastRecord": records[-1]["index"] if records else None,
            "earlierRecords": 0,
            "laterRecords": 0,
            "hasEarlier": False,
            "hasLater": False,
            "nextBeforeRecord": None,
        },
        "turns": turns,
        "records": records,
        "warnings": [],
        "stats": {
            "turns": turn_count,
            "records": len(records),
            "visibleRecords": len(records),
            "omittedRecords": 0,
            "toolCalls": len(tool_records),
            "failedTools": sum(record["status"] == "error" for record in tool_records),
            "compactions": sum(record["kind"] == "compaction" for record in records),
            "tokens": _aggregate_usage(usage_items),
            "contextWindow": 200_000,
            "rateLimits": {
                "primary": {
                    "usedPercent": 31.5,
                    "windowMinutes": 300,
                    "resetsAt": "2026-08-14T02:00:00Z",
                },
                "secondary": {
                    "usedPercent": 56,
                    "windowMinutes": 10080,
                    "resetsAt": "2026-08-21T00:00:00Z",
                },
            },
        },
    }


def wrapper_html(
    language: str,
    *,
    host_display: bool = False,
    native_pip_unavailable: bool = False,
) -> str:
    """Create a parent page that emulates the Codex app-resource bridge."""
    payload = json.dumps(demo_trajectories(), ensure_ascii=False).replace("</", "<\\/")
    requested_language = "zh-CN" if language == "zh" else "en"
    trajectory_path = (
        f"/trajectory.html?lang={requested_language}"
        f"&hostDisplay={int(host_display)}"
        f"&nativePipUnavailable={int(native_pip_unavailable)}"
    )
    return f"""<!doctype html>
<html lang="{language}">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex Trajectory UI Harness</title>
<style>html,body,#viewer{{width:100%;height:100%;margin:0;border:0;background:#f7f8fc}}body{{overflow:auto}}#viewer{{display:block}}</style>
</head>
    <body><iframe
      id="viewer"
      title="Codex Trajectory"
      sandbox="allow-scripts"
      src="{trajectory_path}"
    ></iframe>
<script>
const trajectories = {payload};
const viewer = document.getElementById("viewer");
window.__trajectoryCalls = [];
window.__trajectoryToolNames = [];
window.__trajectoryDisplayModes = [];
window.__trajectoryFollowUps = [];
window.__trajectoryFollowUpFailures = 0;
window.__trajectoryWidgetStates = [];
window.__trajectoryCdpToolbar = {{
  schemaVersion: 1,
  enabled: false,
  port: 9222,
  cdpAvailable: true,
  daemonRunning: false,
  connected: false,
  injected: false,
  viewerServing: false,
  lastError: null,
}};
let liveVersion = 1;
const currentLiveRevision = () => liveVersion.toString(16).padStart(64, "0");
window.__setTrajectoryRemaining = remaining => {{
  const value = Number(remaining);
  if (!Number.isFinite(value)) throw new Error("remaining must be finite");
  trajectories["session-alpha"].stats.rateLimits.primary.usedPercent = 100 - value;
  liveVersion += 1;
}};
window.__setTrajectoryResetAt = resetsAt => {{
  if (typeof resetsAt !== "string" || !resetsAt) throw new Error("resetsAt must be a string");
  trajectories["session-alpha"].stats.rateLimits.primary.resetsAt = resetsAt;
  liveVersion += 1;
}};
window.__setTrajectoryRunning = running => {{
  const source = trajectories["session-alpha"];
  const latest = source.turns.at(-1);
  latest.status = running ? "running" : "complete";
  latest.completedAt = running ? null : (source.records.at(-1)?.completedAt || null);
  liveVersion += 1;
}};
window.__advanceTrajectoryLive = () => {{
  const source = trajectories["session-alpha"];
  if (!source.records.some(record => record.index === 10)) {{
    const usage = {{
      input_tokens: 48,
      cached_input_tokens: 32,
      output_tokens: 16,
      reasoning_output_tokens: 4,
      total_tokens: 64,
    }};
    source.records.push({{
      ...structuredClone(source.records.at(-1)),
      index: 10,
      id: "record-3-10",
      turn: 3,
      step: 1,
      kind: "assistant",
      event: "Assistant message",
      summary: "Live update arrived",
      status: "running",
      callId: null,
      usage,
      input: null,
      output: null,
      metadata: {{}},
    }});
    source.turns.push({{
      index: 3,
      id: "turn-3",
      startedAt: source.records.at(-1).startedAt,
      completedAt: null,
      durationMs: 0,
      timeToFirstTokenMs: 0,
      status: "running",
      error: null,
      records: 1,
      steps: 1,
      model: source.session.model,
      modelCalls: 1,
      usage,
    }});
    source.stats.turns = 3;
    source.stats.records = 10;
    source.stats.visibleRecords = 10;
    source.stats.tokens = {{
      input_tokens: 560,
      cached_input_tokens: 416,
      output_tokens: 144,
      reasoning_output_tokens: 44,
      total_tokens: 704,
    }};
    source.stats.rateLimits.primary.usedPercent = 32;
  }}
  liveVersion += 1;
}};
function trajectory(
  sessionId = "session-alpha",
  detailLevel = "summary",
  maxRecords = 500,
  beforeRecord = null
) {{
  const source = trajectories[sessionId] || trajectories["session-alpha"];
  const copy = structuredClone(source);
  copy.detailLevel = detailLevel;
  const limit = Number.isInteger(maxRecords) ? Math.max(50, Math.min(1000, maxRecords)) : 500;
  const eligible = copy.records.filter(
    record => beforeRecord === null || record.index < beforeRecord
  );
  copy.records = eligible.slice(-limit);
  const visibleTurns = new Set(copy.records.map(record => record.turn));
  copy.turns = copy.turns.filter(turn => visibleTurns.has(turn.index));
  const firstRecord = copy.records.at(0)?.index ?? null;
  const lastRecord = copy.records.at(-1)?.index ?? null;
  const totalRecords = source.stats.records;
  const earlierRecords = firstRecord === null ? 0 : Math.max(0, firstRecord - 1);
  const laterRecords = lastRecord === null ? totalRecords : Math.max(0, totalRecords - lastRecord);
  copy.pagination = {{
    firstRecord,
    lastRecord,
    earlierRecords,
    laterRecords,
    hasEarlier: earlierRecords > 0,
    hasLater: laterRecords > 0,
    nextBeforeRecord: earlierRecords > 0 ? firstRecord : null,
  }};
  copy.stats.visibleRecords = copy.records.length;
  copy.stats.omittedRecords = earlierRecords + laterRecords;
  if (detailLevel !== "full") {{
    copy.records = copy.records.map(record => (
      {{...record, input: null, output: null, metadata: {{}}}}
    ));
  }}
  return copy;
}}
function notify(value) {{
  viewer.contentWindow.postMessage(
    {{jsonrpc:"2.0",method:"ui/notifications/tool-result",params:{{structuredContent:value}}}},
    "*"
  );
}}
viewer.addEventListener("load", () => notify(trajectory()));
window.addEventListener("message", event => {{
  if (event.source !== viewer.contentWindow) return;
  if (event.data?.method === "trajectory/follow-up") {{
    window.__trajectoryFollowUps.push(structuredClone(event.data.params));
    return;
  }}
  if (event.data?.method === "trajectory/widget-state") {{
    window.__trajectoryWidgetStates.push(structuredClone(event.data.params));
    return;
  }}
  if (event.data?.method === "trajectory/display-mode") {{
    window.__trajectoryDisplayModes.push(event.data.params?.mode);
    return;
  }}
  if (event.data?.method !== "tools/call") return;
  const name = event.data.params?.name;
  const args = event.data.params?.arguments || {{}};
  window.__trajectoryToolNames.push(name);
  window.__trajectoryCalls.push(structuredClone(args));
  if (args.sessionId === "session-missing") {{
    const result = {{isError: true, content: [{{type: "text", text: "Task disappeared"}}]}};
    viewer.contentWindow.postMessage({{jsonrpc:"2.0",id:event.data.id,result}}, "*");
    return;
  }}
  let result;
  if (name === "get_codex_toolbar_injection_status") {{
    result = {{structuredContent: structuredClone(window.__trajectoryCdpToolbar)}};
  }} else if (name === "set_codex_toolbar_injection") {{
    window.__trajectoryCdpToolbar = {{
      ...window.__trajectoryCdpToolbar,
      enabled: args.enabled === true,
      port: Number.isInteger(args.port) ? args.port : 9222,
      daemonRunning: args.enabled === true,
      connected: args.enabled === true,
      injected: args.enabled === true,
      viewerServing: args.enabled === true,
      lastError: null,
    }};
    result = {{structuredContent: structuredClone(window.__trajectoryCdpToolbar)}};
  }} else if (name === "get_codex_trajectory_update") {{
    const revision = currentLiveRevision();
    const unchanged = args.revision === revision;
    const update = {{schemaVersion: 1, unchanged, revision}};
    if (!unchanged) {{
      update.trajectory = trajectory(args.sessionId, "summary", 50, null);
      delete update.trajectory.recentSessions;
    }}
    result = {{structuredContent: update}};
  }} else {{
    result = {{
      structuredContent: trajectory(
        args.sessionId,
        args.detailLevel,
        args.maxRecords,
        args.beforeRecord ?? null
      )
    }};
  }}
  viewer.contentWindow.postMessage({{jsonrpc:"2.0",id:event.data.id,result}}, "*");
}});
</script></body></html>"""


class HarnessHandler(BaseHTTPRequestHandler):
    """Serve the app resource and its deterministic Codex bridge."""

    def do_GET(self) -> None:
        """Return one harness resource."""
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)
        if route in {"/", "/en"}:
            self._send(wrapper_html("en"), "text/html; charset=utf-8")
            return
        if route == "/en-dock":
            self._send(
                wrapper_html("en", host_display=True, native_pip_unavailable=True),
                "text/html; charset=utf-8",
            )
            return
        if route == "/en-pip-unavailable":
            self._send(
                wrapper_html("en", native_pip_unavailable=True),
                "text/html; charset=utf-8",
            )
            return
        if route == "/zh":
            self._send(wrapper_html("zh"), "text/html; charset=utf-8")
            return
        if route == "/toolbar-fixture":
            self._send(
                """<!doctype html><html><head><meta charset="utf-8"></head><body>
                <div data-app-action-sidebar-thread-active="true"
                  data-app-action-sidebar-thread-id="local:session-alpha"></div>
                <form id="composer"><button id="access" type="button">Full access</button>
                <textarea role="textbox"></textarea>
                <button id="send" type="submit" aria-label="Send message">Send</button></form>
                <script>window.__submitted=[];document.querySelector('form').addEventListener(
                'submit',event=>{event.preventDefault();window.__submitted.push(
                document.querySelector('textarea').value);});</script></body></html>""",
                "text/html; charset=utf-8",
            )
            return
        if route in {"/trajectory.html", "/trajectory.zh.html"}:
            content = app_resource_html()
            host_display = query.get("hostDisplay") == ["1"]
            if host_display:
                mock_openai = """<script>
window.openai = {
  displayMode: "inline",
  theme: "dark",
  widgetState: null,
  setWidgetState: state => {
    window.openai.widgetState = structuredClone(state);
    window.parent.postMessage({method:"trajectory/widget-state",params:state}, "*");
  },
  sendFollowUpMessage: async value => {
    if (window.__trajectoryFollowUpFailures > 0) {
      window.__trajectoryFollowUpFailures -= 1;
      throw new Error("Temporary follow-up failure");
    }
    window.parent.postMessage({method:"trajectory/follow-up",params:value}, "*");
    return {};
  },
  requestDisplayMode: async ({ mode }) => {
    if (mode !== "inline" && mode !== "fullscreen") throw new Error("Unsupported mode");
    window.openai.displayMode = mode;
    window.parent.postMessage({method:"trajectory/display-mode",params:{mode}}, "*");
    window.dispatchEvent(new CustomEvent("openai:set_globals", {
      detail: { globals: { displayMode: mode } },
    }));
    return { mode };
  },
};
window.__trajectoryFollowUpFailures = 0;
window.__setOpenAITheme = theme => {
  window.openai.theme = theme;
  window.dispatchEvent(new CustomEvent("openai:set_globals", {
    detail: { globals: { theme } },
  }));
};
</script>"""
            else:
                mock_openai = """<script>
window.openai = {};
</script>"""
            content = content.replace("<body>", f"<body>{mock_openai}", 1)
            if route.endswith("zh.html"):
                content = content.replace('<html lang="en">', '<html lang="zh-CN">', 1)
            self._send(content, "text/html; charset=utf-8")
            return
        self.send_error(404)

    def _send(self, body: str, content_type: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args: object) -> None:
        """Keep deterministic test output quiet."""


def start_server() -> tuple[ThreadingHTTPServer, threading.Thread]:
    """Start a harness server on an ephemeral loopback port."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), HarnessHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def main() -> None:
    """Run the harness until interrupted."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), HarnessHandler)
    print(f"http://127.0.0.1:{server.server_port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

# Codex Trajectory

[![CI](https://github.com/icesixgod/codex-trajectory/actions/workflows/ci.yml/badge.svg)](https://github.com/icesixgod/codex-trajectory/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/icesixgod/codex-trajectory)](https://github.com/icesixgod/codex-trajectory/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Read this in [简体中文](README.zh-CN.md).

Codex Trajectory is a privacy-aware Codex plugin whose MCP tools turn local task logs into an event ledger and interactive timeline. It shows turns, approximate model steps, reasoning summaries, assistant messages, tool timing, subagents, compaction, token usage, and failures without changing the original logs. Its optional live stop controls use an explicitly enabled loopback CDP path to pause an active Goal and interrupt the current turn directly; they never post a follow-up message, enter the steering queue, delete worktrees, or modify task files.

![Codex Trajectory desktop viewer](plugins/codex-trajectory/assets/screenshots/desktop-en.png)

## Install

Prerequisites: Codex and [uv](https://docs.astral.sh/uv/getting-started/installation/) on macOS, Linux, or Windows.

```sh
codex plugin marketplace add icesixgod/codex-trajectory
codex plugin add codex-trajectory@icesixgod
```

Open a new Codex task so the installed tools and skill are loaded. Then ask:

> Show the safe trajectory summary for this Codex task.

## Privacy model

Safe summary mode is the default. It returns event names, timing, status, token usage, and bounded summaries while hiding tool inputs, tool outputs, raw record metadata, absolute log paths, Git remotes, base instructions, and encrypted reasoning.

Full details are opt-in through `detailLevel: "full"` or the viewer's confirmation button. They expose bounded record details to the active Codex conversation, but still never return base instructions or encrypted reasoning. The Python runtime has no telemetry and, unless the optional CDP toolbar integration is enabled, makes no application network requests; that integration connects only to the user-selected loopback debugging port. The `uv` launcher may provision a compatible Python according to the user's own uv configuration. See [PRIVACY.md](PRIVACY.md).

## Tools

| Tool | Purpose |
| --- | --- |
| `list_codex_sessions` | List recent task metadata without transcript bodies. |
| `get_codex_trajectory` | Return structured trajectory data for analysis. |
| `show_codex_trajectory` | Return the trajectory with an interactive MCP Apps viewer. |

`get_codex_trajectory` and `show_codex_trajectory` accept `sessionId`, `maxRecords` (50–1000), the exclusive `beforeRecord` cursor, `includeArchived`, and `detailLevel` (`summary` or `full`). Omit `sessionId` for the latest task and omit `beforeRecord` for its newest tail; pass `pagination.nextBeforeRecord` to retrieve the immediately preceding page.

The output uses [`schemaVersion: 1`](schemas/trajectory-v1.schema.json). Both legacy rollouts and current paginated `history_base` lineages are supported. Paginated identities, byte boundaries, and contiguous ordinals are validated before inherited history is joined; copied parent context before a subagent's `subagent_history_start_ordinal` is excluded from the child trajectory. Codex logs do not expose DeepSeek Harness step boundaries directly, so a new approximate step begins when model output resumes after one or more tool results. Unknown control events are ignored; malformed complete JSONL or UTF-8 lines are reported in `warnings`, while an unfinished JSON or UTF-8 tail is tolerated during active writes. See [the interface reference](docs/interface.md).

The viewer's Token details panel separates input, cache reads, uncached input, output, and reasoning output. It also shows cache-hit rate and per-turn totals in a collapsed section. Large totals, including tens or hundreds of billions of tokens, are displayed in full with responsive numeric sizing. Cache and reasoning counters are subsets of input and output respectively, not additional tokens.

The event ledger expands only the latest loaded turn by default. When an earlier page exists, **Load earlier records** prepends the next 500-record page, deduplicates it by stable record index, and preserves the current ledger viewport; repeat it to load the complete task. Every turn header shows its model plus separate uncached-input, cache-read, and output totals, and the entire summary strip—including all three token totals—toggles that turn. An expanded turn owns its column header, which stays pinned only while that turn's records are in view, so collapsed turns never separate a global header from the records it describes. Since the turn is already identified by that header, record rows show only their approximate Step instead of repeating Turn/step. Event and Content use compact proportional columns so all three token columns remain visible without horizontal scrolling; hovering either truncated field reveals its complete value, and focusing a record exposes both values together. Earlier turns and the Token details per-turn table are rendered lazily when opened, reducing DOM work for large tasks. Search, type filters, timeline selection, and direct record selection reveal matching collapsed turns automatically.

Click **Live window** to keep the current task visible without launching a standalone app. Inside Codex, the component uses the host's supported `fullscreen` presentation to fill its side panel; a frozen header shows the whole task's cumulative Token breakdown and current turn/step/record cursor, while the remaining height is a separately scrollable safe-summary event stream. When Codex records account rate-limit windows, the header's upper-right quota badge shows the remaining percentage for each window—normally 5-hour and weekly—with reset times in its tooltip; the badge stays hidden when that data is unavailable. A one-click **Stop** control and the optional auto-stop guard both require the experimental loopback CDP integration. They pause an active Goal through `thread/goal/set` and then call `turn/interrupt`; the standard Apps surface uses the private `request_codex_task_stop` helper, while the configured Browser uses its session-bound endpoint. Neither path posts a follow-up message, invokes the model, waits for a confirmation dialog, or enters the steering queue. The guard is off by default with a 10% threshold and stops at most once while the same displayed quota cycle remains at or below that threshold. A stale projected turn is rebound once, and transient automatic failures retry with bounded backoff. When the turn finishes, **Stopped** changes to **Idle**, and a later running turn rearms manual Stop. The automatic latch deliberately survives later turns—including `/goal` continuations—until all displayed windows recover above the threshold, a quota window resets, or the user changes the guard configuration. The controls are disabled with an explicit setup message while direct CDP is unavailable, or if the viewer switches to a task other than the one that opened it.

Each event shows its status, duration, and record-level Token delta as a three-row Total/Input/Output breakdown: Input separates uncached input from cache reads, while Output separates visible output from reasoning. Point-only records without a measured elapsed interval show `—` instead of the misleading `0 ms`; genuinely measured zero-duration records remain `0 ms`. The newest record stays at the bottom with automatic follow-latest behavior. A compact transparent 32-frame mining mascot stays fully inside that newest card's upper-left corner and plays one cycle only when the latest record identity changes; unchanged polls leave it idle, and reduced-motion preferences disable the animation. The component never requests the unsupported host `pip` mode. In a regular Chromium page without the Codex display bridge, it prefers browser-native video picture-in-picture and renders the same quota summary in the upper right; if that API is unavailable or rejected at runtime, it falls back to the same full-height live panel inside the current page. Stop controls are not shown in the non-interactive video surface; an interactive in-page fallback enables them when either supported stop bridge is present. Full tool input, output, and raw metadata stay out of every live surface. The window checks for changes serially every second, pauses while hidden or after exit, and backs off after errors. Its mounted side-panel shell is updated in place, so unchanged polls do not recreate or move the status row. The app-only refresh helper compares an opaque lineage revision first; unchanged tasks are not reparsed, while changed tasks return only a 50-record safe-summary tail. Standard entry still requires an explicit click.

### Optional direct stop and Codex in-app Browser shortcut

The inline viewer has an off-by-default **Unattended stop and in-app Browser shortcut** setting. When enabled, its app-private stop helper and dependency-free local watcher connect only to the selected `127.0.0.1` Chrome DevTools Protocol port. The helper gives the standard Apps side panel the same fixed-intent Goal-pause plus `turn/interrupt` sequence as the Browser page, including one stale-turn rebind; it accepts only bounded identifiers, a manual/auto source, threshold, and locale, never arbitrary prompt text. The watcher also survives Codex React rerenders and injects **View trajectory** immediately after **Full access**. It serves the complete trajectory interface from a tokenized random loopback port with the same safe-summary default, task selection, statistics, Token details, timing overview, filters, ledger, refresh, pagination, explicit full-detail confirmation, and stop controls. Opening or stopping never changes the composer, posts a task message, invokes the model, or creates a steering turn. The Browser endpoint remains bound to the session and active turn that opened it; a lightweight App Server status read keeps Stop/Idle current without repeatedly materializing turn history. If an active Goal cannot be paused, the turn is not interrupted and the UI reports the bounded error. Disabling the setting removes the injected DOM, closes the local page server, and stops the watcher. Updated MCP runtimes reconcile the opt-in and safely replace a verified older watcher. Settings, heartbeat, and watcher lock remain bounded single-link regular files under `CODEX_HOME/codex-trajectory`; task logs, drafts, attachments, and theme values remain untouched.

CDP must already be enabled when the app starts. Completely quit ChatGPT/Codex first. On macOS, relaunch it from Terminal with:

```sh
open -a ChatGPT --args --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222
```

For the Microsoft Store build on Windows, use PowerShell (the installed package name or executable subpath may differ in future builds):

```powershell
$codex = Get-AppxPackage OpenAI.Codex
Start-Process "$($codex.InstallLocation)\app\Codex.exe" -ArgumentList '--remote-debugging-address=127.0.0.1','--remote-debugging-port=9222'
```

The viewer shows this command when the selected port is unavailable. A debugging port can control the application page, so keep it bound to loopback, do not expose it through a tunnel or non-loopback address, and turn the setting off when it is not needed. This is an experimental integration against the current Codex DOM, not a documented plugin toolbar API.

Session logs are parsed incrementally. Only the requested record page and a bounded turn/warning/call state are retained in memory, while aggregate statistics still describe the complete parsed task. JSON objects must be unambiguous and interoperable: duplicate keys, non-finite numbers, excessively large integers, and complete lines over 16 MiB are rejected or reported. Repeated cumulative Token snapshots are deduplicated, partial valid snapshots preserve the last valid counters, and unchanged session overviews are cached using every lineage file's metadata. Discovery is restricted to regular, single-link files under the configured session roots; symlinks, hardlinks, and path-like session selectors are rejected. Full-view refreshes start from the bounded 500-record tail instead of requesting the 1,000-record maximum, while earlier pages are loaded only on request.

## Development

```sh
uv sync --group dev
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest --cov --cov-report=term-missing
```

The runtime has no third-party Python dependencies. Development and tests are locked in `uv.lock`. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Attribution

Portions of the event-ledger, timeline, selection, and inspector implementation are adapted from [`@deepseek-ai/dsh-client-ui-trajectory`](https://github.com/deepseek-ai/deepseek-harness/tree/master/packages/client/ui-trajectory), copyright (c) 2026 DeepSeek, under the MIT License. The complete upstream license is included in [`LICENSES/DeepSeek-Harness.txt`](LICENSES/DeepSeek-Harness.txt), with additional details in [`NOTICE`](NOTICE).

Codex Trajectory is an independent project and is not affiliated with or endorsed by DeepSeek. Codex uses a different persisted event vocabulary, and this repository bundles no DeepSeek Harness package, Cordis runtime, React runtime, TanStack Virtual package, or `diff` package.

**Friendly Links**

[Linux.do](https://linux.do/)

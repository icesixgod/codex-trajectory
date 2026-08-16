# Codex Trajectory

[![CI](https://github.com/icesixgod/codex-trajectory/actions/workflows/ci.yml/badge.svg)](https://github.com/icesixgod/codex-trajectory/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/icesixgod/codex-trajectory)](https://github.com/icesixgod/codex-trajectory/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Read this in [简体中文](README.zh-CN.md).

Codex Trajectory is a read-only Codex plugin that turns local task logs into a privacy-aware event ledger and interactive timeline. It shows turns, approximate model steps, reasoning summaries, assistant messages, tool timing, subagents, compaction, token usage, and failures without changing the original logs.

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

Full details are opt-in through `detailLevel: "full"` or the viewer's confirmation button. They expose bounded record details to the active Codex conversation, but still never return base instructions or encrypted reasoning. The Python runtime has no telemetry and makes no application network requests; the `uv` launcher may provision a compatible Python according to the user's own uv configuration. See [PRIVACY.md](PRIVACY.md).

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

Session logs are parsed incrementally. Only the requested record page and a bounded turn/warning/call state are retained in memory, while aggregate statistics still describe the complete parsed task. JSON objects must be unambiguous and interoperable: duplicate keys, non-finite numbers, excessively large integers, and complete lines over 16 MiB are rejected or reported. Repeated cumulative Token snapshots are deduplicated, partial valid snapshots preserve the last valid counters, and unchanged session overviews are cached using every lineage file's metadata. Discovery is restricted to regular, single-link files under the configured session roots; symlinks, hardlinks, and path-like session selectors are rejected. Viewer refreshes start from the bounded 500-record tail instead of requesting the 1,000-record maximum, while earlier pages are loaded only on request; continuous polling is intentionally avoided because repeatedly rescanning a very large active log would increase load.

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

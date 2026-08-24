# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- Remove the redundant `SessionStart` command hook and launch the MCP through one portable relative entry point: Unix executes `uv` directly, while a reviewed Windows GUI-subsystem shim prefers Astral's stdio-preserving `uvw.exe` and falls back to `uv.exe` with `CREATE_NO_WINDOW`. Start the optional watcher with the base `pythonw.exe` plus the active uv environment's import path instead of a venv redirector that can reopen `python.exe`. Together these prevent Session selection from creating a Windows Terminal/`cmd.exe` tab while preserving watcher recovery and Linux/macOS startup.
- Select the outer authenticated `OpenAI.Codex` desktop ancestor when packaged `codex.exe` and `ChatGPT.exe` both appear in the Windows process chain, allowing the real parent-owned CDP listener to pass peer authentication; report total shell-target authentication failure through the fixed public error category instead of silently returning disconnected state.
- Keep watcher runtime identity stable across the hook and MCP launch environments, replace stale toolbar markup deterministically, and make the Windows runtime and browser acceptance checks portable.

## [0.4.0] - 2026-08-24

### Added

- Show standard-API-equivalent USD cost estimates for the complete task, every priced turn, and each Token-usage record, including cache discounts, mixed-model coverage, and explicit unavailable pricing for unknown model IDs; surface all three levels in the live side panel and native PiP.

### Changed

- Advance trajectory responses and the MCP Apps resource to schema version 2 for the new required cost fields while keeping the closed version 1 schema frozen for existing validators.
- Refresh the GPT-5.6 standard short-context price snapshot to the current Sol promotional rate and the reduced Terra and Luna rates published on 2026-08-24.
- Read Codex `.jsonl.zst` rollouts, including concatenated frames and decompressed logical lineage offsets; Python 3.10–3.13 use pinned `zstandard==0.25.0`, while Python 3.14 uses its standard library.
- Restore the token-bearing in-app Browser shortcut on macOS and Windows only after per-connection operating-system TCP ownership, Codex host ancestry, process-creation identity, and platform signing/package checks succeed; unsupported or failed authentication remains fail-closed while app-private direct stop stays available after explicit opt-in.

### Fixed

- Serialize CDP recovery and watcher replacement across MCP processes, preserve later disable/port choices with settings revisions, use safe Windows process handles and the active uv interpreter environment, avoid disabled status probes, and expose only a fixed public error category.
- Reconcile an already enabled authenticated watcher from a reviewed asynchronous session-start hook on macOS and Windows, reducing the cold-start delay before **View trajectory** appears while retaining MCP recovery as a fallback.
- Apply aggregate absolute deadlines to CDP discovery, WebSocket traversal, task-state, theme, and stop operations; recheck the opt-in before direct stop; accept the current stale-turn error shape; and avoid full-history bootstraps.
- Harden rollout discovery and reads with descriptor-checked root containment, file/line/byte/depth/lineage budgets, leading metadata rules for paginated files, dual thread/rollout UUID validation, and decompression-bomb, truncation, trailing-data, symlink, hardlink, and non-regular-file rejection.
- Accumulate Token counters across resets without double-counting duplicate snapshots, retain late samples on their producing turns, align overview/detail/model-call/cost totals, and reject non-scalar protocol identifiers.
- Require complete valid price inputs, leave unpublished or fake dated models unpriced, represent unavailable cached-input prices explicitly, and make large Decimal aggregation safe.
- Enforce strict MCP initialization, stable unknown-resource and invalid-parameter errors, an 8 MiB encoded response limit, observable response cancellation, accurate destructive stop annotations, and a compatible unlisted version 1 resource URI alias.
- Keep initial CDP status in a retrying state until loaded, prevent stale reads from overwriting a newer user choice, distinguish the withheld Browser shortcut from ready direct stop, restore semantic table/group accessibility, and preserve very large PiP Token values without ellipsis.
- Validate the frozen version 1 schema hash, version 2 summary/cost/pagination invariants, archive runtime contents and versioned roots, extracted ZIP/TAR smoke tests, cross-platform Python 3.10–3.14 behavior, Windows mypy, and focused package/CDP/MCP coverage floors before release.

## [0.3.2] - 2026-08-21

### Fixed

- Replace the standard Apps stop follow-up with an app-private CDP/App Server action that pauses an active Goal and interrupts the bound turn directly, including one missing/stale-turn rebind; automatic stop now fails closed when direct CDP is unavailable instead of opening a confirmation dialog or entering the steering queue.
- Show platform-specific loopback CDP relaunch guidance in the viewer and document the Microsoft Store PowerShell launch path for Windows.

## [0.3.1] - 2026-08-21

### Added

- Show the remaining percentage for persisted Codex rate-limit windows in the live view's upper right, with reset times in tooltips and matching native PiP output; hide the quota UI when official limit data is absent.
- Add a one-click live stop request and an opt-in remaining-quota guard, defaulting to 10%, that sends at most one automatic shutdown request per displayed quota cycle while preserving worktrees and uncommitted changes.
- Treat a selected task with no running turn as already stopped: disable the manual stop button, keep auto-stop armed for the next running turn, and convert Browser stop races into a retryable idle state instead of an error.
- Add an off-by-default loopback CDP integration, configurable inside the viewer, that places “View trajectory” after `Full access` and opens the complete trajectory interface in a tokenized Codex in-app Browser page without sending a task message or invoking the model.
- Match Codex's System, Light, and Dark appearances by synchronizing a fixed allowlist of effective host colors into the live trajectory interface, including already-open Browser tabs.

### Fixed

- Drive Browser Stop/Idle from App Server task state and keep automatic quota stops latched across later turns in the same quota cycle, preventing running tasks from flickering to or remaining at **Idle** without creating a durable Goal continuation loop.
- Rearm **Requested** stop controls after a turn finishes and for each later running turn, retry transient automatic failures without consuming the latch, and disable stopping after the viewer selects a different task.
- Let CDP stop evaluation cover the bounded App Server round trip, bind Browser stop messages to the page's original session, and safely replace verified outdated watcher processes after plugin cache-buster upgrades.
- Return a bounded Browser API error when the CDP-backed stop provider is unavailable instead of dropping the loopback HTTP connection.
- Restrict the token-bearing CDP injection to Codex-owned `app://` renderer targets, remove stale injection state from other targets, refuse linked watcher lock files, reject non-interoperable local bridge JSON, prevent CDP discovery from following HTTP redirects, and enforce absolute WebSocket command deadlines.
- Fall back to an in-page live panel when the Codex in-app Browser exposes Chromium's
  picture-in-picture API but rejects `requestPictureInPicture()` at runtime.
- Reuse the full MCP Apps trajectory resource in the Codex Browser shortcut instead of showing a reduced live-summary interface.
- Enable one-click and quota-threshold stop controls in the in-app Browser fallback through a session-matched, fixed-intent CDP/App Server bridge that pauses an active Goal and calls `turn/interrupt` without touching composer drafts or attachments.
- Recognize the visible `Full access` permission label when Codex exposes a different accessibility
  label on the same control, and scan only Codex-owned page, iframe, and `webview` targets when
  maintaining the injected trajectory shortcut.
- Open the injected trajectory shortcut as a host-managed Codex Browser tab instead of a fixed
  overlay that obscures the native Environment/Changes side panel.
- Make Browser auto-stop fire at an exact remaining-quota threshold even when the separate App Server state poll is unavailable, bind state reads directly to the trajectory task, and use `turn/interrupt` so a successful trigger actually stops the active turn.
- Resolve persistent `client-new-thread:*` sidebar keys to the materialized App Server task UUID before opening or stopping a Browser trajectory, disable the shortcut until that UUID exists, and reconcile an enabled outdated watcher whenever the updated MCP runtime starts.
- Recheck the bound turn after an interrupt error so completion races become **Idle**, while still-running failures report a specific App Server timeout or interrupt error instead of the misleading generic rejection message.
- Stop passing very large tasks through full-history `thread/read` calls on every state poll and stop request; carry the bound trajectory turn directly to `turn/interrupt`, use history-free App Server status checks for routine polling and race verification, and perform a full turn bootstrap only when no candidate exists or a rejected stale candidate must be rebound.
- Prevent a late routine state response from restoring a turn candidate that a concurrent Stop request already rejected as stale.
- Try every eligible Codex renderer before reporting a task-state or stop bridge failure, so an auxiliary iframe without the App Server bridge cannot mask a healthy page target.
- Remove the injected Browser entry from the previous CDP endpoint when the configured port changes while the watcher is enabled.
- Pause an active Goal through the App Server before interrupting its turn, and keep automatic quota stops latched across later turns in the same quota cycle as a compatibility fallback; manual Stop still rearms per turn, while quota recovery, window reset, or guard reconfiguration rearms automation.
- Reject linked, non-regular, multiply linked, and oversized private CDP settings, heartbeat, and watcher-lock files before decoding them.
- Show unknown record durations as `—` instead of `0 ms` when the log contains only a point timestamp, preserve genuinely measured zero-duration intervals, and round positive sub-millisecond durations up to `1 ms`.

## [0.3.0] - 2026-08-18

### Added

- Add a bilingual adaptive live trajectory window with current turn/step/index, latest event, status, cumulative Token counters, user-controlled entry/exit, visibility-aware serialized refreshes, and retry backoff—using the Codex side panel when the host bridge is available and browser-native canvas-to-video PiP otherwise, without a standalone app.
- Add an app-only safe-summary update tool that skips projection when an opaque rollout-lineage revision is unchanged and returns a bounded 50-record tail after changes.
- Add a compact transparent 32-frame whale-girl mining mascot inside the upper-left corner of the newest Codex live-record card. It stays within the card, plays exactly one cycle when a new record appears, remains idle across unchanged polls, and honors reduced-motion preferences.

### Changed

- Redesign the Codex live presentation as a full-height side panel with frozen whole-task Token totals and a chronological, independently scrolling event stream that follows the newest record at the bottom. Per-record Token deltas now use aligned Total/Input/Output rows with input and output subsets, while the mounted shell updates in place so one-second checks do not shake the status row.

## [0.2.0] - 2026-08-17

### Added

- Add a Token details panel with usage breakdowns, cache-hit rate, collapsed per-turn totals, and indexed ledger-row uncached-input, cached-input, and output usage.
- Add an exclusive `beforeRecord` cursor, pagination metadata, and a bilingual “Load earlier records” viewer button that prepends bounded pages without moving the ledger viewport.
- Show each turn's active model plus separate uncached-input, cache-read, and output totals.
- Validate that the plugin manifest, project metadata, runtime package, changelog, and release notes all agree on the tagged version.
- Support Codex paginated `history_base` lineages and canonical persisted turn items in addition to legacy rollouts.

### Fixed

- Preserve MCP completion failures when projecting the corresponding tool output.
- Stream JSONL parsing and retain only the requested record tail in memory.
- Show large Token totals in full with responsive numeric sizing instead of truncating values with an ellipsis.
- Remove the unsupported Cache writes metric and derive uncached input solely from input minus cache reads.
- Keep bounded tool correlation independent of the selected page so late terminal results remain visible without changing stable record indexes between requests.
- Negotiate only the MCP protocol version implemented by the server.
- Recover the interactive viewer after tool-level loading errors.
- Replace the sandbox-blocked full-detail modal with an inline confirmation flow.
- Deduplicate repeated cumulative Token snapshots so per-turn usage and `modelCalls` remain accurate.
- Return exact session matches immediately and cache unchanged session overviews by file signature.
- Use the documented top-level `mcpServers` wrapper in the bundled `.mcp.json` configuration.
- Fail closed when redacting paths and keep filesystem error responses free of absolute session paths.
- Defer incomplete UTF-8 tail writes while reporting malformed complete UTF-8 lines.
- Run the browser acceptance suite in the tagged release job before publishing artifacts.
- Upgrade the development test runner to a patched pytest release.
- Exclude copied parent context from paginated subagent trajectories and validate metadata identity, contiguous ordinals, complete-line byte boundaries, missing sources, ambiguity, and cycles across the complete lineage.
- Reject linked session files, path-like selectors, duplicate JSON keys, non-finite numbers, oversized integers, oversized JSONL/RPC messages, and explicit `null` MCP request IDs.
- Apply a deterministic JSON nesting limit across Python 3.10–3.14 instead of relying on interpreter recursion behavior.
- Keep overview and detailed turn/tool/Token statistics aligned across mismatched turn events, partial cumulative Token snapshots, and legacy/canonical duplicate terminal events.
- Preserve empty successful MCP results, assistant phases, review hints, aborted-turn semantics, and valid timing when adjacent records are malformed.
- Enforce a closed trajectory schema, restrictive viewer CSP and escaped dynamic markup, request timeouts, bounded projection state, portable archive names, safe permissions, and byte-identical ZIP/tar contents.

### Changed

- Expand only the latest ledger turn by default, make the complete turn summary row—including token totals—toggle expansion, give each expanded turn a group-scoped sticky column header, lazily render earlier turns and per-turn token rows, and keep viewer refreshes to a bounded 500-record tail.
- Rebalance the ledger around compact proportional Event and Content columns, keep every token column visible without horizontal scrolling, shorten the redundant Turn/step ledger column to Step, contain every header label within its cell, and reveal truncated values in an unclipped hover/focus tooltip.
- Include the complete DeepSeek Harness MIT License and clarify the adapted implementation's provenance and independence.
- Identify the Contributor Covenant 2.1 adaptation as CC BY 4.0 licensed material.
- Expand the CI runtime matrix across Linux, macOS, Windows and Python 3.10, 3.12, and 3.14; pin release actions by immutable commit, isolate release builds from shared dependency caches, and scope release permissions to the publishing job.

## [0.1.0] - 2026-08-14

### Added

- Read-only discovery and projection of local Codex session logs.
- Safe summary mode and explicit full-detail mode.
- Interactive timeline, filters, inspector, and bilingual interface.
- Cross-platform `uv` launcher for macOS, Linux, and Windows.
- Codex Git Marketplace distribution and MCP Apps resource.

[0.1.0]: https://github.com/icesixgod/codex-trajectory/releases/tag/v0.1.0
[0.2.0]: https://github.com/icesixgod/codex-trajectory/compare/v0.1.0...v0.2.0
[0.3.0]: https://github.com/icesixgod/codex-trajectory/compare/v0.2.0...v0.3.0
[0.3.1]: https://github.com/icesixgod/codex-trajectory/compare/v0.3.0...v0.3.1
[0.3.2]: https://github.com/icesixgod/codex-trajectory/compare/v0.3.1...v0.3.2
[0.4.0]: https://github.com/icesixgod/codex-trajectory/compare/v0.3.2...v0.4.0
[Unreleased]: https://github.com/icesixgod/codex-trajectory/compare/v0.4.0...HEAD

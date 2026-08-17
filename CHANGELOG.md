# Changelog

All notable changes follow [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Add a bilingual adaptive live trajectory window with current turn/step/index, latest event, status, cumulative Token counters, user-controlled entry/exit, visibility-aware serialized refreshes, and retry backoff—using the Codex side panel when the host bridge is available and browser-native canvas-to-video PiP otherwise, without a standalone app.
- Add an app-only safe-summary update tool that skips projection when an opaque rollout-lineage revision is unchanged and returns a bounded 50-record tail after changes.

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
[Unreleased]: https://github.com/icesixgod/codex-trajectory/compare/v0.2.0...HEAD

---
name: inspect-codex-trajectory
description: Inspect or show a local Codex task trajectory, including turns, approximate model steps, assistant messages, reasoning summaries, tool calls, failures, compaction, token usage, and timing. Use when the user asks for a trajectory, execution trace, task timeline, slow-tool analysis, visual event ledger, or live picture-in-picture tracker for Codex work.
---

# Inspect Codex Trajectory

Use the plugin's session MCP tools instead of opening raw files under the Codex home directory.

1. For the current task, use a caller-provided verified session ID, or call `list_codex_sessions` and select the matching exact ID before calling `show_codex_trajectory`. Omit the ID only when the user requests the latest modified active task; omission is not caller binding and excludes archives by default. Keep the default `detailLevel: summary`; this renders the interactive trajectory UI without tool inputs or outputs.
2. For a historical task, call `list_codex_sessions` (set `includeArchived: true` only when archived history is wanted), choose the exact session ID, then call `show_codex_trajectory` with that ID.
3. Use `get_codex_trajectory` when the user wants analysis without the interactive UI. Cite turn numbers and record indexes from its structured result. If the requested evidence predates the returned page, call it again with the response's `pagination.nextBeforeRecord` as `beforeRecord`; continue only as far back as the request requires.
4. When the user wants a live or picture-in-picture view, call `show_codex_trajectory` in summary mode. The user can click **Live window**. Codex uses its supported side panel with task Token totals, quota windows, and a chronological safe-summary event stream; regular Chromium hosts can use native video PiP. The native right-panel **Codex Trajectory** entry can also open the calling task without a model turn. Status and quotas reflect persisted samples. The viewer provides no stopping or automatic-stop feature. Do not call the app-only native-entry or live-update helpers as model tools.
5. Supply the exact selected session ID and use `detailLevel: full` only after the user explicitly asks to inspect full tool input or output. Treat those values as potentially sensitive and quote no more than the task requires. Never expose `session_meta.base_instructions`, encrypted reasoning, credentials, or environment variables.
6. Explain that Codex logs do not expose DeepSeek Harness step boundaries directly. The plugin starts a new approximate step when model output resumes after one or more tool results.
7. Treat read warnings as evidence of skipped malformed records. If a paginated lineage fails validation, report that the local task history is inconsistent; do not bypass the plugin by opening inherited rollout files directly.
8. A `read-limit-exceeded` result means the selected log exceeds its byte budget, not that the task is missing. The viewer's error page shows the size and lets the user choose a larger limit. Keep the default 512 MiB unless the user explicitly chooses or requests a larger budget; pass the chosen byte count as `maxReadBytes` with an explicit `sessionId` on subsequent reads. Never enable full details merely because a larger read budget was approved. Other line, page, cache, lineage, and filesystem checks continue to apply.

The tools never modify task logs. Session-list calls do update a private search index; its title contains up to 100 characters of the first prompt, so treat list results as potentially sensitive. Do not claim that list results contain no prompt text. They read local `sessions/` and, when requested, `archived_sessions/` beneath `CODEX_HOME` or `~/.codex`.

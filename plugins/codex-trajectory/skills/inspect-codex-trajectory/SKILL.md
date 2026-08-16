---
name: inspect-codex-trajectory
description: Inspect or show a local Codex task trajectory, including turns, approximate model steps, assistant messages, reasoning summaries, tool calls, failures, compaction, token usage, and timing. Use when the user asks for a trajectory, execution trace, task timeline, slow-tool analysis, or a visual event ledger for Codex work.
---

# Inspect Codex Trajectory

Use the plugin's read-only MCP tools instead of opening raw files under the Codex home directory.

1. For the active task or the most recently updated task, call `show_codex_trajectory` without a session ID. Keep the default `detailLevel: summary`; this renders the interactive trajectory UI without tool inputs or outputs.
2. For a historical task, call `list_codex_sessions`, choose the exact session ID, then call `show_codex_trajectory` with that ID.
3. Use `get_codex_trajectory` when the user wants analysis without the interactive UI. Cite turn numbers and record indexes from its structured result.
4. Use `detailLevel: full` only after the user explicitly asks to inspect full tool input or output. Treat those values as potentially sensitive and quote no more than the task requires. Never expose `session_meta.base_instructions`, encrypted reasoning, credentials, or environment variables.
5. Explain that Codex logs do not expose DeepSeek Harness step boundaries directly. The plugin starts a new approximate step when model output resumes after one or more tool results.
6. When diagnosing latency, compare recorded tool durations and turn timing. Do not infer provider latency from an instantaneous message row.
7. Treat read warnings as evidence of skipped malformed records. If a paginated lineage fails validation, report that the local task history is inconsistent; do not bypass the plugin by opening inherited rollout files directly.

The tools are read-only. They read local `sessions/` and, when requested, `archived_sessions/` beneath `CODEX_HOME` or `~/.codex`.

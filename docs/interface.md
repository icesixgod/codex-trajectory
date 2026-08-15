# Interface reference

Codex Trajectory exposes three read-only MCP tools and one `text/html;profile=mcp-app` resource at `ui://codex-trajectory/trajectory-v1.html`.

## Session resolution

Session identifiers must exactly match or unambiguously prefix an identifier discovered below `CODEX_HOME/sessions`; archived sessions are considered only when `includeArchived` is true. File paths are never accepted. Symlinks and files whose canonical path leaves an authorized root are excluded.

## Detail levels

`summary` is the default. It omits record input, output, and metadata; log source paths and Git remotes are never part of the public response. `full` includes bounded record details up to 12,000 characters per field. Both modes exclude `session_meta.base_instructions` and encrypted reasoning content. Persisted reasoning summaries remain visible.

## Projection behavior

`schemaVersion` is `1`. Turns use `task_started`, `task_complete`, and `turn_aborted` events where available. Because Codex rollout logs do not persist an authoritative model-step identifier, the projection begins step one with the first model response and increments the approximate step after a tool result when model output resumes.

Tool duration is measured between persisted call and output timestamps unless a completion event provides a duration. Explicit completion failures, including MCP `result.Err` values, remain failures when the later tool output is projected. Unknown event types are ignored. A malformed complete JSONL line produces a warning; an invalid final line without a newline is treated as an in-progress write and silently deferred.

The log is parsed incrementally, and only the last `maxRecords` projected records are retained and returned with stable original `index` values. Aggregate statistics continue to describe the complete parsed task.

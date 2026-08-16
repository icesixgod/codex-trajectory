# Privacy Policy

Codex Trajectory runs locally and reads Codex JSONL task logs from `CODEX_HOME/sessions` and, when requested for a target, `CODEX_HOME/archived_sessions`. Paginated targets may inherit earlier segments from either authorized root; copied parent context before a subagent's declared history boundary is excluded from that child trajectory.

The plugin does not edit task logs, persist a secondary copy, or add telemetry. Its Python runtime makes no application network requests; the `uv` launcher may provision a compatible Python according to the user's own uv configuration. It accepts session identifiers rather than paths and excludes descendant symlinks, file hardlinks, and files outside the configured roots. Safe summary mode is the default: tool inputs, tool outputs, raw record metadata, absolute log paths, Git remotes, base instructions, and encrypted reasoning are not returned. Full-detail mode requires an explicit tool parameter or confirmation in the viewer; it returns bounded tool inputs and outputs but still excludes base instructions and encrypted reasoning. All returned collections and text fields are bounded.

When a user invokes the plugin through Codex, structured results may become part of the active Codex conversation and may therefore be processed according to the user's Codex service configuration. Users should review task contents before enabling full details.

Questions or deletion requests do not apply to a project-operated service because this project stores no user data. Security-sensitive reports should follow [SECURITY.md](SECURITY.md).

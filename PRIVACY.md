# Privacy Policy

Codex Trajectory runs locally and reads Codex JSONL task logs from `CODEX_HOME/sessions` and, when requested, `CODEX_HOME/archived_sessions`.

The plugin does not edit task logs, persist a secondary copy, add telemetry, or make network requests. Safe summary mode is the default: tool inputs, tool outputs, raw record metadata, absolute log paths, Git remotes, base instructions, and encrypted reasoning are not returned. Full-detail mode requires an explicit tool parameter or confirmation in the viewer; it returns bounded tool inputs and outputs but still excludes base instructions and encrypted reasoning.

When a user invokes the plugin through Codex, structured results may become part of the active Codex conversation and may therefore be processed according to the user's Codex service configuration. Users should review task contents before enabling full details.

Questions or deletion requests do not apply to a project-operated service because this project stores no user data. Security-sensitive reports should follow [SECURITY.md](SECURITY.md).

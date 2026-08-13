# Security Policy

## Supported versions

Security fixes are provided for the latest released version.

## Reporting a vulnerability

Do not open a public issue for a vulnerability that may expose local task content or escape the authorized Codex session directories. Use GitHub's private vulnerability reporting for this repository. Include the affected version, platform, reproduction steps, and expected impact. You should receive an acknowledgement within seven days.

## Security model

The plugin is read-only, follows no session-log symlinks, accepts session identifiers rather than file paths, excludes base instructions and encrypted reasoning, and defaults to a reduced-detail response. It does not protect content after a user explicitly loads full details into a Codex conversation.

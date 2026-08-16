# Security Policy

## Supported versions

Security fixes are provided for the latest released version.

## Reporting a vulnerability

Do not open a public issue for a vulnerability that may expose local task content or escape the authorized Codex session directories. Use GitHub's private vulnerability reporting for this repository. Include the affected version, platform, reproduction steps, and expected impact. You should receive an acknowledgement within seven days.

## Security model

The plugin is read-only and accepts session identifiers rather than file paths. It resolves each configured root, then discovers only regular, single-link descendant files below the active or archived root; descendant symbolic links, file hardlinks, root escapes, ambiguous ID prefixes, and inconsistent paginated lineage boundaries fail closed. Session and JSON-RPC inputs have byte, collection, nesting, and numeric conversion bounds, and public filesystem failures do not include local paths.

Safe summary mode excludes raw inputs, outputs, metadata, base instructions, and encrypted reasoning. Full details remain bounded and still exclude base instructions and encrypted reasoning, but the plugin cannot protect content after a user explicitly loads those details into a Codex conversation. The viewer uses a restrictive Content Security Policy, escapes log-derived markup, and times out unanswered host requests.

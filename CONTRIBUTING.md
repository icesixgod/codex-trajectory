# Contributing

Thank you for improving Codex Trajectory. By participating, you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development

Install [uv](https://docs.astral.sh/uv/), clone the repository, then run:

```sh
uv sync --group dev
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run mypy --no-incremental --platform win32
uv run pytest --cov --cov-report=term-missing
uv run python scripts/validate_release.py
uv run --no-project --script scripts/smoke_mcp.py
```

`smoke_mcp.py` executes the command declared by the packaged `.mcp.json`; do not replace it with a hand-built equivalent command. The relative launcher executes `uv` directly on Unix. On Windows, the smoke verifies the reviewed binary hash, rejects a console-subsystem launcher, and exercises its inherited stdio path end to end. Rebuild `codex_trajectory_launcher.exe` only from its adjacent C source. The packaged binary uses the pinned Zig 0.14.1 cross-toolchain: run `uv run --no-project --script scripts/build_windows_launcher.py`, then repeat with `--check` to verify byte equality. CI runs the parity check. Update the reviewed SHA-256 in both release validation and the stdio smoke after reviewing a new binary. The source also documents an alternative Visual Studio x64 build; do not expect different toolchains to emit identical bytes. Native Windows stdio smoke remains required before a Windows release. Focused coverage gates protect the parser modules, native task-context layer, tool dispatcher, and MCP entry point.

Browser acceptance tests are opt-in locally:

```sh
RUN_UI_TESTS=1 uv run pytest -m ui -q
```

CI runs the browser suite on Linux and the native panel tests on Windows. Local tests emulate both the standard MCP Apps handshake and the legacy host bridge; they exercise actual rendering, pagination, live refresh, theme/locale changes, task selection, and teardown. For an installed candidate, run the same packaged stdio smoke with `--plugin-root` and optionally `--local-session` to check an exact real local task. See [native migration and local testing](docs/native-viewer.md).

Behavior changes need focused tests and corresponding English and Chinese documentation updates. Changes to session storage must cover plain and compressed rollouts on Python 3.10 and 3.14; native-entry changes must cover exact caller identity, absent/conflicting context, task isolation, host lifecycle, and supported-client integration. Never commit real Codex task logs, credentials, base instructions, encrypted reasoning, or unredacted screenshots.

Use conventional, imperative commit subjects. Pull requests should explain the user-visible behavior, privacy impact, and validation performed.

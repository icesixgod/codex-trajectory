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
uv run python scripts/smoke_mcp.py
```

`smoke_mcp.py` executes the command declared by the packaged `.mcp.json`; do not replace it with a hand-built equivalent command. The relative launcher executes `uv` directly on Unix. On Windows, the smoke verifies the reviewed binary hash, rejects a console-subsystem launcher, and exercises its inherited stdio path end to end. Rebuild `codex_trajectory_launcher.exe` only from its adjacent C source using the documented Visual Studio x64 commands; the `/Brepro` link makes repeated builds deterministic with the same toolchain. The default coverage run measures Python only, so the CI gates `browser_view.py`, `cdp_peer.py`, `cdp_settings.py`, the CDP transport, and the MCP entry point separately instead of relying only on the project-wide percentage.

Browser acceptance tests are opt-in locally:

```sh
RUN_UI_TESTS=1 uv run pytest -m ui -q
```

CI runs the full browser suite on Linux and the focused CDP browser transport on Windows. Before a Windows release that changes launcher, watcher, peer authentication, or injection behavior, install the candidate plugin, start Codex with loopback CDP enabled, open this repository as a Codex task, and run the installed-app canary from that task:

```powershell
$env:RUN_WINDOWS_CODEX_CANARY = '1'
uv run python scripts/smoke_windows_codex.py
```

The canary requires the optional shortcut to be enabled and verifies the outer `ChatGPT.exe` identity, live watcher state, authenticated CDP connection, and a visible session-ready shortcut in the real Codex DOM. It is intentionally not run on GitHub-hosted workers because they do not contain the packaged Codex desktop app.

Behavior changes need focused tests and corresponding English and Chinese documentation updates. Changes to session storage must cover plain and compressed rollouts on Python 3.10 and 3.14; CDP changes must cover disabled, cleanup-only, absolute-deadline, the real packaged Windows process-tree shape, and the installed-app canary when applicable. Never commit real Codex task logs, credentials, base instructions, encrypted reasoning, or unredacted screenshots.

Use conventional, imperative commit subjects. Pull requests should explain the user-visible behavior, privacy impact, and validation performed.

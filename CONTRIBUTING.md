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

Behavior changes need focused tests and corresponding English and Chinese documentation updates. Changes to session storage must cover plain and compressed rollouts on Python 3.10 and 3.14; CDP changes must cover disabled, cleanup-only, absolute-deadline, and Windows type-check paths. Never commit real Codex task logs, credentials, base instructions, encrypted reasoning, or unredacted screenshots.

Use conventional, imperative commit subjects. Pull requests should explain the user-visible behavior, privacy impact, and validation performed.

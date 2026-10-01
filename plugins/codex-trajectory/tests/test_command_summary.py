"""Command activity interpretation stays bounded and independent of private arguments."""

from __future__ import annotations

from typing import Any

import pytest
from codex_trajectory.command_summary import command_activity


@pytest.mark.parametrize(
    "command",
    [
        ["cat", "/private/account-key.txt"],
        ["cat", "pytest; git diff; private-secret"],
        ["/private/bin/cat", "private-argument"],
    ],
)
def test_file_arguments_are_data_and_never_become_activity_labels(command: list[str]) -> None:
    assert command_activity(command) == command_activity(["cat", "README.md"]) == "Read files"


def test_script_bodies_are_opaque_and_executable_paths_are_not_exposed() -> None:
    command = ["/private/python3.13", "-c", "import os; pytest(); cat('private-secret')"]
    assert command_activity(command) == "Run script"
    assert command_activity([r"C:\private\python.exe", "-m", "pytest", "private-secret"]) == (
        "Run tests"
    )


def test_shell_commands_are_distinguished_from_quoted_arguments_and_newlines() -> None:
    assert command_activity("grep 'pytest; cat private-secret' README.md") == "Search text"
    assert command_activity("cd /private; cat README.md\nuv run pytest") == "Read files + Run tests"
    assert command_activity(["/bin/zsh", "-lc", "cat /private/file | rg private-pattern"]) == (
        "Read files + Search text"
    )
    assert command_activity(["env", "PRIVATE_TOKEN=secret", "uv", "run", "pytest"]) == "Run tests"


def test_search_patterns_and_write_options_are_not_mistaken_for_read_only_modes() -> None:
    assert command_activity(["rg", "--files", "private-directory"]) == "List files"
    assert command_activity(["rg", "--", "--files"]) == "Search text"
    assert command_activity(["rg", "-e", "--files"]) == "Search text"
    assert command_activity(["sed", "-n", "-i", "private-expression", "private-file"]) == (
        "Run command"
    )


@pytest.mark.parametrize(
    "command",
    [
        "cat README.md; private-program --token private-secret",
        "cat $(private-program)",
        "cat README.md > /private/result",
        "python - <<'PY'\nprivate_code()\nPY",
        "for name in private-values; do cat file; done",
        "cat 'unclosed-private-value",
        ["private-program", "pytest", "cat", "README.md"],
        ["/bin/zsh", "-lc", "bash -c 'sh -c \"zsh -c pytest\"'"],
    ],
)
def test_unrecognized_or_ambiguous_commands_do_not_claim_a_known_purpose(command: Any) -> None:
    assert command_activity(command) == "Run command"


@pytest.mark.parametrize("command", [None, {}, ["cat", {}], [], "x" * 8_193, ["cat"] * 129])
def test_invalid_or_oversized_metadata_returns_only_the_fixed_fallback(command: Any) -> None:
    assert command_activity(command) == "Run command"

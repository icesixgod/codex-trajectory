"""Fixed command activity labels without exposing arguments or executing a shell."""

from __future__ import annotations

import re
import shlex
from itertools import islice
from typing import Any

UNKNOWN_ACTIVITY = "Run command"
MAX_COMMAND_CHARS = 8_192
MAX_COMMAND_TOKENS = 128


def command_activity(command: Any) -> str:
    """Describe known command forms with fixed labels; never return source text."""
    if isinstance(command, str):
        activities = _shell_activities(command, 0)
    elif isinstance(command, list) and 0 < len(command) <= MAX_COMMAND_TOKENS:
        if any(not isinstance(value, str) for value in command):
            return UNKNOWN_ACTIVITY
        argv = [value for value in command if isinstance(value, str)]
        if sum(map(len, argv)) > MAX_COMMAND_CHARS:
            return UNKNOWN_ACTIVITY
        activities = _argv_activities(argv, 0)
    else:
        return UNKNOWN_ACTIVITY
    if not activities or UNKNOWN_ACTIVITY in activities:
        return UNKNOWN_ACTIVITY
    unique = list(dict.fromkeys(activities))
    if len(unique) > 1 and "Change directory" in unique:
        unique.remove("Change directory")
    return " + ".join(unique) if len(unique) <= 4 else UNKNOWN_ACTIVITY


def _shell_activities(script: str, depth: int) -> list[str]:
    if depth > 4 or len(script) > MAX_COMMAND_CHARS or "$(" in script or "`" in script:
        return [UNKNOWN_ACTIVITY]
    lexer = shlex.shlex(script, posix=True, punctuation_chars=";&|()<>")
    lexer.whitespace = " \t\r"
    try:
        tokens = list(islice(lexer, MAX_COMMAND_TOKENS + 1))
    except ValueError:
        return [UNKNOWN_ACTIVITY]
    if len(tokens) > MAX_COMMAND_TOKENS or any(
        token and all(char in "()<>" for char in token) for token in tokens
    ):
        return [UNKNOWN_ACTIVITY]
    activities: list[str] = []
    argv: list[str] = []
    for token in [*tokens, ";"]:
        if token in {";", "&&", "||", "|", "&", "\n"}:
            if argv:
                activities.extend(_argv_activities(argv, depth + 1))
                argv = []
        else:
            argv.append(token)
    return activities


def _argv_activities(argv: list[str], depth: int) -> list[str]:
    if depth > 4:
        return [UNKNOWN_ACTIVITY]
    while argv and re.match(r"^[A-Za-z_]\w*=", argv[0]):
        argv = argv[1:]
    if not argv:
        return [UNKNOWN_ACTIVITY]
    name = re.split(r"[/\\]", argv[0])[-1].casefold().removesuffix(".exe")
    args = argv[1:]
    if name in {"sh", "bash", "zsh"}:
        if len(args) >= 2 and re.fullmatch(r"-[eil]*c[il]*", args[0]):
            return _shell_activities(args[1], depth + 1)
        return [UNKNOWN_ACTIVITY]
    if name == "env":
        return _argv_activities(args, depth + 1)
    if name == "uv":
        if args[:1] == ["run"]:
            rest = args[1:]
            while rest and rest[0] in {"--no-project", "--no-sync", "--offline", "--frozen", "--"}:
                rest = rest[1:]
            if rest[:1] == ["--script"]:
                return ["Run script"]
            return _argv_activities(rest, depth + 1)
        if args[:1] == ["sync"] or args[:2] == ["pip", "install"]:
            return ["Install dependencies"]
    if re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", name):
        if len(args) >= 2 and args[0] == "-m":
            if args[1] in {"pytest", "unittest"}:
                return ["Run tests"]
            if args[1] in {"mypy", "ruff"}:
                return _argv_activities(args[1:], depth + 1)
            if args[1] == "pip" and args[2:3] == ["install"]:
                return ["Install dependencies"]
            if args[1] == "build":
                return ["Build project"]
        return ["Run script"]
    if name == "pytest":
        return ["Run tests"]
    if name in {"mypy", "pyright"}:
        return ["Check types"]
    if name == "ruff":
        if args[:1] == ["format"]:
            return ["Check formatting" if "--check" in args else "Format code"]
        if args[:1] == ["check"]:
            return ["Check code style"]
    if name in {"npm", "pnpm", "yarn", "bun"}:
        rest = args[1:] if args[:1] in (["run"], ["run-script"]) else args
        if rest[:1] in (["test"], ["build"], ["lint"]):
            return [
                {"test": "Run tests", "build": "Build project", "lint": "Check code style"}[rest[0]]
            ]
        if args[:1] in (["install"], ["ci"], ["add"]):
            return ["Install dependencies"]
    if name in {"cargo", "go"} and args:
        activity = {
            "test": "Run tests",
            "build": "Build project",
            "check": "Check types",
            "vet": "Check code style",
        }.get(args[0])
        if activity:
            return [activity]
    if name == "git" and args:
        activity = {
            "status": "Inspect workspace",
            "diff": "Inspect changes",
            "log": "Inspect history",
            "show": "Inspect history",
        }.get(args[0])
        if activity:
            return [activity]
    if name == "rg":
        skip_value = False
        for arg in args:
            if skip_value:
                skip_value = False
            elif arg == "--":
                break
            elif arg in {"-e", "--regexp", "-f", "--file", "-g", "--glob", "--iglob"}:
                skip_value = True
            elif arg == "--files":
                return ["List files"]
        return ["Search text"]
    if name in {"grep", "egrep", "fgrep"}:
        return ["Search text"]
    if name in {"cat", "head", "tail", "less", "more"}:
        return ["Read files"]
    if name == "sed" and args[:1] == ["-n"] and not any(arg.startswith("-i") for arg in args):
        return ["Read files"]
    if name in {"ls", "dir"}:
        return ["List files"]
    if name in {"pwd", "uname", "which", "where", "whoami"}:
        return ["Inspect environment"]
    if name == "cd":
        return ["Change directory"]
    return [UNKNOWN_ACTIVITY]

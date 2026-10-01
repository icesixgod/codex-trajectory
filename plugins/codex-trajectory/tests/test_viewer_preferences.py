"""Full-detail consent persistence and privacy-preserving defaults."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from codex_trajectory import tools, viewer_preferences
from codex_trajectory.viewer_preferences import (
    MAX_PREFERENCE_BYTES,
    default_full_details,
    preferences_path,
    set_default_full_details,
)


def test_preferences_start_safe_without_creating_a_file(codex_home: Path) -> None:
    assert default_full_details() is False
    result = tools.call_tool("get_codex_trajectory_preferences", {})
    assert result["structuredContent"] == {"defaultFullDetails": False}
    assert not preferences_path().exists()


def test_remember_and_revoke_persist_only_the_flag(codex_home: Path) -> None:
    result = tools.call_tool("set_codex_trajectory_preferences", {"defaultFullDetails": True})
    assert result["structuredContent"] == {"defaultFullDetails": True}
    path = preferences_path()
    assert json.loads(path.read_text()) == {"schemaVersion": 1, "defaultFullDetails": True}
    assert path.stat().st_size < MAX_PREFERENCE_BYTES
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    assert default_full_details() is True
    # Existing public and native reads remain summaries; only the viewer opts in.
    for name in ["get_codex_trajectory", "show_codex_trajectory"]:
        trajectory = tools.call_tool(name, {"sessionId": "session-alpha"})["structuredContent"]
        assert trajectory["detailLevel"] == "summary"
        assert "secret-tool-input" not in json.dumps(trajectory)
    native = tools.call_tool("open_codex_trajectory", {}, metadata={"threadId": "session-alpha"})
    assert native["structuredContent"]["detailLevel"] == "summary"
    assert tools.call_tool("get_codex_trajectory_preferences", {})["structuredContent"] == {
        "defaultFullDetails": True
    }
    set_default_full_details(False)
    assert default_full_details() is False
    assert json.loads(path.read_text())["defaultFullDetails"] is False
    assert not list(path.parent.glob(".*.tmp"))


@pytest.mark.parametrize(
    "raw",
    [
        "invalid",
        "[]",
        '{"schemaVersion":true,"defaultFullDetails":true}',
        '{"schemaVersion":2,"defaultFullDetails":true}',
        '{"schemaVersion":1,"defaultFullDetails":"true"}',
        '{"schemaVersion":1,"defaultFullDetails":1}',
        '{"schemaVersion":1,"defaultFullDetails":true,"sessionId":"private"}',
        '{"schemaVersion":1,"defaultFullDetails":false,"defaultFullDetails":true}',
        '{"schemaVersion":1}',
        '{"schemaVersion":1,"defaultFullDetails":true}' + " " * MAX_PREFERENCE_BYTES,
    ],
)
def test_invalid_preferences_never_enable_full_details(codex_home: Path, raw: str) -> None:
    path = preferences_path()
    path.parent.mkdir(parents=True)
    path.write_text(raw)
    assert default_full_details() is False


@pytest.mark.parametrize("link_kind", ["file", "directory", "hardlink"])
def test_preferences_refuse_linked_sources_and_writes(
    codex_home: Path, tmp_path: Path, link_kind: str
) -> None:
    path = preferences_path()
    target = tmp_path / "linked-preferences.json"
    target.write_text('{"schemaVersion":1,"defaultFullDetails":true}')
    try:
        if link_kind == "directory":
            directory = tmp_path / "linked-directory"
            directory.mkdir()
            (directory / path.name).write_bytes(target.read_bytes())
            path.parent.symlink_to(directory, target_is_directory=True)
        else:
            path.parent.mkdir(parents=True)
            if link_kind == "hardlink":
                os.link(target, path)
            else:
                path.symlink_to(target)
    except OSError:
        pytest.skip("This platform does not permit creating the required link.")
    assert default_full_details() is False
    original = target.read_bytes()
    with pytest.raises(OSError):
        set_default_full_details(False)
    assert target.read_bytes() == original
    result = tools.call_tool("set_codex_trajectory_preferences", {"defaultFullDetails": False})
    assert result["isError"] is True
    assert str(tmp_path) not in json.dumps(result)


def test_failed_atomic_save_preserves_previous_consent(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    set_default_full_details(False)
    original = preferences_path().read_bytes()

    def fail(*args: object) -> None:
        raise OSError("sensitive local path")

    monkeypatch.setattr(viewer_preferences.os, "replace", fail)
    result = tools.call_tool("set_codex_trajectory_preferences", {"defaultFullDetails": True})
    assert result["isError"] is True
    assert "sensitive local path" not in json.dumps(result)
    assert preferences_path().read_bytes() == original
    assert default_full_details() is False
    assert len(list(preferences_path().parent.iterdir())) == 1


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("get_codex_trajectory_preferences", {"defaultFullDetails": True}),
        ("set_codex_trajectory_preferences", {}),
        ("set_codex_trajectory_preferences", {"defaultFullDetails": 1}),
        ("set_codex_trajectory_preferences", {"defaultFullDetails": "true"}),
        ("set_codex_trajectory_preferences", {"defaultFullDetails": True, "sessionId": "x"}),
    ],
)
def test_preference_tools_reject_ambiguous_opt_ins(
    codex_home: Path, name: str, arguments: dict[str, object]
) -> None:
    assert tools.call_tool(name, arguments)["isError"] is True
    assert not preferences_path().exists()


def test_preference_tools_are_app_only_and_describe_writes() -> None:
    catalog = {tool["name"]: tool for tool in tools.tool_definitions()}
    for name in ["get_codex_trajectory_preferences", "set_codex_trajectory_preferences"]:
        tool = catalog[name]
        assert tool["_meta"]["ui"]["visibility"] == ["app"]
        assert tool["_meta"]["openai/visibility"] == "private"
        assert tool["inputSchema"]["additionalProperties"] is False
        assert tool["annotations"]["readOnlyHint"] is name.startswith("get_")

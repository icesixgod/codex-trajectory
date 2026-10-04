"""Native entrypoint binding and safe fallback behavior."""

from __future__ import annotations

import json
from base64 import b64decode
from pathlib import Path
from typing import Any

import pytest
from codex_trajectory import projection
from codex_trajectory.native_view import calling_thread_id
from codex_trajectory.protocol import JsonRpcError, handle
from conftest import rollout_events, write_rollout


def open_panel(metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"name": "open_codex_trajectory", "arguments": {}}
    if metadata is not None:
        params["_meta"] = metadata
    return handle("tools/call", params)


def test_native_entrypoint_is_app_only_and_read_only() -> None:
    tool = next(
        item for item in projection.tool_definitions() if item["name"] == "open_codex_trajectory"
    )
    assert tool["_meta"]["openai/ui"]["entrypoints"] == [{"type": "global"}, {"type": "thread"}]
    assert tool["_meta"]["ui"] == {"resourceUri": projection.UI_URI, "visibility": ["app"]}
    assert tool["_meta"]["openai/visibility"] == "private"
    assert tool["annotations"]["readOnlyHint"] is True
    assert tool["inputSchema"]["additionalProperties"] is False


def test_native_entrypoint_embeds_the_packaged_icon() -> None:
    tool = next(
        item for item in projection.tool_definitions() if item["name"] == "open_codex_trajectory"
    )
    icon = tool["icons"][0]
    prefix, data = icon["src"].split(",", 1)
    assert prefix == "data:image/png;base64"
    assert icon["mimeType"] == "image/png"
    assert (
        b64decode(data, validate=True)
        == (Path(__file__).parents[1] / "assets" / "icon.png").read_bytes()
    )


def test_sidebar_without_context_selects_before_reading_a_trajectory(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unexpected_read(*args: Any, **kwargs: Any) -> dict[str, Any]:
        pytest.fail("A sidebar entry without task context must wait for explicit selection.")

    monkeypatch.setattr(projection, "trajectory_result", unexpected_read)
    result = open_panel()
    assert result["structuredContent"] == {
        "viewerState": "select-session",
        "reason": "missing-context",
    }
    assert result["_meta"]["ui"]["resourceUri"] == projection.UI_URI


def test_native_panel_binds_exact_caller_instead_of_latest(codex_home: Path) -> None:
    write_rollout(codex_home / "sessions" / "rollout-newer.jsonl", rollout_events("session-newer"))
    result = open_panel({"thread_id": "session-alpha", "threadId": "session-alpha"})
    trajectory = result["structuredContent"]
    assert trajectory["session"]["id"] == "session-alpha"
    assert trajectory["detailLevel"] == "summary"
    assert all(
        record["input"] is None and record["output"] is None for record in trajectory["records"]
    )
    assert "secret-tool-input" not in json.dumps(result)


def test_native_context_is_per_call_and_supports_archived_task(codex_home: Path) -> None:
    assert (
        open_panel({"threadId": "session-alpha"})["structuredContent"]["session"]["id"]
        == "session-alpha"
    )
    assert (
        open_panel({"thread_id": "session-archive"})["structuredContent"]["session"]["id"]
        == "session-archive"
    )
    assert open_panel()["structuredContent"] == {
        "viewerState": "select-session",
        "reason": "missing-context",
    }


@pytest.mark.parametrize(
    "metadata",
    [
        {"threadId": "../secret"},
        {"thread_id": 5},
        {"threadId": None},
        {"thread_id": "session-alpha", "threadId": "session-archive"},
        {"threadId": "session-alpha "},
        {"threadId": "x" * 129},
    ],
)
def test_invalid_context_never_substitutes_a_task(
    codex_home: Path, metadata: dict[str, Any]
) -> None:
    assert open_panel(metadata)["structuredContent"] == {
        "viewerState": "select-session",
        "reason": "invalid-context",
    }


@pytest.mark.parametrize("identifier", ["session-missing", "session-a", "session-al"])
def test_native_task_context_requires_an_exact_local_id(codex_home: Path, identifier: str) -> None:
    assert open_panel({"threadId": identifier})["structuredContent"] == {
        "viewerState": "select-session",
        "reason": "task-unavailable",
    }


def test_unrelated_metadata_and_process_environment_are_not_task_identity(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_THREAD_ID", "session-alpha")
    assert calling_thread_id({"openai/session": "session-alpha"}) is None
    assert (
        open_panel({"openai/session": "session-alpha"})["structuredContent"]["viewerState"]
        == "select-session"
    )


def test_native_failure_hides_paths(codex_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise OSError("private local path")

    monkeypatch.setattr(projection, "trajectory_result", fail)
    result = open_panel({"threadId": "session-alpha"})
    assert result["structuredContent"]["reason"] == "task-unavailable"
    assert "private local path" not in json.dumps(result)


@pytest.mark.parametrize("compressed", [False, True])
@pytest.mark.parametrize("metadata_state", ["absent", "missing-id", "invalid-id"])
def test_native_success_cannot_bind_filename_without_metadata_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compressed: bool,
    metadata_state: str,
) -> None:
    from test_sessions import _zstd_compress

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    task_id = "10101010-1010-4010-8010-101010101010"
    events = rollout_events(task_id)
    if metadata_state == "absent":
        events = [event for event in events if event["type"] != "session_meta"]
    else:
        events[0]["payload"].pop("id", None)
        events[0]["payload"].pop("session_id", None)
        if metadata_state == "invalid-id":
            events[0]["payload"]["id"] = None
    path = write_rollout(tmp_path / "sessions" / f"{task_id}.jsonl", events)
    if compressed:
        raw = path.read_bytes()
        path.unlink()
        path = path.with_suffix(".jsonl.zst")
        path.write_bytes(_zstd_compress(raw))
    assert open_panel({"thread_id": task_id})["structuredContent"] == {
        "viewerState": "select-session",
        "reason": "task-unavailable",
    }
    # Explicit public reads retain their legacy filename compatibility.
    public = projection.trajectory_result({"sessionId": task_id}, False)
    assert public["structuredContent"]["records"]


def test_native_open_rejects_prompt_arguments_and_malformed_metadata(codex_home: Path) -> None:
    result = handle(
        "tools/call", {"name": "open_codex_trajectory", "arguments": {"sessionId": "session-alpha"}}
    )
    assert result["isError"] is True
    with pytest.raises(JsonRpcError, match="metadata"):
        handle("tools/call", {"name": "open_codex_trajectory", "_meta": []})

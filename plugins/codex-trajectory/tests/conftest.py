"""Shared rollout fixtures for Codex Trajectory tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


def rollout_events(session_id: str = "session-alpha") -> list[dict[str, Any]]:
    """Return a two-turn rollout covering every supported record family."""
    return [
        {
            "timestamp": "2026-08-14T00:00:00.000Z",
            "type": "session_meta",
            "payload": {
                "session_id": session_id,
                "cwd": str(Path.home() / "work" / "project"),
                "base_instructions": "private system instructions",
                "originator": "codex",
                "source": "cli",
                "git": {
                    "branch": "main",
                    "commit_hash": "abc123",
                    "repository_url": "https://user:secret@example.invalid/private.git",
                },
            },
        },
        {
            "timestamp": "2026-08-14T00:00:00.010Z",
            "type": "turn_context",
            "payload": {
                "model": "gpt-test",
                "effort": "high",
                "collaboration_mode": {"mode": "default"},
            },
        },
        {
            "timestamp": "2026-08-14T00:00:01.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Inspect the task", "images": [1]},
        },
        {
            "timestamp": "2026-08-14T00:00:01.010Z",
            "type": "event_msg",
            "payload": {
                "type": "task_started",
                "turn_id": "turn-1",
                "started_at": 1786665601,
                "model_context_window": 100000,
            },
        },
        {
            "timestamp": "2026-08-14T00:00:02.000Z",
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "id": "reason-1",
                "summary": [{"type": "summary_text", "text": "Plan the check"}],
                "encrypted_content": "opaque-secret-reasoning",
            },
        },
        {
            "timestamp": "2026-08-14T00:00:03.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "id": "call-item-1",
                "call_id": "call-1",
                "name": "exec",
                "arguments": '{"cmd":"printf secret-tool-input"}',
            },
        },
        {
            "timestamp": "2026-08-14T00:00:04.250Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call-1",
                "output": {"output": "secret-tool-output", "exit_code": 0},
            },
        },
        {
            "timestamp": "2026-08-14T00:00:05.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "id": "message-1",
                "role": "assistant",
                "phase": "final",
                "content": [{"type": "output_text", "text": "Done"}],
            },
        },
        {
            "timestamp": "2026-08-14T00:00:05.010Z",
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": 100,
                        "output_tokens": 10,
                        "total_tokens": 110,
                    },
                    "last_token_usage": {
                        "input_tokens": 100,
                        "output_tokens": 10,
                        "total_tokens": 110,
                    },
                    "model_context_window": 100000,
                },
                "rate_limits": {
                    "primary": {
                        "used_percent": 31.5,
                        "window_minutes": 300,
                        "resets_at": 1786672800,
                    },
                    "secondary": {
                        "used_percent": 56,
                        "window_minutes": 10080,
                        "resets_at": 1787270400,
                    },
                },
            },
        },
        {
            "timestamp": "2026-08-14T00:00:05.500Z",
            "type": "event_msg",
            "payload": {
                "type": "sub_agent_activity",
                "kind": "started",
                "agent_path": "/root/reviewer",
                "agent_thread_id": "agent-1",
                "event_id": "event-1",
            },
        },
        {
            "timestamp": "2026-08-14T00:00:05.600Z",
            "type": "compacted",
            "payload": {"replacement": "private compaction body"},
        },
        {
            "timestamp": "2026-08-14T00:00:06.000Z",
            "type": "event_msg",
            "payload": {
                "type": "task_complete",
                "turn_id": "turn-1",
                "started_at": 1786665601,
                "completed_at": 1786665606,
                "duration_ms": 5000,
                "time_to_first_token_ms": 900,
            },
        },
        {
            "timestamp": "2026-08-14T00:00:07.000Z",
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "Try failure"},
        },
        {
            "timestamp": "2026-08-14T00:00:07.010Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn-2"},
        },
        {
            "timestamp": "2026-08-14T00:00:08.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "id": "call-item-2",
                "call_id": "call-2",
                "namespace": "demo",
                "name": "fail",
                "input": {"value": 1},
            },
        },
        {
            "timestamp": "2026-08-14T00:00:09.000Z",
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call-2",
                "output": {"isError": True, "message": "expected failure"},
            },
        },
        {
            "timestamp": "2026-08-14T00:00:09.500Z",
            "type": "response_item",
            "payload": {
                "type": "agent_message",
                "id": "agent-message-1",
                "author": "reviewer",
                "recipient": "root",
                "content": [{"text": "Review complete"}],
            },
        },
        {
            "timestamp": "2026-08-14T00:00:10.000Z",
            "type": "event_msg",
            "payload": {"type": "turn_aborted", "reason": "user cancelled"},
        },
        {
            "timestamp": "2026-08-14T00:00:11.000Z",
            "type": "future_unknown_event",
            "payload": {"type": "future_unknown_payload", "private": "ignored"},
        },
    ]


def write_rollout(path: Path, events: list[dict[str, Any]] | None = None) -> Path:
    """Write a complete rollout fixture."""
    path.parent.mkdir(parents=True, exist_ok=True)
    values = events if events is not None else rollout_events()
    path.write_text("".join(json.dumps(event) + "\n" for event in values), encoding="utf-8")
    return path


@pytest.fixture
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide an isolated Codex home with one active and one archived task."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    write_rollout(tmp_path / "sessions" / "2026" / "rollout-alpha.jsonl")
    write_rollout(
        tmp_path / "archived_sessions" / "rollout-archive.jsonl",
        rollout_events("session-archive"),
    )
    return tmp_path

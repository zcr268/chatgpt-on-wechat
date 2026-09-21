"""Tests for incremental DB persistence and crash-resume (#3179)."""

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from agent.memory.conversation_store import ConversationStore


@pytest.fixture
def store(tmp_path):
    db = ConversationStore(tmp_path / "test.db", agent_id="test-agent")
    yield db
    db._connect().close()


def test_load_run_messages_returns_tool_chains(store):
    """load_run_messages returns raw messages including tool_use/tool_result."""
    store.append_messages("s1", [
        {"role": "user", "content": [{"type": "text", "text": "hello"}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "read", "input": {}}
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "data"}
        ]},
    ], run_id="run-abc")

    msgs = store.load_run_messages("s1", "run-abc")
    assert len(msgs) == 3
    assert msgs[1]["content"][0]["type"] == "tool_use"
    assert msgs[2]["content"][0]["type"] == "tool_result"


def test_get_unfinished_run_finds_running(store):
    """A run with status='running' is detectable for resume."""
    store.create_run(run_id="run-1", session_id="s1", status="running")
    result = store.get_unfinished_run("s1")
    assert result is not None
    assert result["run_id"] == "run-1"

    store.finish_run("run-1", status="done")
    assert store.get_unfinished_run("s1") is None


def test_load_messages_excludes_unfinished_run(store):
    """Normal history load skips messages from a still-running run."""
    store.append_messages("s1", [
        {"role": "user", "content": [{"type": "text", "text": "finished"}]},
    ], run_id="run-done")

    store.create_run(run_id="run-crash", session_id="s1", status="running")
    store.append_messages("s1", [
        {"role": "user", "content": [{"type": "text", "text": "crashed turn"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "partial"}]},
    ], run_id="run-crash")

    # Normal load only sees the finished run
    msgs = store.load_messages("s1", max_turns=30)
    assert len(msgs) == 1
    assert msgs[0]["content"][0]["text"] == "finished"

    # Resume load sees the crashed run's messages
    crashed = store.load_run_messages("s1", "run-crash")
    assert len(crashed) == 2


def test_load_messages_returns_finished_runs(store):
    """Completed runs appear in normal history load."""
    store.create_run(run_id="run-ok", session_id="s1", status="running")
    store.append_messages("s1", [
        {"role": "user", "content": [{"type": "text", "text": "q"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "a"}]},
    ], run_id="run-ok")
    store.finish_run("run-ok", status="done")

    msgs = store.load_messages("s1", max_turns=30)
    assert len(msgs) == 2
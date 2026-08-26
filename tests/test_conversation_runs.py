"""Tests for run tracking in the conversation store: the runs table, the
messages.run_id column, and how a message picks up its run id."""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.memory.conversation_store import ConversationStore
from common.runtime_identity import identity_scope


def _store(tmpdir):
    return ConversationStore(Path(tmpdir) / "index.db")


def _message_run_ids(db_path, session_id):
    conn = sqlite3.connect(str(db_path))
    try:
        return [
            row[0]
            for row in conn.execute(
                "SELECT run_id FROM messages WHERE session_id = ? ORDER BY seq ASC",
                (session_id,),
            ).fetchall()
        ]
    finally:
        conn.close()


def test_runs_table_and_message_column_exist():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "index.db"
        ConversationStore(db)
        conn = sqlite3.connect(str(db))
        try:
            run_cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)")}
            assert {
                "run_id", "agent_id", "user_id", "session_id", "parent_run_id",
                "task_id", "task_source", "status", "started_at", "ended_at",
                "error", "extras",
            } <= run_cols
            msg_cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
            assert "run_id" in msg_cols
        finally:
            conn.close()


def test_create_run_is_idempotent():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        assert store.create_run("r1", agent_id="sales", session_id="s1") is True
        # A retried entry point must not duplicate the row or reset its fields.
        assert store.create_run("r1", agent_id="other") is False
        run = store.get_run("r1")
        assert run["agent_id"] == "sales"
        assert run["status"] == "running"
        assert run["ended_at"] is None


def test_finish_run_sets_status_and_merges_extras():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.create_run("r1", session_id="s1", extras={"a": 1})
        assert store.finish_run("r1", status="done", extras={"b": 2}) is True
        run = store.get_run("r1")
        assert run["status"] == "done"
        assert run["ended_at"] is not None
        assert run["extras"] == {"a": 1, "b": 2}
        # Finishing a run that does not exist reports failure rather than raising.
        assert store.finish_run("missing", status="done") is False


def test_external_task_handle_is_free_form_text():
    """task_id/task_source must hold an external id, not a foreign key we own."""
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.create_run(
            "r1", session_id="s1", task_id="T-260826-001", task_source="linkai"
        )
        found = store.list_runs(task_source="linkai", task_id="T-260826-001")
        assert [r["run_id"] for r in found] == ["r1"]


def test_list_runs_filters_parent_and_session():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.create_run("root", session_id="s1")
        store.create_run("child_a", session_id="s1", parent_run_id="root")
        store.create_run("child_b", session_id="s1", parent_run_id="root")
        store.create_run("other", session_id="s2")

        children = store.list_runs(parent_run_id="root")
        assert {r["run_id"] for r in children} == {"child_a", "child_b"}

        # parent_run_id="" selects top-level runs only.
        top_level = {r["run_id"] for r in store.list_runs(parent_run_id="")}
        assert top_level == {"root", "other"}

        s1 = {r["run_id"] for r in store.list_runs(session_id="s1")}
        assert s1 == {"root", "child_a", "child_b"}


def test_append_messages_records_explicit_run_id():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.append_messages(
            "s1", [{"role": "user", "content": "hi"}], run_id="r1"
        )
        assert _message_run_ids(Path(tmp) / "index.db", "s1") == ["r1"]


def test_append_messages_falls_back_to_ambient_run_id():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        with identity_scope(run_id="ambient"):
            store.append_messages("s1", [{"role": "user", "content": "hi"}])
        assert _message_run_ids(Path(tmp) / "index.db", "s1") == ["ambient"]


def test_per_message_run_id_overrides_batch():
    with tempfile.TemporaryDirectory() as tmp:
        store = _store(tmp)
        store.append_messages(
            "s1",
            [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "yo", "run_id": "special"},
            ],
            run_id="batch",
        )
        assert _message_run_ids(Path(tmp) / "index.db", "s1") == ["batch", "special"]


def test_legacy_db_is_migrated():
    """A database predating run tracking gains the runs table and the run_id
    column, and its existing rows default to an empty run id."""
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "index.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(
            """
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY, created_at INTEGER,
                last_active INTEGER, msg_count INTEGER DEFAULT 0
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
                seq INTEGER, role TEXT, content TEXT, created_at INTEGER,
                UNIQUE(session_id, seq)
            );
            INSERT INTO sessions VALUES ('old', 1, 1, 1);
            INSERT INTO messages (session_id, seq, role, content, created_at)
                VALUES ('old', 0, 'user', '"legacy"', 1);
            """
        )
        conn.commit()
        conn.close()

        ConversationStore(db)

        conn = sqlite3.connect(str(db))
        try:
            assert conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='runs'"
            ).fetchone()
            msg_cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
            assert "run_id" in msg_cols
            old = conn.execute(
                "SELECT run_id FROM messages WHERE session_id = 'old'"
            ).fetchone()[0]
            assert old == ""
        finally:
            conn.close()

"""Tests for shared msg thread retention cleanup."""

from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from claude_code_tools.msg import cleanup as cleanup_mod
from claude_code_tools.msg.cli import cli
from claude_code_tools.msg.migrations import CURRENT_SCHEMA_VERSION
from claude_code_tools.msg.models import AgentKind
from claude_code_tools.msg.store import MsgStore
from claude_code_tools.msg.watcher import Watcher

SECRET = "SECRET-CLEANUP-TOKEN-42"


@pytest.fixture
def store(tmp_path):
    return MsgStore(str(tmp_path / "msg.db"))


@pytest.fixture
def agents(store):
    a = store.register_agent("a", "%1", "t", AgentKind.CLAUDE, "/tmp/s")
    b = store.register_agent("b", "%2", "t", AgentKind.CODEX, "/tmp/s")
    return a, b


def _iso_ago(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _make_thread(
    store,
    sender,
    recipient,
    *,
    title: str,
    body: str = "ordinary body",
    delivery_state: str = "read",
    age_days: int = 40,
    read_age_days: int | None = None,
):
    """Create a thread whose activity is backdated to the given age."""
    ts = _iso_ago(age_days)
    thread = store.create_thread(
        title, sender.session_id,
        [sender.session_id, recipient.session_id],
    )
    store.send_message(thread.id, sender.session_id, body)
    future = (
        datetime.now(timezone.utc) + timedelta(hours=1)
    ).isoformat()
    read_ts = _iso_ago(read_age_days) if read_age_days is not None else ts
    with sqlite3.connect(store.db_path) as conn:
        conn.execute(
            "UPDATE threads SET created_at=? WHERE id=?",
            (ts, thread.id),
        )
        conn.execute(
            "UPDATE messages SET created_at=? WHERE thread_id=?",
            (ts, thread.id),
        )
        conn.execute(
            """UPDATE deliveries SET created_at=?
            WHERE message_id IN (
                SELECT id FROM messages WHERE thread_id=?
            )""",
            (ts, thread.id),
        )
        if delivery_state == "read":
            conn.execute(
                """UPDATE deliveries SET state='read',
                    claimed_by=NULL, claim_expires_at=NULL,
                    notified_at=NULL, read_at=?
                WHERE message_id IN (
                    SELECT id FROM messages WHERE thread_id=?
                )""",
                (read_ts, thread.id),
            )
        elif delivery_state == "notified":
            conn.execute(
                """UPDATE deliveries SET state='notified',
                    claimed_by=NULL, claim_expires_at=NULL,
                    notified_at=?, read_at=NULL
                WHERE message_id IN (
                    SELECT id FROM messages WHERE thread_id=?
                )""",
                (ts, thread.id),
            )
        elif delivery_state == "pending":
            conn.execute(
                """UPDATE deliveries SET state='pending',
                    claimed_by=NULL, claim_expires_at=NULL,
                    notified_at=NULL, read_at=NULL
                WHERE message_id IN (
                    SELECT id FROM messages WHERE thread_id=?
                )""",
                (thread.id,),
            )
        elif delivery_state == "claimed":
            conn.execute(
                """UPDATE deliveries SET state='claimed',
                    claimed_by='someone', claim_expires_at=?,
                    notified_at=NULL, read_at=NULL
                WHERE message_id IN (
                    SELECT id FROM messages WHERE thread_id=?
                )""",
                (future, thread.id),
            )
        elif delivery_state == "failed":
            conn.execute(
                """UPDATE deliveries SET state='failed',
                    notify_attempts=3, last_error='tmux failed',
                    claimed_by=NULL, claim_expires_at=NULL,
                    notified_at=NULL, read_at=NULL
                WHERE message_id IN (
                    SELECT id FROM messages WHERE thread_id=?
                )""",
                (thread.id,),
            )
        else:  # pragma: no cover - defensive
            raise ValueError(delivery_state)
    return thread


def _run_cleanup(store, days=30):
    plan = cleanup_mod.build_cleanup_plan(store.db_path, days)
    return cleanup_mod.execute_cleanup(
        store.db_path, plan,
        expected_fingerprint=plan.fingerprint,
        source=cleanup_mod.SOURCE_MANUAL,
    )


def _thread_ids(store):
    return {t.id for t in store.list_threads()}


class TestEligibilityRules:
    def test_old_read_thread_is_deleted(self, store, agents):
        a, b = agents
        old = _make_thread(
            store, a, b, title="done", body=SECRET,
            delivery_state="read",
        )

        result = _run_cleanup(store)

        assert result.status == "success"
        assert result.threads_deleted == 1
        assert old.id not in _thread_ids(store)

    @pytest.mark.parametrize("state", ("pending", "notified", "claimed"))
    def test_active_delivery_states_protect_whole_thread(
        self, store, agents, state,
    ):
        a, b = agents
        protected = _make_thread(
            store, a, b,
            title=f"kept-{state}", delivery_state=state,
        )

        plan = cleanup_mod.build_cleanup_plan(store.db_path, 30)

        assert plan.thread_ids == ()
        assert protected.id in _thread_ids(store)

    def test_failed_notification_thread_can_be_cleaned(self, store, agents):
        a, b = agents
        failed = _make_thread(
            store, a, b, title="failed", delivery_state="failed",
        )

        result = _run_cleanup(store)

        assert result.status == "success"
        assert failed.id not in _thread_ids(store)

    def test_recent_thread_is_kept(self, store, agents):
        a, b = agents
        fresh = _make_thread(
            store, a, b, title="fresh",
            delivery_state="read", age_days=2,
        )

        plan = cleanup_mod.build_cleanup_plan(store.db_path, 30)

        assert plan.thread_ids == ()
        assert fresh.id in _thread_ids(store)

    def test_recent_read_activity_revives_old_thread(self, store, agents):
        a, b = agents
        # Message is 40 days old but it was read today: the delivery
        # change counts as thread activity.
        revived = _make_thread(
            store, a, b, title="read-late",
            delivery_state="read", age_days=40, read_age_days=0,
        )

        plan = cleanup_mod.build_cleanup_plan(store.db_path, 30)

        assert plan.thread_ids == ()
        assert revived.id in _thread_ids(store)

    def test_mixed_batch_deletes_only_eligible(self, store, agents):
        a, b = agents
        deletable = _make_thread(
            store, a, b, title="old", delivery_state="read",
        )
        _make_thread(
            store, a, b, title="waiting",
            delivery_state="notified",
        )
        _make_thread(
            store, a, b, title="young",
            delivery_state="read", age_days=1,
        )

        result = _run_cleanup(store)

        assert result.threads_deleted == 1
        remaining = _thread_ids(store)
        assert deletable.id not in remaining
        assert len(remaining) == 2

    def test_related_rows_are_removed(self, store, agents):
        a, b = agents
        thread = _make_thread(
            store, a, b, title="old", delivery_state="read",
        )

        _run_cleanup(store)

        with sqlite3.connect(store.db_path) as conn:
            assert conn.execute(
                "SELECT count(*) FROM threads"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT count(*) FROM messages"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT count(*) FROM deliveries"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT count(*) FROM thread_participants"
            ).fetchone()[0] == 0


class TestAtomicityAndWiping:
    def test_failure_mid_delete_rolls_back_the_whole_thread(
        self, monkeypatch, store, agents,
    ):
        a, b = agents
        _make_thread(
            store, a, b, title="old", delivery_state="read",
        )
        real_connect = cleanup_mod.sqlite3.connect

        class FailingConn:
            def __init__(self, inner):
                object.__setattr__(self, "inner", inner)

            def execute(self, sql, *args, **kwargs):
                if "DELETE FROM messages" in sql:
                    raise RuntimeError("simulated mid-cleanup failure")
                return self.inner.execute(sql, *args, **kwargs)

            def __setattr__(self, name, value):
                setattr(self.inner, name, value)

            def __enter__(self):
                return self.inner.__enter__()

            def __exit__(self, *exc):
                return self.inner.__exit__(*exc)

            def __getattr__(self, name):
                return getattr(self.inner, name)

        def failing_connect(path, *args, **kwargs):
            return FailingConn(real_connect(path, *args, **kwargs))

        monkeypatch.setattr(
            cleanup_mod.sqlite3, "connect", failing_connect,
        )

        plan = cleanup_mod.build_cleanup_plan(store.db_path, 30)
        result = cleanup_mod.execute_cleanup(
            store.db_path, plan,
            expected_fingerprint=plan.fingerprint,
            source=cleanup_mod.SOURCE_MANUAL,
        )

        assert result.status == "failed"
        with sqlite3.connect(store.db_path) as conn:
            assert conn.execute(
                "SELECT count(*) FROM threads"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT count(*) FROM messages"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT count(*) FROM deliveries"
            ).fetchone()[0] == 1
            audit_status = conn.execute(
                "SELECT status FROM cleanup_audit"
            ).fetchone()[0]
        assert audit_status == "failed"

    def test_deleted_body_cannot_be_found_in_db_directory(
        self, store, agents, tmp_path,
    ):
        a, b = agents
        _make_thread(
            store, a, b, title="old", body=SECRET,
            delivery_state="read",
        )

        result = _run_cleanup(store)
        assert result.status == "success"

        db_dir = os.path.dirname(store.db_path)
        leaks = []
        for name in os.listdir(db_dir):
            path = os.path.join(db_dir, name)
            if os.path.isfile(path) and SECRET.encode() in open(
                path, "rb"
            ).read():
                leaks.append(name)
        assert leaks == []


class TestFingerprintGuards:
    def test_stale_plan_aborts_and_deletes_nothing(self, store, agents):
        a, b = agents
        old = _make_thread(
            store, a, b, title="old", delivery_state="read",
        )
        plan = cleanup_mod.build_cleanup_plan(store.db_path, 30)

        # The candidate list changes before execution: a fresh read
        # revives the thread.
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(store.db_path) as conn:
            conn.execute(
                """UPDATE deliveries SET read_at=?
                WHERE message_id IN (
                    SELECT id FROM messages WHERE thread_id=?
                )""",
                (now, old.id),
            )

        result = cleanup_mod.execute_cleanup(
            store.db_path, plan,
            expected_fingerprint=plan.fingerprint,
            source=cleanup_mod.SOURCE_MANUAL,
        )

        assert result.status == "aborted"
        assert result.threads_deleted == 0
        assert old.id in _thread_ids(store)
        assert cleanup_mod.latest_audit(store.db_path)["status"] == "aborted"


class TestAuditTrail:
    def test_audit_records_only_counts_time_status(self, store, agents):
        a, b = agents
        _make_thread(
            store, a, b, title=SECRET, body=SECRET,
            delivery_state="read",
        )

        _run_cleanup(store)

        with sqlite3.connect(store.db_path) as conn:
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(cleanup_audit)")
            }
            assert columns == {
                "id", "ran_at", "source", "status",
                "retention_days", "threads_deleted",
                "messages_deleted", "deliveries_deleted",
            }
            row = conn.execute(
                """SELECT ran_at, source, status, retention_days,
                          threads_deleted, messages_deleted,
                          deliveries_deleted
                   FROM cleanup_audit"""
            ).fetchone()
            dump = "\n".join(str(value) for value in row)
        assert SECRET not in dump
        assert row[1] == "manual"
        assert row[2] == "success"
        assert row[4:] == (1, 1, 1)


class TestDailyGate:
    def test_watcher_cleans_once_per_day_and_skips_on_restart(
        self, store, agents,
    ):
        a, b = agents
        _make_thread(
            store, a, b, title="old", delivery_state="read",
        )

        first = cleanup_mod.run_due_cleanup(store.db_path, 30)
        assert first is not None and first.status == "success"
        # Simulate a watcher restart against the same database.
        second = cleanup_mod.run_due_cleanup(store.db_path, 30)
        assert second is None

        with sqlite3.connect(store.db_path) as conn:
            days = conn.execute(
                "SELECT count(*) FROM cleanup_audit "
                "WHERE status='success' AND source='watcher'"
            ).fetchone()[0]
        assert days == 1

    def test_aborted_run_does_not_close_the_daily_gate(self, store, agents):
        a, b = agents
        old = _make_thread(
            store, a, b, title="old", delivery_state="read",
        )
        plan = cleanup_mod.build_cleanup_plan(store.db_path, 30)
        aborted = cleanup_mod.execute_cleanup(
            store.db_path, plan,
            expected_fingerprint="wrong-fingerprint",
            source=cleanup_mod.SOURCE_WATCHER,
        )
        assert aborted.status == "aborted"

        retried = cleanup_mod.run_due_cleanup(store.db_path, 30)
        assert retried is not None and retried.status == "success"
        assert old.id not in _thread_ids(store)

    def test_cleanup_loop_runs_inside_watcher(self, monkeypatch, tmp_path):
        path = str(tmp_path / "msg.db")
        store = MsgStore(path)
        a = store.register_agent("a", "%1", "t", AgentKind.CLAUDE, "/tmp/s")
        b = store.register_agent("b", "%2", "t", AgentKind.CODEX, "/tmp/s")
        _make_thread(
            store, a, b, title="old", delivery_state="read",
        )
        watcher = Watcher(path, retention_days=30)
        monkeypatch.setattr(
            cleanup_mod, "CLEANUP_STARTUP_DELAY", 0, raising=False,
        )
        monkeypatch.setattr(
            "claude_code_tools.msg.watcher.CLEANUP_STARTUP_DELAY", 0,
        )
        monkeypatch.setattr(
            "claude_code_tools.msg.watcher.CLEANUP_CHECK_INTERVAL", 3600,
        )

        async def exercise():
            task = asyncio.create_task(watcher._cleanup_loop())
            await asyncio.sleep(0.2)
            watcher._running = False
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(exercise())

        assert cleanup_mod.latest_audit(path)["status"] == "success"

    def test_invalid_retention_env_disables_watcher_cleanup(
        self, monkeypatch, tmp_path,
    ):
        watcher = Watcher(str(tmp_path / "msg.db"))
        monkeypatch.setenv("MSG_RETENTION_DAYS", "soon")
        monkeypatch.setattr(
            "claude_code_tools.msg.watcher.CLEANUP_STARTUP_DELAY", 0,
        )

        async def exercise():
            await asyncio.wait_for(watcher._cleanup_loop(), timeout=1)

        # Returns instead of running or sleeping forever.
        asyncio.run(exercise())
        assert cleanup_mod.latest_audit(watcher.store.db_path) is None


class TestRetentionConfig:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        (
            (None, 30),
            ("", 30),
            ("7", 7),
            (" 14 ", 14),
            (90, 90),
        ),
    )
    def test_parse(self, raw, expected):
        assert cleanup_mod.parse_retention_days(raw) == expected

    @pytest.mark.parametrize("raw", ("soon", "-1", "0", "3.5"))
    def test_invalid(self, raw):
        with pytest.raises(ValueError):
            cleanup_mod.parse_retention_days(raw)

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("MSG_RETENTION_DAYS", "9")
        assert cleanup_mod.retention_days_from_env() == 9


class TestCli:
    @pytest.fixture(autouse=True)
    def no_watcher(self, monkeypatch):
        monkeypatch.setattr(
            "claude_code_tools.msg.cli._ensure_watcher_running",
            lambda _store: None,
        )

    def test_preview_then_execute(self, store, agents):
        a, b = agents
        old = _make_thread(
            store, a, b, title="old-thread",
            body=SECRET, delivery_state="read",
        )
        runner = CliRunner()

        preview = runner.invoke(
            cli, ["--db", store.db_path, "cleanup", "--days", "30"],
        )
        assert preview.exit_code == 0, preview.output
        assert "old-thread" in preview.output
        assert "1 thread(s)" in preview.output
        assert "--execute" in preview.output
        # Preview does not delete.
        assert old.id in _thread_ids(store)

        execute = runner.invoke(
            cli,
            ["--db", store.db_path, "cleanup", "--days", "30",
             "--execute"],
        )
        assert execute.exit_code == 0, execute.output
        assert "Deleted 1 thread(s)" in execute.output
        assert old.id not in _thread_ids(store)

    def test_execute_without_preview_is_refused(self, store, agents):
        a, b = agents
        _make_thread(
            store, a, b, title="old", delivery_state="read",
        )
        result = CliRunner().invoke(
            cli,
            ["--db", store.db_path, "cleanup", "--days", "30",
             "--execute"],
        )
        assert result.exit_code != 0
        assert "review the candidate list first" in result.output
        assert len(_thread_ids(store)) == 1

    def test_changed_list_aborts_execution(self, store, agents):
        a, b = agents
        old = _make_thread(
            store, a, b, title="old", delivery_state="read",
        )
        runner = CliRunner()
        preview = runner.invoke(
            cli, ["--db", store.db_path, "cleanup", "--days", "30"],
        )
        assert preview.exit_code == 0

        # A second old thread appears after the preview.
        newcomer = _make_thread(
            store, a, b, title="arrived-later",
            delivery_state="failed",
        )

        execute = runner.invoke(
            cli,
            ["--db", store.db_path, "cleanup", "--days", "30",
             "--execute"],
        )
        assert execute.exit_code == 1
        assert "candidate list changed" in execute.output
        assert old.id in _thread_ids(store)
        assert newcomer.id in _thread_ids(store)

    def test_days_mismatch_since_preview_is_refused(self, store, agents):
        a, b = agents
        _make_thread(
            store, a, b, title="old", delivery_state="read",
        )
        runner = CliRunner()
        runner.invoke(
            cli, ["--db", store.db_path, "cleanup", "--days", "30"],
        )
        result = runner.invoke(
            cli,
            ["--db", store.db_path, "cleanup", "--days", "7",
             "--execute"],
        )
        assert result.exit_code != 0
        assert "refresh the list" in result.output

    def test_preview_empty_says_so(self, store):
        result = CliRunner().invoke(
            cli, ["--db", store.db_path, "cleanup", "--days", "30"],
        )
        assert result.exit_code == 0
        assert "No threads older than 30 day(s)" in result.output


class TestMigrationV4:
    def test_fresh_schema_is_version_four(self, tmp_path):
        path = str(tmp_path / "msg.db")
        MsgStore(path)
        with sqlite3.connect(path) as conn:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            state_rows = conn.execute(
                "SELECT count(*) FROM cleanup_state"
            ).fetchone()[0]
        assert version == CURRENT_SCHEMA_VERSION == 4
        assert state_rows == 1

    def test_version_three_database_is_upgraded(self, tmp_path):
        path = str(tmp_path / "msg.db")
        MsgStore(path)
        with sqlite3.connect(path) as conn:
            conn.execute("DELETE FROM cleanup_state")
            conn.execute("PRAGMA user_version = 3")
        MsgStore(path)
        with sqlite3.connect(path) as conn:
            assert conn.execute(
                "PRAGMA user_version"
            ).fetchone()[0] == 4
            assert conn.execute(
                "SELECT singleton, last_success_day FROM cleanup_state"
            ).fetchone() == (1, None)

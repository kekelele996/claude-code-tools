"""Thread retention cleanup shared by the watcher and ``msg cleanup``.

Rules (identical for background and manual entry points):

* A thread's age is its latest activity: the newest message timestamp or
  the newest delivery change (created / notified / read).
* A thread is eligible when its latest activity is older than the
  configured retention period.
* A whole thread is retained while any of its deliveries is waiting to be
  sent (``pending``), already notified but not yet read (``notified``), or
  currently claimed (``claimed``). Failed notifications do not protect a
  thread.
* Deletion happens in one transaction: a failure halfway through rolls
  back instead of removing only part of a thread.
* After commit the database is checkpoints and rebuilt so deleted bodies
  cannot be recovered from files in the database directory.
* The audit trail stores counts, time, and status only -- never bodies or
  titles.

Retrieval is guarded by a plan fingerprint. If the candidate list differs
at execution time, the run aborts and deletes nothing.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .models import _new_uuid, _now_iso

ENV_RETENTION_DAYS = "MSG_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 30

SOURCE_WATCHER = "watcher"
SOURCE_MANUAL = "manual"

STATUS_SUCCESS = "success"
STATUS_ABORTED = "aborted"
STATUS_FAILED = "failed"

# Deliveries still needing user-visible action protect the whole thread.
PROTECTED_DELIVERY_STATES = ("pending", "claimed", "notified")

_CANDIDATE_SQL = """
WITH message_activity AS (
    SELECT thread_id,
           count(*) AS message_count,
           MAX(created_at) AS last_message_at
    FROM messages
    GROUP BY thread_id
),
delivery_activity AS (
    SELECT m.thread_id,
           MAX(COALESCE(d.read_at, d.notified_at, d.created_at))
               AS last_delivery_at
    FROM deliveries d
    JOIN messages m ON m.id = d.message_id
    GROUP BY m.thread_id
),
protected_threads AS (
    SELECT DISTINCT m.thread_id
    FROM deliveries d
    JOIN messages m ON m.id = d.message_id
    WHERE d.state IN ({states})
)
SELECT t.id AS thread_id,
       t.title AS title,
       t.created_at AS created_at,
       COALESCE(ma.message_count, 0) AS message_count,
       MAX(t.created_at, ma.last_message_at, da.last_delivery_at)
           AS activity_at
FROM threads t
LEFT JOIN message_activity ma ON ma.thread_id = t.id
LEFT JOIN delivery_activity da ON da.thread_id = t.id
WHERE MAX(t.created_at, ma.last_message_at, da.last_delivery_at) < ?
    AND NOT EXISTS (
        SELECT 1 FROM protected_threads p WHERE p.thread_id = t.id
    )
ORDER BY activity_at ASC, t.id ASC
""".format(states=",".join("?" for _ in PROTECTED_DELIVERY_STATES))


@dataclass(frozen=True)
class ThreadCandidate:
    """One thread on a cleanup plan (titles never enter the audit trail)."""

    thread_id: str
    title: str
    created_at: str
    activity_at: str
    message_count: int


@dataclass(frozen=True)
class CleanupPlan:
    """Candidate snapshot for one retention cutoff."""

    retention_days: int
    cutoff_at: str
    candidates: tuple[ThreadCandidate, ...] = ()
    fingerprint: str = ""

    @property
    def thread_ids(self) -> tuple[str, ...]:
        return tuple(c.thread_id for c in self.candidates)


@dataclass(frozen=True)
class CleanupResult:
    """Outcome of one cleanup attempt. Counts only, never content."""

    status: str
    source: str
    retention_days: int
    threads_deleted: int = 0
    messages_deleted: int = 0
    deliveries_deleted: int = 0
    ran_at: str = field(default_factory=_now_iso)
    error: str | None = None


def parse_retention_days(
    raw: str | int | None,
    default: int = DEFAULT_RETENTION_DAYS,
) -> int:
    """Resolve the retention period from the MSG_RETENTION_DAYS-style value."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    try:
        days = int(str(raw).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid retention days value: {raw!r}") from exc
    if days < 1:
        raise ValueError("retention days must be a positive integer")
    return days


def retention_days_from_env() -> int:
    """Read retention days from the environment used by the existing config."""
    return parse_retention_days(os.environ.get(ENV_RETENTION_DAYS))


def _utc_now(value: datetime | str | None = None) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _cutoff(retention_days: int, now: datetime | None = None) -> datetime:
    return _utc_now(now) - timedelta(days=retention_days)


def _connect(db_path: str, *, autocommit: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path,
        isolation_level=None if autocommit else "",
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _fingerprint(candidates: tuple[ThreadCandidate, ...]) -> str:
    digest = hashlib.sha256()
    for candidate in candidates:
        digest.update(
            f"{candidate.thread_id}|{candidate.activity_at}"
            f"|{candidate.message_count}\n".encode("utf-8")
        )
    return digest.hexdigest()


def _gather(
    conn: sqlite3.Connection,
    retention_days: int,
    cutoff_at: datetime,
) -> CleanupPlan:
    rows = conn.execute(
        _CANDIDATE_SQL,
        (*PROTECTED_DELIVERY_STATES, cutoff_at.isoformat()),
    ).fetchall()
    candidates = tuple(
        ThreadCandidate(
            thread_id=row["thread_id"],
            title=row["title"],
            created_at=row["created_at"],
            activity_at=row["activity_at"],
            message_count=row["message_count"],
        )
        for row in rows
    )
    return CleanupPlan(
        retention_days=retention_days,
        cutoff_at=cutoff_at.isoformat(),
        candidates=candidates,
        fingerprint=_fingerprint(candidates),
    )


def release_expired_claims(db_path: str) -> None:
    """Return stale claims to pending before they protect old threads."""
    from .store import MsgStore

    MsgStore(db_path).release_expired_claims()


def build_cleanup_plan(
    db_path: str,
    retention_days: int,
    now: datetime | None = None,
) -> CleanupPlan:
    """Snapshot the threads eligible for deletion right now."""
    cutoff = _cutoff(retention_days, now)
    conn = _connect(db_path)
    try:
        return _gather(conn, retention_days, cutoff)
    finally:
        conn.close()


def save_preview(db_path: str, plan: CleanupPlan) -> None:
    """Persist the fingerprint of the list shown for manual review."""
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO cleanup_state
                (singleton, preview_fingerprint, preview_at,
                 preview_retention_days)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(singleton) DO UPDATE SET
                preview_fingerprint = excluded.preview_fingerprint,
                preview_at = excluded.preview_at,
                preview_retention_days = excluded.preview_retention_days""",
            (plan.fingerprint, _now_iso(), plan.retention_days),
        )
        conn.commit()
    finally:
        conn.close()


def get_cleanup_state(db_path: str) -> dict | None:
    """Return the singleton cleanup state row, if initialized."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            """SELECT last_success_at, last_success_day,
                      preview_fingerprint, preview_at,
                      preview_retention_days
               FROM cleanup_state WHERE singleton = 1"""
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def latest_audit(db_path: str) -> dict | None:
    """Return the most recent audit entry (metadata columns only)."""
    conn = _connect(db_path)
    try:
        row = conn.execute(
            """SELECT ran_at, source, status, retention_days,
                      threads_deleted, messages_deleted,
                      deliveries_deleted
               FROM cleanup_audit ORDER BY ran_at DESC LIMIT 1"""
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _write_audit(
    db_path: str,
    *,
    source: str,
    status: str,
    retention_days: int,
    threads: int,
    messages: int,
    deliveries: int,
    ran_at: str,
) -> None:
    conn = _connect(db_path)
    try:
        conn.execute(
            """INSERT INTO cleanup_audit
                (id, ran_at, source, status, retention_days,
                 threads_deleted, messages_deleted,
                 deliveries_deleted)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                _new_uuid(), ran_at, source, status, retention_days,
                threads, messages, deliveries,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _record_success_state(db_path: str, ran_at: str) -> None:
    day = ran_at[:10]
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """INSERT INTO cleanup_state
                (singleton, last_success_at, last_success_day,
                 preview_fingerprint, preview_at,
                 preview_retention_days)
            VALUES (1, ?, ?, NULL, NULL, NULL)
            ON CONFLICT(singleton) DO UPDATE SET
                last_success_at = excluded.last_success_at,
                last_success_day = excluded.last_success_day,
                preview_fingerprint = NULL,
                preview_at = NULL,
                preview_retention_days = NULL""",
            (ran_at, day),
        )
        conn.commit()
    finally:
        conn.close()


def _hard_wipe(db_path: str) -> None:
    """Flush WAL and rebuild the DB so deleted rows leave no bytes behind."""
    conn = _connect(db_path, autocommit=True)
    try:
        # Overwrite already happened with secure_delete; make sure old WAL
        # frames holding pre-delete pages are merged and truncated.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.commit()
        # Rebuild the file without free pages carrying deleted content.
        conn.execute("VACUUM")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()


def execute_cleanup(
    db_path: str,
    plan: CleanupPlan,
    *,
    expected_fingerprint: str,
    source: str,
    now: datetime | None = None,
) -> CleanupResult:
    """Delete exactly the planned threads, aborting if the list moved.

    Recomputes the candidate set inside a write lock and only proceeds when
    it still matches ``expected_fingerprint``. All deletions for the batch
    commit together; on failure nothing is removed.
    """
    ran_at = (_utc_now(now)).isoformat()
    conn = _connect(db_path)
    thread_ids: tuple[str, ...] = ()
    try:
        # Must be set outside of a transaction.
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("BEGIN IMMEDIATE")
        cutoff = datetime.fromisoformat(plan.cutoff_at)
        current = _gather(conn, plan.retention_days, cutoff)
        if current.fingerprint != expected_fingerprint:
            conn.rollback()
            conn.close()
            _write_audit(
                db_path,
                source=source,
                status=STATUS_ABORTED,
                retention_days=plan.retention_days,
                threads=0, messages=0, deliveries=0,
                ran_at=ran_at,
            )
            return CleanupResult(
                status=STATUS_ABORTED,
                source=source,
                retention_days=plan.retention_days,
                ran_at=ran_at,
            )

        thread_ids = current.thread_ids
        threads_deleted = messages_deleted = deliveries_deleted = 0
        if thread_ids:
            conn.execute(
                "CREATE TEMP TABLE _msg_cleanup_targets "
                "(thread_id TEXT PRIMARY KEY)"
            )
            conn.executemany(
                "INSERT INTO _msg_cleanup_targets VALUES (?)",
                ((thread_id,) for thread_id in thread_ids),
            )
            messages_deleted = conn.execute(
                """SELECT count(*) FROM messages
                WHERE thread_id IN (
                    SELECT thread_id FROM _msg_cleanup_targets
                )"""
            ).fetchone()[0]
            deliveries_deleted = conn.execute(
                """SELECT count(*) FROM deliveries
                WHERE message_id IN (
                    SELECT id FROM messages
                    WHERE thread_id IN (
                        SELECT thread_id FROM _msg_cleanup_targets
                    )
                )"""
            ).fetchone()[0]
            conn.execute(
                """DELETE FROM deliveries WHERE message_id IN (
                    SELECT id FROM messages
                    WHERE thread_id IN (
                        SELECT thread_id FROM _msg_cleanup_targets
                    )
                )"""
            )
            conn.execute(
                """DELETE FROM messages WHERE thread_id IN (
                    SELECT thread_id FROM _msg_cleanup_targets
                )"""
            )
            conn.execute(
                """DELETE FROM thread_participants WHERE thread_id IN (
                    SELECT thread_id FROM _msg_cleanup_targets
                )"""
            )
            threads_deleted = conn.execute(
                """DELETE FROM threads WHERE id IN (
                    SELECT thread_id FROM _msg_cleanup_targets
                )"""
            ).rowcount
            conn.execute("DROP TABLE _msg_cleanup_targets")
        conn.commit()
    except BaseException as exc:  # noqa: BLE001 - audit any failure
        conn.rollback()
        conn.close()
        _write_audit(
            db_path,
            source=source,
            status=STATUS_FAILED,
            retention_days=plan.retention_days,
            threads=0, messages=0, deliveries=0,
            ran_at=ran_at,
        )
        return CleanupResult(
            status=STATUS_FAILED,
            source=source,
            retention_days=plan.retention_days,
            ran_at=ran_at,
            error=str(exc),
        )
    conn.close()

    # Committed: make the deleted bodies unrecoverable, then publish state.
    try:
        if thread_ids:
            _hard_wipe(db_path)
        _record_success_state(db_path, ran_at)
    except BaseException as exc:  # noqa: BLE001 - keep the audit honest
        _write_audit(
            db_path,
            source=source,
            status=STATUS_FAILED,
            retention_days=plan.retention_days,
            threads=threads_deleted,
            messages=messages_deleted,
            deliveries=deliveries_deleted,
            ran_at=ran_at,
        )
        return CleanupResult(
            status=STATUS_FAILED,
            source=source,
            retention_days=plan.retention_days,
            threads_deleted=threads_deleted,
            messages_deleted=messages_deleted,
            deliveries_deleted=deliveries_deleted,
            ran_at=ran_at,
            error=str(exc),
        )

    _write_audit(
        db_path,
        source=source,
        status=STATUS_SUCCESS,
        retention_days=plan.retention_days,
        threads=threads_deleted,
        messages=messages_deleted,
        deliveries=deliveries_deleted,
        ran_at=ran_at,
    )
    return CleanupResult(
        status=STATUS_SUCCESS,
        source=source,
        retention_days=plan.retention_days,
        threads_deleted=threads_deleted,
        messages_deleted=messages_deleted,
        deliveries_deleted=deliveries_deleted,
        ran_at=ran_at,
    )


def cleanup_is_due(
    db_path: str,
    now: datetime | None = None,
) -> bool:
    """Whether a successful cleanup has already landed this UTC day."""
    state = get_cleanup_state(db_path)
    today = _utc_now(now).date().isoformat()
    return state is None or state.get("last_success_day") != today


def run_due_cleanup(
    db_path: str,
    retention_days: int,
    now: datetime | None = None,
) -> CleanupResult | None:
    """Run the shared cleanup at most once per UTC day (watcher entry)."""
    if not cleanup_is_due(db_path, now):
        return None
    release_expired_claims(db_path)
    moment = _utc_now(now)
    plan = build_cleanup_plan(db_path, retention_days, moment)
    return execute_cleanup(
        db_path,
        plan,
        expected_fingerprint=plan.fingerprint,
        source=SOURCE_WATCHER,
        now=moment,
    )

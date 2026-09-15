"""Small SQLite job and audit store for Shelfmark services."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


JOB_STATUSES = {"queued", "running", "succeeded", "failed", "cancelled"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class Job:
    id: str
    kind: str
    payload: dict[str, Any]
    status: str
    attempts: int
    created_at: str
    started_at: str | None
    finished_at: str | None
    heartbeat_at: str | None
    worker_id: str | None
    error: str | None
    error_code: str | None
    result: dict[str, Any] | None
    cancel_requested: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        return cls(
            id=row["id"],
            kind=row["kind"],
            payload=json.loads(row["payload_json"]),
            status=row["status"],
            attempts=row["attempts"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            heartbeat_at=row["heartbeat_at"],
            worker_id=row["worker_id"],
            error=row["error"],
            # NULL for any row written before migration 2 added this column,
            # and reading it back does not crash: sqlite backfills existing
            # rows with NULL on `ALTER TABLE ... ADD COLUMN`, so this is the
            # honest value for "no code was ever recorded" rather than a
            # fabricated "internal", which would claim to know the failure was
            # unmapped when it might just predate the column entirely.
            error_code=row["error_code"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            cancel_requested=bool(row["cancel_requested"]),
        )


class Database:
    """A single-process-friendly SQLite store with safe multi-worker claims."""

    def __init__(self, path: Path):
        self.path = Path(path).expanduser()

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def initialize(self) -> None:
        with closing(self.connect()) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    heartbeat_at TEXT,
                    worker_id TEXT,
                    error TEXT,
                    result_json TEXT,
                    cancel_requested INTEGER NOT NULL DEFAULT 0
                );

                CREATE INDEX IF NOT EXISTS jobs_status_created_idx
                    ON jobs(status, created_at);

                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    target_type TEXT NOT NULL,
                    target_id TEXT,
                    details_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS audit_events_created_idx
                    ON audit_events(created_at);
                """
            )
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                (1, utc_now()),
            )
            # Migration 2: a stable error_code alongside the existing free-text
            # `error` column, so a job's failure reason can be branched on
            # without parsing a sentence that changes whenever someone rewords
            # it. Unlike the CREATE TABLE/INDEX statements above, `ALTER TABLE
            # ADD COLUMN` is NOT idempotent in SQLite — a second run raises
            # "duplicate column name" — so this has to be gated on the
            # migrations table instead of re-run unconditionally every start.
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version = 2").fetchone() is None:
                conn.execute("ALTER TABLE jobs ADD COLUMN error_code TEXT")
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (2, utc_now()),
                )
            # Migration 3: the reconciler's idempotency ledger. `CREATE TABLE
            # IF NOT EXISTS` is itself safe to re-run (unlike migration 2's
            # ALTER TABLE), but it is still gated on schema_migrations so the
            # table shows up in that history like every other schema change,
            # rather than being the one silent exception to it.
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version = 3").fetchone() is None:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS reconciled_torrents (
                        hash TEXT PRIMARY KEY,
                        name TEXT NOT NULL,
                        transfer_job_id TEXT NOT NULL,
                        created_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (3, utc_now()),
                )
            # Migration 4: one row per worker recording "I am alive", refreshed
            # every main-loop iteration (see worker.py's `_maybe_record_liveness`)
            # -- including when the queue is empty, which `jobs.heartbeat_at`
            # never covers because that column only exists on a RUNNING job
            # row. Without this, an idle worker and a dead one write exactly
            # nothing, either way, so a monitoring probe cannot tell "nothing
            # to do" from "nobody is home". `worker_id` is the primary key
            # rather than a single fixed row so a second worker -- a manual
            # scale-out, or two containers briefly overlapping during a
            # deploy -- gets its own row instead of the two clobbering each
            # other's timestamp.
            if conn.execute("SELECT 1 FROM schema_migrations WHERE version = 4").fetchone() is None:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS worker_liveness (
                        worker_id TEXT PRIMARY KEY,
                        pid INTEGER,
                        last_seen_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (4, utc_now()),
                )

    @staticmethod
    def _audit(
        conn: sqlite3.Connection,
        *,
        actor: str,
        action: str,
        target_type: str,
        target_id: str | None,
        details: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO audit_events
                (created_at, actor, action, target_type, target_id, details_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                utc_now(),
                actor,
                action,
                target_type,
                target_id,
                json.dumps(details or {}, sort_keys=True),
            ),
        )

    def enqueue(self, kind: str, payload: dict[str, Any], actor: str = "system") -> Job:
        if not kind.strip():
            raise ValueError("job kind is required")
        job_id = str(uuid.uuid4())
        created = utc_now()
        payload_json = json.dumps(payload, sort_keys=True)
        with closing(self.connect()) as conn:
            conn.execute(
                """
                INSERT INTO jobs(id, kind, payload_json, status, created_at)
                VALUES (?, ?, ?, 'queued', ?)
                """,
                (job_id, kind, payload_json, created),
            )
            self._audit(
                conn,
                actor=actor,
                action="job.queued",
                target_type="job",
                target_id=job_id,
                details={"kind": kind},
            )
        return self.get_job(job_id)  # type: ignore[return-value]

    def claim_torrent_import(
        self,
        torrent_hash: str,
        name: str,
        kind: str,
        payload: dict[str, Any],
        actor: str = "reconciler",
    ) -> Job | None:
        """Atomically claim a torrent hash for the download pipeline and enqueue its first job.

        The ledger write (`reconciled_torrents`) and the job write happen in
        ONE transaction, not two separate calls. qBittorrent reports a
        finished torrent as complete indefinitely while it seeds, so the
        reconciler sees the same hash again on every future pass -- if a
        crash landed between "hash recorded" and "job enqueued", the choice
        would be between silently dropping the book forever (ledger written,
        no job ever created) or importing it again every single tick
        (job enqueued, ledger never written). Committing both together means
        a crash at any point before COMMIT leaves neither write in effect, so
        the next reconcile pass claims it fresh instead of in a half state.

        Returns the created Job, or None if this hash was already claimed
        (by an earlier reconcile pass, before or after a restart) -- the
        caller's signal to skip it rather than start the pipeline twice.
        """
        if not torrent_hash:
            raise ValueError("torrent_hash is required")
        job_id = str(uuid.uuid4())
        created = utc_now()
        payload_json = json.dumps(payload, sort_keys=True)
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            inserted = conn.execute(
                """
                INSERT OR IGNORE INTO reconciled_torrents(hash, name, transfer_job_id, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (torrent_hash, name, job_id, created),
            ).rowcount
            if inserted != 1:
                # Already claimed by a previous pass -- roll back so this call
                # leaves no trace, not even the job row.
                conn.rollback()
                return None
            conn.execute(
                """
                INSERT INTO jobs(id, kind, payload_json, status, created_at)
                VALUES (?, ?, ?, 'queued', ?)
                """,
                (job_id, kind, payload_json, created),
            )
            self._audit(
                conn,
                actor=actor,
                action="job.queued",
                target_type="job",
                target_id=job_id,
                details={"kind": kind, "torrent_hash": torrent_hash},
            )
            conn.commit()
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> Job | None:
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.from_row(row) if row else None

    def list_jobs(self, status: str | None = None, limit: int = 50) -> list[Job]:
        if status is not None and status not in JOB_STATUSES:
            raise ValueError(f"unknown job status: {status}")
        limit = max(1, min(int(limit), 200))
        with closing(self.connect()) as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [Job.from_row(row) for row in rows]

    def has_active_job(self, kind: str) -> bool:
        """Whether a job of this kind is already queued or running.

        The periodic reconciler (see worker.py's `_maybe_enqueue_reconcile`)
        checks this before enqueuing another `reconcile_downloads` pass, so a
        slow qBittorrent response or the worker being busy on a big organize
        job never piles up duplicate reconcile jobs that would all list the
        exact same category for no benefit.
        """
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT 1 FROM jobs WHERE kind = ? AND status IN ('queued', 'running') LIMIT 1",
                (kind,),
            ).fetchone()
        return row is not None

    def claim_next(self, worker_id: str) -> Job | None:
        with closing(self.connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM jobs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if row is None:
                conn.rollback()
                return None
            now = utc_now()
            updated = conn.execute(
                """
                UPDATE jobs
                   SET status = 'running', attempts = attempts + 1,
                       started_at = COALESCE(started_at, ?), heartbeat_at = ?,
                       worker_id = ?, error = NULL
                 WHERE id = ? AND status = 'queued'
                """,
                (now, now, worker_id, row["id"]),
            ).rowcount
            if updated != 1:
                conn.rollback()
                return None
            conn.commit()
            claimed = conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
        return Job.from_row(claimed) if claimed else None

    def heartbeat(self, job_id: str, worker_id: str) -> bool:
        with closing(self.connect()) as conn:
            return (
                conn.execute(
                    "UPDATE jobs SET heartbeat_at = ? WHERE id = ? AND status = 'running' AND worker_id = ?",
                    (utc_now(), job_id, worker_id),
                ).rowcount
                == 1
            )

    def complete(self, job_id: str, worker_id: str, result: dict[str, Any]) -> bool:
        now = utc_now()
        with closing(self.connect()) as conn:
            updated = conn.execute(
                """
                UPDATE jobs
                   SET status = 'succeeded', finished_at = ?, heartbeat_at = ?, result_json = ?,
                       error = NULL
                 WHERE id = ? AND status = 'running' AND worker_id = ?
                """,
                (now, now, json.dumps(result, sort_keys=True), job_id, worker_id),
            ).rowcount
            if updated:
                self._audit(
                    conn,
                    actor=worker_id,
                    action="job.succeeded",
                    target_type="job",
                    target_id=job_id,
                )
            return updated == 1

    def fail(self, job_id: str, worker_id: str, error: str, code: str | None = None) -> bool:
        now = utc_now()
        with closing(self.connect()) as conn:
            updated = conn.execute(
                """
                UPDATE jobs
                   SET status = 'failed', finished_at = ?, heartbeat_at = ?, error = ?, error_code = ?
                 WHERE id = ? AND status = 'running' AND worker_id = ?
                """,
                (now, now, error[:4000], code, job_id, worker_id),
            ).rowcount
            if updated:
                self._audit(
                    conn,
                    actor=worker_id,
                    action="job.failed",
                    target_type="job",
                    target_id=job_id,
                    details={"error": error[:4000], "code": code},
                )
            return updated == 1

    def cancel(self, job_id: str, actor: str = "system") -> Job | None:
        with closing(self.connect()) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            if row["status"] == "queued":
                conn.execute(
                    "UPDATE jobs SET status = 'cancelled', finished_at = ? WHERE id = ?",
                    (utc_now(), job_id),
                )
            elif row["status"] == "running":
                conn.execute(
                    "UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,)
                )
            self._audit(
                conn,
                actor=actor,
                action="job.cancel_requested",
                target_type="job",
                target_id=job_id,
            )
        return self.get_job(job_id)

    def cancel_running(self, job_id: str, worker_id: str) -> bool:
        """Finalize a running job after its worker observed cancellation.

        Sets `error_code = 'cancelled'` even though `status` already says
        'cancelled': a caller reading only the code column (the same field a
        failed job's reason lives in) should still be able to tell "the user
        stopped this" apart from "this broke" without also inspecting status.
        """
        with closing(self.connect()) as conn:
            updated = conn.execute(
                """
                UPDATE jobs
                   SET status = 'cancelled', finished_at = ?, heartbeat_at = ?, error_code = 'cancelled'
                 WHERE id = ? AND status = 'running' AND worker_id = ?
                """,
                (utc_now(), utc_now(), job_id, worker_id),
            ).rowcount
            if updated:
                self._audit(
                    conn,
                    actor=worker_id,
                    action="job.cancelled",
                    target_type="job",
                    target_id=job_id,
                )
            return updated == 1

    def cancellation_requested(self, job_id: str) -> bool:
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT cancel_requested FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
        return bool(row["cancel_requested"]) if row else False

    def requeue_stale(self, stale_after_seconds: float = 900.0, actor: str = "reaper") -> int:
        """Return abandoned running jobs to the queue after a worker crash."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=max(1.0, stale_after_seconds))
        ).isoformat(timespec="seconds")
        with closing(self.connect()) as conn:
            rows = conn.execute(
                "SELECT id FROM jobs WHERE status = 'running' AND (heartbeat_at IS NULL OR heartbeat_at < ?)",
                (cutoff,),
            ).fetchall()
            if not rows:
                return 0
            now = utc_now()
            conn.execute(
                """
                UPDATE jobs
                   SET status = 'queued', worker_id = NULL, heartbeat_at = NULL,
                       error = 'requeued after stale worker heartbeat'
                 WHERE status = 'running' AND (heartbeat_at IS NULL OR heartbeat_at < ?)
                """,
                (cutoff,),
            )
            for row in rows:
                self._audit(
                    conn,
                    actor=actor,
                    action="job.requeued",
                    target_type="job",
                    target_id=row["id"],
                    details={"requeued_at": now},
                )
            return len(rows)

    def record_liveness(self, worker_id: str, pid: int | None = None) -> None:
        """Upsert this worker's row in `worker_liveness`.

        Called once per main-loop iteration (see worker.py's
        `_maybe_record_liveness`), not once per job. That distinction is the
        entire point of this table: `heartbeat()` above only ever touches a
        RUNNING job row, so a worker sitting idle with an empty queue writes
        nothing there -- indistinguishable, to any caller, from a worker that
        crashed. This is deliberately a small, separate write rather than
        piggybacking on the jobs table, so it means the same thing whether or
        not a job happens to be in flight.
        """
        now = utc_now()
        with closing(self.connect()) as conn:
            conn.execute(
                """
                INSERT INTO worker_liveness(worker_id, pid, last_seen_at)
                VALUES (?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    pid = excluded.pid,
                    last_seen_at = excluded.last_seen_at
                """,
                (worker_id, pid, now),
            )

    def latest_worker_liveness(self) -> dict[str, Any] | None:
        """The most recently updated `worker_liveness` row, across every worker_id.

        Returns None both when no worker has ever ticked AND when the table
        itself does not exist yet -- an older database from before migration 4,
        or a brand-new one whose worker container has not reached
        `initialize()` for the first time yet. Both situations mean exactly
        the same thing to a caller ("no liveness data exists") and must read
        as unknown rather than stale: `/readyz` and the worker's own Docker
        healthcheck command (see the Dockerfile-adjacent compose healthcheck)
        both call this single method instead of each re-implementing that
        distinction and risking the two disagreeing.
        """
        with closing(self.connect()) as conn:
            try:
                row = conn.execute(
                    "SELECT worker_id, pid, last_seen_at FROM worker_liveness "
                    "ORDER BY last_seen_at DESC LIMIT 1"
                ).fetchone()
            except sqlite3.OperationalError:
                return None
        return dict(row) if row else None

    def youngest_running_job_started_at(self) -> str | None:
        """The most recent `started_at` among currently RUNNING jobs, or None.

        `Worker.run_once` executes exactly one job to completion, synchronously,
        before the main loop returns to write another `worker_liveness` row
        (see `_maybe_record_liveness`) -- so a single long job (a big
        `transfer_completed` pull, its settle wait, then its checksum verify)
        can legitimately leave that row unrefreshed for the job's entire
        duration with nothing wrong. `/readyz` uses this to tell that case
        apart from an actually dead or wedged worker: a RUNNING job that
        started recently is itself evidence someone is home, even though the
        liveness row alone looks stale. `MAX(started_at)` picks the most
        favorable evidence available -- the freshest running job, in the rare
        case more than one exists (e.g. two worker containers briefly
        overlapping) -- since any one sufficiently recent running job is
        enough to explain the silence.
        """
        with closing(self.connect()) as conn:
            row = conn.execute(
                "SELECT MAX(started_at) AS started_at FROM jobs WHERE status = 'running'"
            ).fetchone()
        return row["started_at"] if row and row["started_at"] else None

    def worker_liveness_status(
        self, stale_after_seconds: float, running_job_bound_seconds: float
    ) -> dict[str, Any]:
        """Classify this worker fleet's liveness: `unknown` / `ok` / `busy` / `stale`.

        This is the ONE implementation of the rule, called by both
        api.py's `/readyz` and worker.py's `check_liveness_cli` (the
        `shelfmark-worker` Docker healthcheck). It used to be two: `/readyz`
        had this exact logic, and the Docker healthcheck was a separate
        inline `python -c` one-liner that only checked liveness age with no
        busy-job exception -- which meant `docker ps` reported
        `shelfmark-worker` as unhealthy during any legitimately long
        transfer, even after `/readyz` was fixed to say `busy` for the same
        situation. Two health signals disagreeing is worse than either
        alone, since now the operator has to know which one lies -- and
        `docker ps` is the one people check first. Sharing this method is
        what makes that impossible to reintroduce: there is nowhere left
        for the two to drift apart.

        Four outcomes:

        - `unknown`: no worker has EVER ticked -- a database from before
          migration 4, or a worker container a fraction of a second into
          startup, before its first loop iteration. Must never read as
          `stale`: that would fail every upgrade and the first moment of
          every deploy.
        - `ok`: the freshest `worker_liveness` row is within
          `stale_after_seconds`.
        - `busy`: that row is older, but a job is `running` that started
          within `running_job_bound_seconds`. `Worker.run_once` executes
          one job to completion synchronously -- no threads -- so a big
          transfer's pull, settle-wait, and checksum verify can together
          outlast `stale_after_seconds` with the worker perfectly healthy
          the whole time; nothing refreshes `worker_liveness` until that
          job returns. Reporting this as `stale` pages for a routine
          import, and an alert that fires when nothing is wrong trains
          whoever gets paged to ignore it.
        - `stale`: the row is older AND either no job is running or the
          running job itself started longer ago than
          `running_job_bound_seconds`. That second half is deliberate, not
          a loophole: a job stuck in `running` past its own ceiling is no
          longer credible evidence of anything -- it either genuinely
          overran, or the worker died mid-job and left the row stuck in
          `running` forever, which is exactly the "wedged worker hides
          behind a permanently running job" failure this bound exists to
          still catch.
        """
        row = self.latest_worker_liveness()
        if row is None:
            return {"status": "unknown", "worker_id": None, "last_seen_at": None}
        last_seen = datetime.fromisoformat(row["last_seen_at"])
        age_seconds = (datetime.now(timezone.utc) - last_seen).total_seconds()
        if age_seconds <= stale_after_seconds:
            return {
                "status": "ok",
                "worker_id": row["worker_id"],
                "last_seen_at": row["last_seen_at"],
                "age_seconds": round(age_seconds, 1),
            }
        started_at = self.youngest_running_job_started_at()
        if started_at is not None:
            job_age_seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(started_at)).total_seconds()
            if job_age_seconds <= running_job_bound_seconds:
                return {
                    "status": "busy",
                    "worker_id": row["worker_id"],
                    "last_seen_at": row["last_seen_at"],
                    "age_seconds": round(age_seconds, 1),
                    "running_job_age_seconds": round(job_age_seconds, 1),
                }
        return {
            "status": "stale",
            "worker_id": row["worker_id"],
            "last_seen_at": row["last_seen_at"],
            "age_seconds": round(age_seconds, 1),
        }

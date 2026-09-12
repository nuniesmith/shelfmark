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

    def fail(self, job_id: str, worker_id: str, error: str) -> bool:
        now = utc_now()
        with closing(self.connect()) as conn:
            updated = conn.execute(
                """
                UPDATE jobs
                   SET status = 'failed', finished_at = ?, heartbeat_at = ?, error = ?
                 WHERE id = ? AND status = 'running' AND worker_id = ?
                """,
                (now, now, error[:4000], job_id, worker_id),
            ).rowcount
            if updated:
                self._audit(
                    conn,
                    actor=worker_id,
                    action="job.failed",
                    target_type="job",
                    target_id=job_id,
                    details={"error": error[:4000]},
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
        """Finalize a running job after its worker observed cancellation."""
        with closing(self.connect()) as conn:
            updated = conn.execute(
                """
                UPDATE jobs
                   SET status = 'cancelled', finished_at = ?, heartbeat_at = ?
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

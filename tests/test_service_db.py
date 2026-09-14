from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from src.shelfmark_service.db import Database


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="shelfmark-db-test-")
        self.database = Database(Path(self.tmp.name) / "state" / "shelfmark.db")
        self.database.initialize()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_claim_complete_and_audit_are_durable(self) -> None:
        queued = self.database.enqueue("organize_preview", {"source": "/incoming"}, actor="tester")
        claimed = self.database.claim_next("worker-a")

        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed.id, queued.id)
        self.assertEqual(claimed.status, "running")
        self.assertEqual(claimed.attempts, 1)
        self.assertTrue(self.database.heartbeat(claimed.id, "worker-a"))
        self.assertTrue(self.database.complete(claimed.id, "worker-a", {"books": 2}))

        finished = self.database.get_job(claimed.id)
        self.assertIsNotNone(finished)
        assert finished is not None
        self.assertEqual(finished.status, "succeeded")
        self.assertEqual(finished.result, {"books": 2})
        with self.database.connect() as conn:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
            self.assertGreaterEqual(
                conn.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0], 2
            )

    def test_queued_and_running_cancellation(self) -> None:
        queued = self.database.enqueue("organize_preview", {"source": "/incoming"})
        cancelled = self.database.cancel(queued.id, actor="tester")
        self.assertIsNotNone(cancelled)
        assert cancelled is not None
        self.assertEqual(cancelled.status, "cancelled")

        running_job = self.database.enqueue("organize_preview", {"source": "/incoming"})
        claimed = self.database.claim_next("worker-a")
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed.id, running_job.id)
        requested = self.database.cancel(running_job.id, actor="tester")
        self.assertIsNotNone(requested)
        assert requested is not None
        self.assertEqual(requested.status, "running")
        self.assertTrue(requested.cancel_requested)
        self.assertTrue(self.database.cancel_running(running_job.id, "worker-a"))
        self.assertEqual(self.database.get_job(running_job.id).status, "cancelled")  # type: ignore[union-attr]

    def test_stale_running_job_is_requeued(self) -> None:
        job = self.database.enqueue("organize_preview", {"source": "/incoming"})
        claimed = self.database.claim_next("worker-a")
        self.assertIsNotNone(claimed)
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE jobs SET heartbeat_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (job.id,),
            )
        self.assertEqual(self.database.requeue_stale(60, actor="reaper"), 1)
        requeued = self.database.get_job(job.id)
        self.assertIsNotNone(requeued)
        assert requeued is not None
        self.assertEqual(requeued.status, "queued")
        self.assertIsNone(requeued.worker_id)

    def test_fail_records_error_code_alongside_the_message(self) -> None:
        self.database.enqueue("organize_preview", {"source": "/incoming"})
        claimed = self.database.claim_next("worker-a")
        assert claimed is not None
        self.assertTrue(self.database.fail(claimed.id, "worker-a", "boom", code="internal"))

        failed = self.database.get_job(claimed.id)
        assert failed is not None
        self.assertEqual(failed.status, "failed")
        self.assertEqual(failed.error, "boom")
        self.assertEqual(failed.error_code, "internal")

    def test_fail_without_a_code_leaves_error_code_null(self) -> None:
        """A caller that omits `code` must not crash the column, and NULL is
        the value it should read back as — the same value a pre-migration row
        gets from `ALTER TABLE ... ADD COLUMN`."""
        self.database.enqueue("organize_preview", {"source": "/incoming"})
        claimed = self.database.claim_next("worker-a")
        assert claimed is not None
        self.assertTrue(self.database.fail(claimed.id, "worker-a", "boom"))

        failed = self.database.get_job(claimed.id)
        assert failed is not None
        self.assertIsNone(failed.error_code)

    def test_cancel_running_records_the_cancelled_code(self) -> None:
        self.database.enqueue("organize_preview", {"source": "/incoming"})
        claimed = self.database.claim_next("worker-a")
        assert claimed is not None
        self.assertTrue(self.database.cancel_running(claimed.id, "worker-a"))

        cancelled = self.database.get_job(claimed.id)
        assert cancelled is not None
        self.assertEqual(cancelled.status, "cancelled")
        self.assertEqual(cancelled.error_code, "cancelled")

    def test_pre_migration_row_reads_back_with_a_null_error_code(self) -> None:
        """Simulates every Shelfmark database as it exists before this change
        ships: a `jobs` table with no `error_code` column at all, holding a
        row that already failed under the old free-text-only `error` column.

        `Database.initialize()` is what runs migration 2 (`ALTER TABLE jobs
        ADD COLUMN error_code`). Run against a database in that pre-existing
        shape, it must add the column without disturbing the row already
        there, and reading that row back must not crash or invent a code for
        a failure nothing ever classified.
        """
        path = Path(self.tmp.name) / "state" / "legacy.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                INSERT INTO schema_migrations(version, applied_at)
                    VALUES (1, '2026-01-01T00:00:00+00:00');

                CREATE TABLE jobs (
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
                INSERT INTO jobs(id, kind, payload_json, status, attempts, created_at, error)
                VALUES (
                    'legacy-1', 'organize_apply', '{}', 'failed', 1,
                    '2026-01-01T00:00:00+00:00', 'old style failure text'
                );
                """
            )

        legacy_db = Database(path)
        legacy_db.initialize()  # runs migration 2 against the pre-existing table

        job = legacy_db.get_job("legacy-1")
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.error, "old style failure text")
        self.assertIsNone(job.error_code)


if __name__ == "__main__":
    unittest.main()

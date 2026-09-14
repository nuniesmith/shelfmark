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

    def test_migration_3_is_recorded_and_creates_the_ledger_table(self) -> None:
        with self.database.connect() as conn:
            self.assertIsNotNone(
                conn.execute("SELECT 1 FROM schema_migrations WHERE version = 3").fetchone()
            )
            # Must not raise: the table migration 3 adds has to actually exist.
            conn.execute("SELECT hash, name, transfer_job_id, created_at FROM reconciled_torrents")


class TorrentImportLedgerTests(unittest.TestCase):
    """`claim_torrent_import` is the reconciler's idempotency gate: see the
    docstring on the method itself for why the ledger row and the job row
    have to commit together rather than as two separate calls."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="shelfmark-ledger-test-")
        self.database = Database(Path(self.tmp.name) / "state" / "shelfmark.db")
        self.database.initialize()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_first_claim_enqueues_a_job(self) -> None:
        job = self.database.claim_torrent_import(
            "abc123", "Some Book (2020)", "transfer_completed", {"remote_path": "Some Book (2020)"}
        )
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.kind, "transfer_completed")
        self.assertEqual(job.status, "queued")
        self.assertEqual(job.payload["remote_path"], "Some Book (2020)")

    def test_second_claim_of_the_same_hash_is_refused(self) -> None:
        """The exact scenario the brief calls out: a hash must never be
        imported twice, including across what would be a worker restart --
        modeled here as simply calling claim_torrent_import again with a
        fresh Database handle pointed at the same file."""
        first = self.database.claim_torrent_import(
            "abc123", "Some Book (2020)", "transfer_completed", {"remote_path": "Some Book (2020)"}
        )
        assert first is not None
        reopened = Database(self.database.path)
        second = reopened.claim_torrent_import(
            "abc123", "Some Book (2020)", "transfer_completed", {"remote_path": "Some Book (2020)"}
        )
        self.assertIsNone(second)
        # Exactly one job was ever created for this hash -- not a second,
        # abandoned one from the refused claim.
        jobs = [j for j in self.database.list_jobs(limit=50) if j.kind == "transfer_completed"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].id, first.id)

    def test_different_hashes_both_claim_successfully(self) -> None:
        first = self.database.claim_torrent_import("hash-a", "Book A", "transfer_completed", {})
        second = self.database.claim_torrent_import("hash-b", "Book B", "transfer_completed", {})
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        assert first is not None and second is not None
        self.assertNotEqual(first.id, second.id)


class HasActiveJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="shelfmark-active-job-test-")
        self.database = Database(Path(self.tmp.name) / "state" / "shelfmark.db")
        self.database.initialize()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_false_when_nothing_of_that_kind_exists(self) -> None:
        self.assertFalse(self.database.has_active_job("reconcile_downloads"))

    def test_true_for_a_queued_job(self) -> None:
        self.database.enqueue("reconcile_downloads", {})
        self.assertTrue(self.database.has_active_job("reconcile_downloads"))

    def test_true_for_a_running_job(self) -> None:
        self.database.enqueue("reconcile_downloads", {})
        self.database.claim_next("worker-a")
        self.assertTrue(self.database.has_active_job("reconcile_downloads"))

    def test_false_once_the_job_finished(self) -> None:
        self.database.enqueue("reconcile_downloads", {})
        claimed = self.database.claim_next("worker-a")
        assert claimed is not None
        self.database.complete(claimed.id, "worker-a", {})
        self.assertFalse(self.database.has_active_job("reconcile_downloads"))


if __name__ == "__main__":
    unittest.main()

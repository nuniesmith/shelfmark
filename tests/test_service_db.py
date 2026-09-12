from __future__ import annotations

import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()

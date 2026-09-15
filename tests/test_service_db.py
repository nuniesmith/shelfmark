from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta, timezone
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

    def test_migration_4_is_recorded_and_creates_the_liveness_table(self) -> None:
        with self.database.connect() as conn:
            self.assertIsNotNone(
                conn.execute("SELECT 1 FROM schema_migrations WHERE version = 4").fetchone()
            )
            # Must not raise: the table migration 4 adds has to actually exist.
            conn.execute("SELECT worker_id, pid, last_seen_at FROM worker_liveness")


class WorkerLivenessTests(unittest.TestCase):
    """`record_liveness`/`latest_worker_liveness` back both /readyz (api.py)
    and the worker's own Docker healthcheck -- see worker.py's
    `_maybe_record_liveness` for why this is written once per loop
    iteration rather than once per job."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="shelfmark-liveness-test-")
        self.database = Database(Path(self.tmp.name) / "state" / "shelfmark.db")
        self.database.initialize()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_no_rows_reports_none_not_a_crash(self) -> None:
        """A fresh database has no liveness row until a worker ticks once --
        this must read as "nothing recorded yet", never raise."""
        self.assertIsNone(self.database.latest_worker_liveness())

    def test_record_liveness_is_readable_back(self) -> None:
        self.database.record_liveness("worker-a", pid=1234)
        row = self.database.latest_worker_liveness()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["worker_id"], "worker-a")
        self.assertEqual(row["pid"], 1234)
        self.assertIsNotNone(row["last_seen_at"])

    def test_recording_again_updates_the_same_row_rather_than_inserting_a_second_one(self) -> None:
        self.database.record_liveness("worker-a", pid=1)
        self.database.record_liveness("worker-a", pid=2)
        with self.database.connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM worker_liveness").fetchone()[0]
        self.assertEqual(count, 1)
        row = self.database.latest_worker_liveness()
        assert row is not None
        self.assertEqual(row["pid"], 2)

    def test_two_different_workers_each_keep_their_own_row(self) -> None:
        """The brief requires telling two workers apart -- `worker_id` is the
        primary key specifically so a second worker never clobbers the
        first's timestamp."""
        self.database.record_liveness("worker-a")
        self.database.record_liveness("worker-b")
        with self.database.connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM worker_liveness").fetchone()[0]
        self.assertEqual(count, 2)

    def test_latest_worker_liveness_returns_the_freshest_row(self) -> None:
        self.database.record_liveness("worker-old")
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE worker_liveness SET last_seen_at = '2000-01-01T00:00:00+00:00' "
                "WHERE worker_id = 'worker-old'"
            )
        self.database.record_liveness("worker-new")
        row = self.database.latest_worker_liveness()
        assert row is not None
        self.assertEqual(row["worker_id"], "worker-new")

    def test_missing_table_reports_none_not_a_crash(self) -> None:
        """Simulates every Shelfmark database as it exists before this change
        ships: no `worker_liveness` table at all (pre-migration-4). Reading
        it must report "unknown", the same as a fresh database with the
        table but no rows -- never raise, and never be confused with the
        `stale` outcome that requires an actual old timestamp to exist."""
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
                """
            )
        legacy_db = Database(path)
        self.assertIsNone(legacy_db.latest_worker_liveness())


class YoungestRunningJobStartedAtTests(unittest.TestCase):
    """Backs /readyz's `busy` classification: see api.py's `_worker_liveness`
    for why a recently-started RUNNING job is treated as evidence the
    worker is alive even when its `worker_liveness` row looks stale."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="shelfmark-running-job-test-")
        self.database = Database(Path(self.tmp.name) / "state" / "shelfmark.db")
        self.database.initialize()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_none_when_nothing_is_running(self) -> None:
        self.assertIsNone(self.database.youngest_running_job_started_at())

    def test_returns_the_running_jobs_started_at(self) -> None:
        self.database.enqueue("organize_preview", {"source": "/incoming"})
        claimed = self.database.claim_next("worker-a")
        assert claimed is not None
        self.assertEqual(self.database.youngest_running_job_started_at(), claimed.started_at)

    def test_ignores_queued_and_finished_jobs(self) -> None:
        self.database.enqueue("organize_preview", {"source": "/incoming"})  # stays queued
        self.database.enqueue("organize_preview", {"source": "/incoming"})
        finished = self.database.claim_next("worker-a")
        assert finished is not None
        self.database.complete(finished.id, "worker-a", {})
        self.assertIsNone(self.database.youngest_running_job_started_at())

    def test_returns_the_most_recent_started_at_when_more_than_one_job_is_running(self) -> None:
        """Not reachable through a single worker (`Worker.run_once` claims
        one job at a time), but the query must still pick the freshest
        evidence if two worker containers ever overlap during a deploy."""
        self.database.enqueue("organize_preview", {"source": "/a"})
        self.database.enqueue("organize_preview", {"source": "/b"})
        older = self.database.claim_next("worker-a")
        newer = self.database.claim_next("worker-b")
        assert older is not None and newer is not None
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE jobs SET started_at = '2000-01-01T00:00:00+00:00' WHERE id = ?", (older.id,)
            )
        self.assertEqual(self.database.youngest_running_job_started_at(), newer.started_at)


class WorkerLivenessStatusTests(unittest.TestCase):
    """`worker_liveness_status` is the ONE implementation of the
    unknown/ok/busy/stale rule, shared by api.py's `/readyz` and worker.py's
    `check_liveness_cli` (the Docker healthcheck) -- see the method's
    docstring for why it used to be two separate implementations that
    disagreed. `stale_after_seconds` and `running_job_bound_seconds` are
    kept small and distinct in these tests so a bug that swaps the two
    bounds, or confuses liveness age with job age, cannot pass by accident.
    """

    STALE_AFTER = 60.0
    RUNNING_JOB_BOUND = 500.0

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="shelfmark-liveness-status-test-")
        self.database = Database(Path(self.tmp.name) / "state" / "shelfmark.db")
        self.database.initialize()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _status(self) -> dict[str, object]:
        return self.database.worker_liveness_status(self.STALE_AFTER, self.RUNNING_JOB_BOUND)

    def _age_liveness(self, worker_id: str) -> None:
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE worker_liveness SET last_seen_at = '2000-01-01T00:00:00+00:00' "
                "WHERE worker_id = ?",
                (worker_id,),
            )

    def test_no_row_reports_unknown(self) -> None:
        self.assertEqual(self._status()["status"], "unknown")

    def test_fresh_row_reports_ok(self) -> None:
        self.database.record_liveness("worker-a")
        result = self._status()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["worker_id"], "worker-a")

    def test_stale_row_with_no_running_job_reports_stale(self) -> None:
        self.database.record_liveness("worker-a")
        self._age_liveness("worker-a")
        self.assertEqual(self._status()["status"], "stale")

    def test_stale_row_with_a_recently_started_running_job_reports_busy(self) -> None:
        self.database.record_liveness("worker-a")
        self._age_liveness("worker-a")
        self.database.enqueue("transfer_completed", {"remote_path": "Some Book"})
        claimed = self.database.claim_next("worker-a")
        assert claimed is not None
        result = self._status()
        self.assertEqual(result["status"], "busy")
        self.assertIn("running_job_age_seconds", result)

    def test_stale_row_with_an_old_running_job_still_reports_stale(self) -> None:
        """The bound's whole purpose: a job stuck in `running` past
        `running_job_bound_seconds` is no longer credible evidence of
        anything -- this must not be waved through as `busy` forever."""
        self.database.record_liveness("worker-a")
        self._age_liveness("worker-a")
        self.database.enqueue("transfer_completed", {"remote_path": "Some Book"})
        claimed = self.database.claim_next("worker-a")
        assert claimed is not None
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE jobs SET started_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
                (claimed.id,),
            )
        self.assertEqual(self._status()["status"], "stale")


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


class JobRetentionSweepTests(unittest.TestCase):
    """`sweep_job_retention` -- see its own docstring in db.py, and the four
    module-level RECONCILE_*/PIPELINE_* constants above it, for the
    reasoning behind each tier's window."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="shelfmark-retention-test-")
        self.database = Database(Path(self.tmp.name) / "state" / "shelfmark.db")
        self.database.initialize()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _age_job(self, job_id: str, age_seconds: float) -> None:
        """Back-date a job's created_at/finished_at so it looks `age_seconds` old.

        Both columns are set, not just finished_at: `sweep_job_retention`
        ages off `COALESCE(finished_at, created_at)` specifically to stay
        correct even for a row that somehow has no finished_at, so a test
        that only aged one of the two would not actually exercise the
        column real terminal jobs always have populated.
        """
        aged = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat(timespec="seconds")
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE jobs SET created_at = ?, finished_at = ? WHERE id = ?",
                (aged, aged, job_id),
            )

    def _make_reconcile(self, claimed_jobs: list[str], status: str = "succeeded") -> str:
        self.database.enqueue("reconcile_downloads", {})
        claimed = self.database.claim_next("worker-a")
        assert claimed is not None
        if status == "succeeded":
            self.database.complete(
                claimed.id,
                "worker-a",
                {"category": "shelfmark-books", "seen": 0, "completed": 0, "claimed_jobs": claimed_jobs},
            )
        elif status == "failed":
            self.database.fail(claimed.id, "worker-a", "qBittorrent unreachable", code="upstream_unavailable")
        else:
            raise ValueError(status)
        return claimed.id

    def _make_pipeline(self, kind: str = "organize_apply", status: str = "succeeded") -> str:
        self.database.enqueue(kind, {"source": "/incoming"})
        claimed = self.database.claim_next("worker-a")
        assert claimed is not None
        if status == "succeeded":
            self.database.complete(claimed.id, "worker-a", {"books": 1})
        elif status == "failed":
            self.database.fail(claimed.id, "worker-a", "boom", code="internal")
        else:
            raise ValueError(status)
        return claimed.id

    def test_empty_reconcile_older_than_its_own_window_is_deleted(self) -> None:
        job_id = self._make_reconcile([])
        self._age_job(job_id, 2 * 60 * 60)  # 2h old, past the 1h default window
        deleted = self.database.sweep_job_retention()
        self.assertEqual(deleted["reconcile_empty"], 1)
        self.assertIsNone(self.database.get_job(job_id))

    def test_empty_reconcile_within_its_window_is_kept(self) -> None:
        job_id = self._make_reconcile([])
        self._age_job(job_id, 60)  # 1 minute old
        deleted = self.database.sweep_job_retention()
        self.assertEqual(deleted["reconcile_empty"], 0)
        self.assertIsNotNone(self.database.get_job(job_id))

    def test_reconcile_that_claimed_something_survives_the_empty_tier_but_not_the_30_day_one(self) -> None:
        job_id = self._make_reconcile(["some-other-job-id"])
        # Past the empty tier's 1h window, but well inside the 30-day one --
        # must survive because it claimed something.
        self._age_job(job_id, 2 * 60 * 60)
        deleted = self.database.sweep_job_retention()
        self.assertEqual(deleted["reconcile_empty"], 0)
        self.assertEqual(deleted["reconcile_claimed_or_failed"], 0)
        self.assertIsNotNone(self.database.get_job(job_id))

        self._age_job(job_id, 31 * 24 * 60 * 60)
        deleted = self.database.sweep_job_retention()
        self.assertEqual(deleted["reconcile_claimed_or_failed"], 1)
        self.assertIsNone(self.database.get_job(job_id))

    def test_failed_reconcile_follows_the_30_day_tier_not_the_1_hour_one(self) -> None:
        job_id = self._make_reconcile([], status="failed")
        self._age_job(job_id, 2 * 60 * 60)
        deleted = self.database.sweep_job_retention()
        # The empty-claim tier only ever inspects SUCCEEDED reconcile jobs --
        # a failed one has no result_json to read "claimed_jobs" off of, and
        # must not be swept this early.
        self.assertEqual(deleted["reconcile_empty"], 0)
        self.assertIsNotNone(self.database.get_job(job_id))

        self._age_job(job_id, 31 * 24 * 60 * 60)
        deleted = self.database.sweep_job_retention()
        self.assertEqual(deleted["reconcile_claimed_or_failed"], 1)
        self.assertIsNone(self.database.get_job(job_id))

    def test_succeeded_pipeline_job_survives_89_days_but_not_91(self) -> None:
        job_id = self._make_pipeline("organize_apply", "succeeded")
        self._age_job(job_id, 89 * 24 * 60 * 60)
        deleted = self.database.sweep_job_retention()
        self.assertEqual(deleted["pipeline"], 0)
        self.assertIsNotNone(self.database.get_job(job_id))

        self._age_job(job_id, 91 * 24 * 60 * 60)
        deleted = self.database.sweep_job_retention()
        self.assertEqual(deleted["pipeline"], 1)
        self.assertIsNone(self.database.get_job(job_id))

    def test_failed_pipeline_job_survives_91_days_but_not_181(self) -> None:
        job_id = self._make_pipeline("organize_apply", "failed")
        self._age_job(job_id, 91 * 24 * 60 * 60)
        deleted = self.database.sweep_job_retention()
        self.assertEqual(deleted["pipeline"], 0)
        self.assertEqual(deleted["pipeline_failed"], 0)
        self.assertIsNotNone(self.database.get_job(job_id))

        self._age_job(job_id, 181 * 24 * 60 * 60)
        deleted = self.database.sweep_job_retention()
        self.assertEqual(deleted["pipeline_failed"], 1)
        self.assertIsNone(self.database.get_job(job_id))

    def test_queued_and_running_jobs_are_never_deleted_regardless_of_age(self) -> None:
        """The hard constraint: a job not in a terminal state must survive
        no matter how old created_at claims it is."""
        queued = self.database.enqueue("organize_apply", {"source": "/incoming"})
        self._age_job(queued.id, 400 * 24 * 60 * 60)
        self.database.enqueue("organize_apply", {"source": "/incoming"})
        running = self.database.claim_next("worker-a")
        assert running is not None
        self._age_job(running.id, 400 * 24 * 60 * 60)

        deleted = self.database.sweep_job_retention()
        self.assertEqual(sum(deleted.values()), 0)
        self.assertIsNotNone(self.database.get_job(queued.id))
        self.assertIsNotNone(self.database.get_job(running.id))

    def test_audit_events_are_deleted_alongside_their_job(self) -> None:
        """`audit_events` must not outlive the job row it describes -- every
        row in that table is target_type='job'/target_id=<job id> (see
        `_delete_jobs_batch`'s docstring), so a leftover row here would be
        pointing at a job that no longer exists."""
        job_id = self._make_pipeline("organize_apply", "succeeded")
        self._age_job(job_id, 91 * 24 * 60 * 60)
        with self.database.connect() as conn:
            before = conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE target_type = 'job' AND target_id = ?",
                (job_id,),
            ).fetchone()[0]
        self.assertGreater(before, 0)

        self.database.sweep_job_retention()

        with self.database.connect() as conn:
            after = conn.execute(
                "SELECT COUNT(*) FROM audit_events WHERE target_type = 'job' AND target_id = ?",
                (job_id,),
            ).fetchone()[0]
        self.assertEqual(after, 0)

    def test_reconciled_torrents_ledger_is_never_touched_by_a_sweep(self) -> None:
        """The hard safety constraint: sweeping job history must never prune
        the reconciler's idempotency ledger -- see the comment on that
        table's CREATE TABLE in initialize() for why a missing row there
        would make the reconciler re-import an already-imported torrent."""
        claimed = self.database.claim_torrent_import(
            "abc123", "Some Book (2020)", "transfer_completed", {"remote_path": "Some Book (2020)"}
        )
        assert claimed is not None
        self._age_job(claimed.id, 400 * 24 * 60 * 60)
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE reconciled_torrents SET created_at = '2000-01-01T00:00:00+00:00' WHERE hash = ?",
                ("abc123",),
            )
        self.database.sweep_job_retention()
        with self.database.connect() as conn:
            row = conn.execute("SELECT 1 FROM reconciled_torrents WHERE hash = ?", ("abc123",)).fetchone()
        self.assertIsNotNone(row)

    def test_batch_size_deletes_every_eligible_row_across_multiple_batches(self) -> None:
        """A regression guard for the batching itself: with batch_size=2 and
        5 eligible rows, the sweep must still remove every one of them by
        looping, not silently stop after the first batch."""
        job_ids = [self._make_pipeline("organize_apply", "succeeded") for _ in range(5)]
        for job_id in job_ids:
            self._age_job(job_id, 91 * 24 * 60 * 60)
        deleted = self.database.sweep_job_retention(batch_size=2)
        self.assertEqual(deleted["pipeline"], 5)
        for job_id in job_ids:
            self.assertIsNone(self.database.get_job(job_id))


class ListJobsIncludeReconcilerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="shelfmark-list-jobs-test-")
        self.database = Database(Path(self.tmp.name) / "state" / "shelfmark.db")
        self.database.initialize()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_default_still_includes_every_kind(self) -> None:
        """`list_jobs`'s OWN default must stay True: every existing caller
        (see e.g. TorrentImportLedgerTests and HasActiveJobTests above) relies
        on it seeing every kind unless told otherwise -- only api.py's
        endpoint flips its default at the HTTP boundary."""
        self.database.enqueue("reconcile_downloads", {})
        self.database.enqueue("organize_apply", {"source": "/incoming"})
        kinds = {job.kind for job in self.database.list_jobs(limit=50)}
        self.assertEqual(kinds, {"reconcile_downloads", "organize_apply"})

    def test_include_reconciler_false_hides_reconciler_ticks(self) -> None:
        self.database.enqueue("reconcile_downloads", {})
        self.database.enqueue("organize_apply", {"source": "/incoming"})
        kinds = {job.kind for job in self.database.list_jobs(limit=50, include_reconciler=False)}
        self.assertEqual(kinds, {"organize_apply"})

    def test_include_reconciler_false_combines_with_a_status_filter(self) -> None:
        self.database.enqueue("reconcile_downloads", {})
        self.database.enqueue("organize_apply", {"source": "/incoming"})
        jobs = self.database.list_jobs(status="queued", limit=50, include_reconciler=False)
        self.assertEqual([job.kind for job in jobs], ["organize_apply"])


if __name__ == "__main__":
    unittest.main()

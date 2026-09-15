from __future__ import annotations

import inspect
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from src.shelfmark_service.api import _job_response
from src.shelfmark_service.config import Settings
from src.shelfmark_service.db import Database, Job


def _job(**overrides: object) -> Job:
    fields: dict[str, object] = dict(
        id="job-1",
        kind="organize_apply",
        payload={},
        status="failed",
        attempts=1,
        created_at="2026-01-01T00:00:00+00:00",
        started_at=None,
        finished_at=None,
        heartbeat_at=None,
        worker_id="worker-a",
        error="source is not a directory: /incoming/x",
        error_code="source_missing",
        result=None,
        cancel_requested=False,
    )
    fields.update(overrides)
    return Job(**fields)  # type: ignore[arg-type]


class JobResponseTests(unittest.TestCase):
    """`GET /api/v1/jobs/{id}` has to carry the code alongside the message —
    the whole point of storing one is that a caller can branch on it instead
    of parsing `error`. `_job_response` is where the DB row becomes the API
    shape, so this is the one place that mapping can silently go missing."""

    def test_response_exposes_the_stable_code_under_a_short_key(self) -> None:
        response = _job_response(_job())
        self.assertEqual(response["code"], "source_missing")
        self.assertEqual(response["error"], "source is not a directory: /incoming/x")

    def test_a_pre_migration_row_exposes_a_null_code_not_a_crash(self) -> None:
        response = _job_response(_job(error_code=None))
        self.assertIsNone(response["code"])


from src.shelfmark_service import api as api_module


class FakeProwlarr:
    """Stands in for ProwlarrClient so these tests never touch the network —
    they exist to prove which categories reach the client, not to exercise
    HTTP transport (that's clients.py's job, and clients.py is off limits
    for this change)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def search(self, query, *, search_type=None, categories=None, limit=None, offset=None):
        self.calls.append({"query": query, "categories": categories})
        return []


class BookOnlyCategoryTests(unittest.TestCase):
    """The one configured indexer advertises 7000/7010/7030/7050, not 7020 —
    `/ebook-request` asks for book_only=true rather than naming 7020 directly,
    and the route is what is supposed to translate that into the configured
    PROWLARR_BOOK_CATEGORIES default. This is the exact gap the task named:
    ProwlarrClient.search() already accepted `categories`, but the route
    never passed anything through.
    """

    def test_book_only_applies_the_configured_book_categories(self) -> None:
        fake = FakeProwlarr()
        with mock.patch.object(api_module, "_prowlarr_client", return_value=fake):
            api_module.release_search(
                q="dune", search_type=None, categories=None, book_only=True,
                limit=50, offset=0, _actor="test",
            )
        self.assertEqual(fake.calls[0]["categories"], list(api_module.settings.prowlarr_book_categories))

    def test_book_only_false_leaves_categories_unset(self) -> None:
        """Must not change /release-search's existing (audiobook-inclusive)
        behavior for the command that doesn't ask for books specifically."""
        fake = FakeProwlarr()
        with mock.patch.object(api_module, "_prowlarr_client", return_value=fake):
            api_module.release_search(
                q="dune", search_type=None, categories=None, book_only=False,
                limit=50, offset=0, _actor="test",
            )
        self.assertIsNone(fake.calls[0]["categories"])

    def test_explicit_categories_override_book_only(self) -> None:
        fake = FakeProwlarr()
        with mock.patch.object(api_module, "_prowlarr_client", return_value=fake):
            api_module.release_search(
                q="dune", search_type=None, categories=[7060], book_only=True,
                limit=50, offset=0, _actor="test",
            )
        self.assertEqual(fake.calls[0]["categories"], [7060])


class ReadyzWorkerLivenessTests(unittest.TestCase):
    """`/readyz` is the endpoint a monitoring probe (Uptime Kuma) actually
    watches for a dead or wedged worker -- see api._worker_liveness's
    docstring for why "no row yet" (unknown) and "old row" (stale) must
    produce different outcomes, not collapse into one."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="shelfmark-readyz-test-")
        self.database = Database(Path(self.tmp.name) / "shelfmark.db")
        self.database.initialize()
        self.settings = Settings(
            database_path=self.database.path,
            worker_liveness_stale_seconds=60.0,
            transfer_timeout_seconds=500.0,
        )
        db_patch = mock.patch.object(api_module, "database", self.database)
        settings_patch = mock.patch.object(api_module, "settings", self.settings)
        db_patch.start()
        settings_patch.start()
        self.addCleanup(db_patch.stop)
        self.addCleanup(settings_patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def _age_liveness(self, worker_id: str) -> None:
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE worker_liveness SET last_seen_at = '2000-01-01T00:00:00+00:00' "
                "WHERE worker_id = ?",
                (worker_id,),
            )

    def test_no_liveness_row_reports_unknown_and_stays_ready(self) -> None:
        """A fresh deploy (or a database older than migration 4) must not be
        reported as a stale worker -- see the brief's explicit constraint."""
        response = api_module.readyz()
        self.assertEqual(response["worker"]["status"], "unknown")
        self.assertEqual(response["status"], "ready")

    def test_fresh_liveness_row_reports_ok_and_names_the_worker(self) -> None:
        self.database.record_liveness("worker-a")
        response = api_module.readyz()
        self.assertEqual(response["worker"]["status"], "ok")
        self.assertEqual(response["worker"]["worker_id"], "worker-a")

    def test_stale_liveness_row_fails_readyz_with_503(self) -> None:
        """A 200 saying "stale" in the body is invisible to an uptime monitor
        that only reads the status code -- this must be a non-2xx. No job is
        running, so there is no competing explanation for the silence."""
        self.database.record_liveness("worker-a")
        self._age_liveness("worker-a")
        with self.assertRaises(HTTPException) as ctx:
            api_module.readyz()
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail["worker"]["status"], "stale")

    def test_the_freshest_of_two_workers_is_reported(self) -> None:
        self.database.record_liveness("worker-old")
        self._age_liveness("worker-old")
        self.database.record_liveness("worker-new")
        response = api_module.readyz()
        self.assertEqual(response["worker"]["worker_id"], "worker-new")
        self.assertEqual(response["worker"]["status"], "ok")

    def test_stale_liveness_with_a_recently_started_running_job_reports_busy(self) -> None:
        """The false-alarm case: `Worker.run_once` runs one job to completion
        synchronously (no threads), so a big transfer's pull + settle-wait +
        checksum verify can legitimately outlast `worker_liveness_stale_seconds`
        with nothing wrong. A running job that started well within
        `transfer_timeout_seconds` (500s here) is evidence of that, not of a
        dead worker -- this must stay a 200, not page anyone."""
        self.database.record_liveness("worker-a")
        self._age_liveness("worker-a")
        self.database.enqueue("transfer_completed", {"remote_path": "Some Book"})
        claimed = self.database.claim_next("worker-a")
        assert claimed is not None
        response = api_module.readyz()
        self.assertEqual(response["status"], "ready")
        self.assertEqual(response["worker"]["status"], "busy")

    def test_stale_liveness_with_an_old_running_job_still_reports_stale(self) -> None:
        """The case the bound exists to prevent hiding forever: a job stuck
        in `running` past its own `transfer_timeout_seconds` ceiling is no
        longer credible evidence anyone is home -- either the job genuinely
        overran or the worker died mid-job and left the row stuck. Must
        still fail with 503, not be waved through as `busy` forever."""
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
        with self.assertRaises(HTTPException) as ctx:
            api_module.readyz()
        self.assertEqual(ctx.exception.status_code, 503)
        self.assertEqual(ctx.exception.detail["worker"]["status"], "stale")


class ListJobsEndpointTests(unittest.TestCase):
    """`GET /api/v1/jobs` -- on the live database, `reconcile_downloads`
    ticks (one every `SHELFMARK_RECONCILE_INTERVAL_SECONDS`, 60s default,
    forever) reached 98.6% of the `jobs` table, and at one point the last 40
    jobs in a row were reconciler noise burying every real pipeline job. The
    endpoint must default to hiding them; `include_reconciler=true` opts
    back in."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="shelfmark-list-jobs-endpoint-test-")
        self.database = Database(Path(self.tmp.name) / "shelfmark.db")
        self.database.initialize()
        db_patch = mock.patch.object(api_module, "database", self.database)
        db_patch.start()
        self.addCleanup(db_patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.database.enqueue("reconcile_downloads", {})
        self.database.enqueue("organize_apply", {"source": "/incoming"})

    def test_default_excludes_reconciler_ticks(self) -> None:
        """Calling with `include_reconciler=False` explicitly is what a
        request that OMITS the query param resolves to --
        `test_the_query_parameter_itself_defaults_to_false` below is what
        proves that resolution, since calling the endpoint function
        directly (there is no TestClient in this suite -- see
        ReadyzWorkerLivenessTests above for the same pattern) always
        requires passing every parameter explicitly."""
        response = api_module.list_jobs(_actor="test", job_status=None, limit=50, include_reconciler=False)
        kinds = {job["kind"] for job in response["jobs"]}
        self.assertEqual(kinds, {"organize_apply"})

    def test_include_reconciler_true_shows_them_again(self) -> None:
        response = api_module.list_jobs(_actor="test", job_status=None, limit=50, include_reconciler=True)
        kinds = {job["kind"] for job in response["jobs"]}
        self.assertEqual(kinds, {"reconcile_downloads", "organize_apply"})

    def test_the_query_parameter_itself_defaults_to_false(self) -> None:
        """A request that omits `?include_reconciler=...` entirely must
        still exclude reconciler ticks -- FastAPI resolves an omitted query
        parameter from the `Query(default=...)` object bound as this
        parameter's own Python default, so inspecting that default is what
        actually proves the HTTP-level behavior the two tests above cannot:
        both of them call the endpoint function directly and always pass
        `include_reconciler` explicitly."""
        default = inspect.signature(api_module.list_jobs).parameters["include_reconciler"].default
        self.assertIs(default.default, False)


if __name__ == "__main__":
    unittest.main()

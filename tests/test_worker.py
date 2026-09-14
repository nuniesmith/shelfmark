from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# worker.execute()'s organize_preview/organize_apply branch does
# `from main import apply_extracts, apply_plan, build_plan` — a bare,
# unqualified import. In the shipped container that resolves because the
# Dockerfile sets PYTHONPATH=/app/src, making main.py a top-level module.
# Nothing in this test run sets that, so a test that reaches that line needs
# the same layout locally or it fails with ModuleNotFoundError before the
# code under test ever executes — a pre-existing gap, not a regression: no
# earlier test exercised this code path (there was no test_worker.py) to
# catch it.
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from src.shelfmark_service.clients import ServiceError
from src.shelfmark_service.config import Settings
from src.shelfmark_service.db import Database
from src.shelfmark_service.errors import ErrorCode, ShelfmarkError
from src.shelfmark_service.transfer import TransferError
from src.shelfmark_service.worker import (
    Worker,
    _is_torrent_complete,
    _maybe_enqueue_reconcile,
    _split_webhook_url,
)


class WorkerTestCase(unittest.TestCase):
    """Shared plumbing: a real temp-file SQLite database, like test_service_db.py.

    Jobs go through enqueue()/claim_next() rather than being built by hand so
    each test exercises the same claimed-row shape `execute()` sees in
    production, error_code column included.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="shelfmark-worker-test-")
        self.database = Database(Path(self.tmp.name) / "state" / "shelfmark.db")
        self.database.initialize()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_worker(self, **settings_overrides: object) -> Worker:
        return Worker(self.database, Settings(**settings_overrides))  # type: ignore[arg-type]

    def make_job(self, kind: str, payload: dict[str, object]):
        self.database.enqueue(kind, payload)
        job = self.database.claim_next("test-worker")
        assert job is not None
        return job


class MetadataJobErrorCodeTests(WorkerTestCase):
    def test_metadata_match_without_config_reports_provider_not_configured(self) -> None:
        worker = self.make_worker()  # audiobookshelf_url/token both unset
        job = self.make_job("metadata_match", {"item_id": "42"})
        with self.assertRaises(ShelfmarkError) as ctx:
            worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.PROVIDER_NOT_CONFIGURED)

    def test_metadata_update_missing_fields_reports_invalid_payload(self) -> None:
        worker = self.make_worker(audiobookshelf_url="http://abs.internal", audiobookshelf_token="t")
        job = self.make_job("metadata_update", {})  # no item_id or media
        with self.assertRaises(ShelfmarkError) as ctx:
            worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PAYLOAD)

    def test_metadata_match_missing_item_id_reports_invalid_payload(self) -> None:
        worker = self.make_worker(audiobookshelf_url="http://abs.internal", audiobookshelf_token="t")
        job = self.make_job("metadata_match", {})  # no item_id
        with self.assertRaises(ShelfmarkError) as ctx:
            worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PAYLOAD)

    def test_library_scan_missing_library_id_reports_invalid_payload(self) -> None:
        worker = self.make_worker(audiobookshelf_url="http://abs.internal", audiobookshelf_token="t")
        job = self.make_job("library_scan", {})  # no library_id
        with self.assertRaises(ShelfmarkError) as ctx:
            worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PAYLOAD)

    def test_metadata_update_upstream_failure_reports_upstream_unavailable_without_leaking_body(
        self,
    ) -> None:
        worker = self.make_worker(
            audiobookshelf_url="http://abs.internal", audiobookshelf_token="super-secret-token"
        )
        job = self.make_job("metadata_update", {"item_id": "42", "media": {"title": "x"}})
        # The upstream response body is exactly the kind of thing api.py's
        # `_upstream_error` already refuses to surface ("can contain release
        # URLs, credentials, or other data") — a job's stored error deserves
        # the same rule, so this body must never reach the job record.
        leaking_body = "invalid bearer super-secret-token for item 42"
        with mock.patch(
            "src.shelfmark_service.worker.AudiobookshelfClient.update_media",
            side_effect=ServiceError("audiobookshelf", leaking_body, status=401),
        ):
            with self.assertRaises(ShelfmarkError) as ctx:
                worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.UPSTREAM_UNAVAILABLE)
        self.assertNotIn("super-secret-token", ctx.exception.message)
        self.assertNotIn("super-secret-token", str(ctx.exception.details))
        self.assertNotIn(leaking_body, ctx.exception.message)


class GrabReleaseErrorCodeTests(WorkerTestCase):
    def test_without_config_reports_provider_not_configured(self) -> None:
        worker = self.make_worker()  # prowlarr_url/api_key both unset
        job = self.make_job("grab_release", {"release": {"guid": "abc"}})
        with self.assertRaises(ShelfmarkError) as ctx:
            worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.PROVIDER_NOT_CONFIGURED)

    def test_missing_release_object_reports_invalid_payload(self) -> None:
        worker = self.make_worker(prowlarr_url="http://prowlarr.internal", prowlarr_api_key="k")
        job = self.make_job("grab_release", {})  # no release
        with self.assertRaises(ShelfmarkError) as ctx:
            worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PAYLOAD)

    def test_upstream_failure_reports_upstream_unavailable(self) -> None:
        worker = self.make_worker(prowlarr_url="http://prowlarr.internal", prowlarr_api_key="secret-key")
        job = self.make_job("grab_release", {"release": {"guid": "abc"}})
        with mock.patch(
            "src.shelfmark_service.worker.ProwlarrClient.grab",
            side_effect=ServiceError("prowlarr", "secret-key rejected", status=401),
        ):
            with self.assertRaises(ShelfmarkError) as ctx:
                worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.UPSTREAM_UNAVAILABLE)
        self.assertNotIn("secret-key", ctx.exception.message)


class TransferCompletedErrorCodeTests(WorkerTestCase):
    def _worker(self) -> Worker:
        return self.make_worker(sullivan_host="sullivan.internal", sullivan_user="shelfmark-sync")

    def test_without_config_reports_provider_not_configured(self) -> None:
        worker = self.make_worker()  # sullivan_host/user both unset
        job = self.make_job("transfer_completed", {"remote_path": "book"})
        with self.assertRaises(ShelfmarkError) as ctx:
            worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.PROVIDER_NOT_CONFIGURED)

    def test_missing_remote_path_reports_invalid_payload(self) -> None:
        worker = self._worker()
        job = self.make_job("transfer_completed", {})
        with self.assertRaises(ShelfmarkError) as ctx:
            worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PAYLOAD)

    def test_remote_path_outside_category_reports_invalid_payload(self) -> None:
        worker = self._worker()
        job = self.make_job("transfer_completed", {"remote_path": "/etc/passwd"})
        with self.assertRaises(ShelfmarkError) as ctx:
            worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PAYLOAD)

    def test_pull_failure_reports_upstream_unavailable(self) -> None:
        worker = self._worker()
        job = self.make_job("transfer_completed", {"remote_path": "book"})
        with mock.patch(
            "src.shelfmark_service.worker.RsyncTransfer.pull",
            side_effect=TransferError("Host key verification failed"),
        ):
            with self.assertRaises(ShelfmarkError) as ctx:
                worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.UPSTREAM_UNAVAILABLE)

    def test_transfer_that_never_stabilizes_reports_source_missing(self) -> None:
        worker = self._worker()
        job = self.make_job("transfer_completed", {"remote_path": "book"})
        with mock.patch("src.shelfmark_service.worker.RsyncTransfer.pull", return_value=None), mock.patch(
            "src.shelfmark_service.worker.wait_until_stable",
            side_effect=TransferError("no files found while waiting for transfer"),
        ):
            with self.assertRaises(ShelfmarkError) as ctx:
                worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.SOURCE_MISSING)

    def test_verify_run_failure_reports_upstream_unavailable(self) -> None:
        worker = self._worker()
        job = self.make_job("transfer_completed", {"remote_path": "book"})
        with mock.patch("src.shelfmark_service.worker.RsyncTransfer.pull", return_value=None), mock.patch(
            "src.shelfmark_service.worker.wait_until_stable", return_value={"book/01.mp3": (5, 1)}
        ), mock.patch(
            "src.shelfmark_service.worker.RsyncTransfer.verify",
            side_effect=TransferError("ssh connection closed"),
        ):
            with self.assertRaises(ShelfmarkError) as ctx:
                worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.UPSTREAM_UNAVAILABLE)

    def test_content_mismatch_reports_verification_failed(self) -> None:
        worker = self._worker()
        job = self.make_job("transfer_completed", {"remote_path": "book"})
        with mock.patch("src.shelfmark_service.worker.RsyncTransfer.pull", return_value=None), mock.patch(
            "src.shelfmark_service.worker.wait_until_stable", return_value={"book/01.mp3": (5, 1)}
        ), mock.patch(
            "src.shelfmark_service.worker.RsyncTransfer.verify", return_value=["book/01.mp3"]
        ):
            with self.assertRaises(ShelfmarkError) as ctx:
                worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.VERIFICATION_FAILED)
        self.assertEqual(ctx.exception.details["differing_count"], 1)


class OrganizeJobErrorCodeTests(WorkerTestCase):
    def test_unsupported_job_kind_reports_invalid_payload(self) -> None:
        worker = self.make_worker()
        job = self.make_job("not-a-real-kind", {})
        with self.assertRaises(ShelfmarkError) as ctx:
            worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PAYLOAD)

    def test_missing_source_directory_reports_source_missing(self) -> None:
        worker = self.make_worker()
        missing = Path(self.tmp.name) / "does-not-exist"
        job = self.make_job("organize_apply", {"source": str(missing)})
        with self.assertRaises(ShelfmarkError) as ctx:
            worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.SOURCE_MISSING)

    def test_broken_archive_reports_extraction_failed(self) -> None:
        source = Path(self.tmp.name) / "dump"
        source.mkdir()
        (source / "broken.zip").write_bytes(b"not a zip")
        worker = self.make_worker(manifest_root=Path(self.tmp.name) / "manifests")
        job = self.make_job("organize_apply", {"source": str(source)})
        with self.assertRaises(ShelfmarkError) as ctx:
            worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.EXTRACTION_FAILED)
        self.assertIn("broken.zip", ctx.exception.message)
        self.assertEqual(len(ctx.exception.details["errors"]), 1)


class RunOnceErrorPersistenceTests(WorkerTestCase):
    """The end-to-end path: execute() raises, run_once() writes it to the row."""

    def test_shelfmark_error_persists_its_code_and_message(self) -> None:
        worker = self.make_worker(manifest_root=Path(self.tmp.name) / "manifests")
        missing = Path(self.tmp.name) / "missing"
        self.database.enqueue("organize_apply", {"source": str(missing)})
        self.assertTrue(worker.run_once())
        failed = self.database.list_jobs(status="failed")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].error_code, ErrorCode.SOURCE_MISSING.value)
        self.assertIn(str(missing), failed[0].error or "")

    def test_unmapped_exception_persists_as_internal(self) -> None:
        # A bug in main.build_plan (or any surprise this module did not
        # anticipate) must still leave the job with SOME code — the whole
        # point of the except-Exception fallback in run_once(). "internal" is
        # that guaranteed floor, never a specific guess.
        source = Path(self.tmp.name) / "dump"
        source.mkdir()
        worker = self.make_worker(manifest_root=Path(self.tmp.name) / "manifests")
        self.database.enqueue("organize_preview", {"source": str(source)})
        with mock.patch("main.build_plan", side_effect=RuntimeError("disk exploded")):
            self.assertTrue(worker.run_once())
        failed = self.database.list_jobs(status="failed")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].error_code, ErrorCode.INTERNAL.value)
        self.assertIn("disk exploded", failed[0].error or "")


class TorrentCompleteStateTests(unittest.TestCase):
    """`_is_torrent_complete` is what stands between the reconciler and
    pulling a torrent qBittorrent is still writing to. See the comment on
    `_QBITTORRENT_COMPLETE_STATES` in worker.py for why `progress == 1` alone
    is not sufficient."""

    def test_seeding_at_full_progress_is_complete(self) -> None:
        self.assertTrue(_is_torrent_complete({"progress": 1.0, "state": "uploading"}))

    def test_stalled_seeding_is_complete(self) -> None:
        self.assertTrue(_is_torrent_complete({"progress": 1, "state": "stalledUP"}))

    def test_both_paused_and_stopped_naming_are_complete(self) -> None:
        # qBittorrent 5.x renamed pausedUP -> stoppedUP; both must be accepted
        # since this codebase does not pin a version.
        self.assertTrue(_is_torrent_complete({"progress": 1.0, "state": "pausedUP"}))
        self.assertTrue(_is_torrent_complete({"progress": 1.0, "state": "stoppedUP"}))

    def test_checking_at_full_progress_is_not_complete(self) -> None:
        # The exact trap named in the brief: 100% progress while qBittorrent
        # re-hashes files already on disk after its own restart.
        self.assertFalse(_is_torrent_complete({"progress": 1.0, "state": "checkingUP"}))

    def test_moving_at_full_progress_is_not_complete(self) -> None:
        # Content is being relocated to its final save path -- not yet at
        # content_path, so pulling now would race the move.
        self.assertFalse(_is_torrent_complete({"progress": 1.0, "state": "moving"}))

    def test_partial_progress_is_not_complete_even_in_an_up_state(self) -> None:
        self.assertFalse(_is_torrent_complete({"progress": 0.9, "state": "uploading"}))

    def test_missing_fields_are_not_complete(self) -> None:
        self.assertFalse(_is_torrent_complete({}))


class ReconcileDownloadsJobTests(WorkerTestCase):
    def _worker(self) -> Worker:
        return self.make_worker(qbittorrent_url="http://qbit.internal", qbittorrent_api_key="key")

    def test_without_url_reports_provider_not_configured(self) -> None:
        worker = self.make_worker()  # qbittorrent_url unset
        job = self.make_job("reconcile_downloads", {})
        with self.assertRaises(ShelfmarkError) as ctx:
            worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.PROVIDER_NOT_CONFIGURED)

    def test_without_credentials_reports_provider_not_configured(self) -> None:
        worker = self.make_worker(qbittorrent_url="http://qbit.internal")  # no key, no user/pass
        job = self.make_job("reconcile_downloads", {})
        with self.assertRaises(ShelfmarkError) as ctx:
            worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.PROVIDER_NOT_CONFIGURED)

    def test_login_failure_reports_upstream_unavailable(self) -> None:
        worker = self._worker()
        job = self.make_job("reconcile_downloads", {})
        with mock.patch(
            "src.shelfmark_service.worker.QBittorrentClient.login",
            side_effect=ServiceError("qbittorrent", "bad credentials", status=403),
        ):
            with self.assertRaises(ShelfmarkError) as ctx:
                worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.UPSTREAM_UNAVAILABLE)

    def test_only_genuinely_complete_torrents_are_claimed(self) -> None:
        worker = self._worker()
        job = self.make_job("reconcile_downloads", {})
        torrents = [
            {"hash": "h1", "name": "Book One", "progress": 1.0, "state": "uploading"},
            {"hash": "h2", "name": "Book Two", "progress": 1.0, "state": "checkingUP"},
            {"hash": "h3", "name": "Book Three", "progress": 1.0, "state": "stalledUP"},
            {"hash": "h4", "name": "Book Four", "progress": 0.5, "state": "downloading"},
        ]
        with mock.patch("src.shelfmark_service.worker.QBittorrentClient.login", return_value="Ok."), mock.patch(
            "src.shelfmark_service.worker.QBittorrentClient.torrents", return_value=torrents
        ):
            result = worker.execute(job)
        self.assertEqual(result["seen"], 4)
        self.assertEqual(result["completed"], 2)
        self.assertEqual(len(result["claimed_jobs"]), 2)
        queued = {j.payload["remote_path"]: j for j in self.database.list_jobs(status="queued", limit=50)}
        self.assertIn("Book One", queued)
        self.assertIn("Book Three", queued)
        self.assertNotIn("Book Two", queued)
        self.assertNotIn("Book Four", queued)
        self.assertEqual(queued["Book One"].payload["_reconcile_hash"], "h1")

    def test_the_same_hash_is_never_claimed_twice(self) -> None:
        """The idempotency requirement from the brief, exercised at the job
        level: a second reconcile pass over the same still-seeding torrent
        must not enqueue a second transfer_completed job."""
        worker = self._worker()
        torrents = [{"hash": "h1", "name": "Book One", "progress": 1.0, "state": "uploading"}]
        with mock.patch("src.shelfmark_service.worker.QBittorrentClient.login", return_value="Ok."), mock.patch(
            "src.shelfmark_service.worker.QBittorrentClient.torrents", return_value=torrents
        ):
            first_job = self.make_job("reconcile_downloads", {})
            first_result = worker.execute(first_job)
            # claim_torrent_import enqueued a transfer_completed job as a side
            # effect of the first pass. Drain it the way run_once() would
            # before the next reconcile job runs, so claim_next()'s FIFO
            # order (both rows share the same second-precision created_at)
            # can't hand it to make_job() below instead of a fresh
            # reconcile_downloads job.
            leftover = self.database.claim_next("test-worker")
            assert leftover is not None
            self.assertEqual(leftover.kind, "transfer_completed")
            second_job = self.make_job("reconcile_downloads", {})
            second_result = worker.execute(second_job)
        self.assertEqual(len(first_result["claimed_jobs"]), 1)
        self.assertEqual(second_result["claimed_jobs"], [])
        transfer_jobs = [j for j in self.database.list_jobs(limit=50) if j.kind == "transfer_completed"]
        self.assertEqual(len(transfer_jobs), 1)


class ChainAfterSuccessTests(WorkerTestCase):
    """`_chain_after_success` is the wiring between the three separate job
    kinds -- see the module docstring on it in worker.py."""

    def test_transfer_completed_chains_to_organize_apply(self) -> None:
        worker = self.make_worker()
        job = self.make_job(
            "transfer_completed",
            {"remote_path": "Book One", "_reconcile_hash": "h1", "_reconcile_name": "Book One"},
        )
        worker._chain_after_success(job, {"verified": True, "local_path": "/incoming/Book One"})
        queued = self.database.list_jobs(status="queued", limit=50)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].kind, "organize_apply")
        self.assertEqual(queued[0].payload["source"], "/incoming/Book One")
        self.assertEqual(queued[0].payload["_reconcile_hash"], "h1")

    def test_unverified_transfer_does_not_chain(self) -> None:
        # Defense in depth: execute() already raises VERIFICATION_FAILED
        # rather than returning a result when verify() finds a mismatch, so
        # this path should be unreachable in production -- but organize_apply
        # is destructive, so it is checked again here rather than trusted.
        worker = self.make_worker()
        job = self.make_job(
            "transfer_completed", {"remote_path": "Book One", "_reconcile_hash": "h1"}
        )
        worker._chain_after_success(job, {"verified": False, "local_path": "/incoming/Book One"})
        self.assertEqual(self.database.list_jobs(status="queued", limit=50), [])

    def test_manual_transfer_job_without_reconcile_marker_does_not_chain(self) -> None:
        # A job submitted through POST /api/v1/transfers/pull has no
        # _reconcile_hash key -- this feature must not start auto-organizing
        # transfers nobody asked it to chain.
        worker = self.make_worker()
        job = self.make_job("transfer_completed", {"remote_path": "Book One"})
        worker._chain_after_success(job, {"verified": True, "local_path": "/incoming/Book One"})
        self.assertEqual(self.database.list_jobs(status="queued", limit=50), [])

    def test_organize_apply_chains_to_library_scan_when_abs_is_configured(self) -> None:
        worker = self.make_worker(
            audiobookshelf_url="http://abs.internal",
            audiobookshelf_token="tok",
            audiobookshelf_library_id="lib-1",
        )
        job = self.make_job(
            "organize_apply",
            {"source": "/incoming/Book One", "_reconcile_hash": "h1", "_reconcile_name": "Book One"},
        )
        worker._chain_after_success(job, {"applied": True})
        queued = self.database.list_jobs(status="queued", limit=50)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].kind, "library_scan")
        self.assertEqual(queued[0].payload["library_id"], "lib-1")
        self.assertEqual(queued[0].payload["_reconcile_hash"], "h1")

    def test_organize_apply_notifies_instead_of_scanning_when_abs_is_not_configured(self) -> None:
        worker = self.make_worker()  # no audiobookshelf_* settings
        worker._notify = mock.Mock()  # type: ignore[method-assign]
        job = self.make_job(
            "organize_apply", {"source": "/incoming/Book One", "_reconcile_hash": "h1", "_reconcile_name": "Book One"}
        )
        worker._chain_after_success(job, {"applied": True})
        self.assertEqual(self.database.list_jobs(status="queued", limit=50), [])
        worker._notify.assert_called_once()
        self.assertIn("Book One", worker._notify.call_args[0][0])

    def test_library_scan_success_sends_the_completion_notification(self) -> None:
        worker = self.make_worker()
        worker._notify = mock.Mock()  # type: ignore[method-assign]
        job = self.make_job(
            "library_scan", {"library_id": "lib-1", "_reconcile_hash": "h1", "_reconcile_name": "Book One"}
        )
        worker._chain_after_success(job, {"scan_started": True})
        worker._notify.assert_called_once()
        message = worker._notify.call_args[0][0]
        self.assertIn("Book One", message)
        self.assertIn("library", message)


class ChainFailureNotificationTests(WorkerTestCase):
    def test_reconciler_originated_failure_is_notified(self) -> None:
        worker = self.make_worker(manifest_root=Path(self.tmp.name) / "manifests")
        worker._notify = mock.Mock()  # type: ignore[method-assign]
        missing = Path(self.tmp.name) / "missing"
        self.database.enqueue(
            "organize_apply",
            {"source": str(missing), "_reconcile_hash": "h1", "_reconcile_name": "Book One"},
        )
        self.assertTrue(worker.run_once())
        worker._notify.assert_called_once()
        message = worker._notify.call_args[0][0]
        self.assertIn("Book One", message)
        self.assertIn("organize_apply", message)
        self.assertIn(ErrorCode.SOURCE_MISSING.value, message)

    def test_manually_submitted_failure_is_not_notified(self) -> None:
        # Regression guard: a job with no _reconcile_hash (anything submitted
        # through the existing API/Discord commands) must not start paging
        # Discord just because this feature now exists.
        worker = self.make_worker(manifest_root=Path(self.tmp.name) / "manifests")
        worker._notify = mock.Mock()  # type: ignore[method-assign]
        missing = Path(self.tmp.name) / "missing"
        self.database.enqueue("organize_apply", {"source": str(missing)})
        self.assertTrue(worker.run_once())
        worker._notify.assert_not_called()


class SplitWebhookUrlTests(unittest.TestCase):
    def test_splits_the_token_off_a_normal_webhook_url(self) -> None:
        base, path = _split_webhook_url("https://discord.com/api/webhooks/123/tok")
        self.assertEqual(base, "https://discord.com/api/webhooks/123")
        self.assertEqual(path, "tok")

    def test_trailing_slash_is_ignored(self) -> None:
        base, path = _split_webhook_url("https://discord.com/api/webhooks/123/tok/")
        self.assertEqual(base, "https://discord.com/api/webhooks/123")
        self.assertEqual(path, "tok")


class NotifyWebhookTests(WorkerTestCase):
    def test_no_webhook_configured_makes_no_request(self) -> None:
        worker = self.make_worker()  # discord_webhook_url unset
        with mock.patch("src.shelfmark_service.worker.HttpClient") as http_client:
            worker._notify("hello")
        http_client.assert_not_called()

    def test_configured_webhook_posts_the_split_url_and_content(self) -> None:
        worker = self.make_worker(discord_webhook_url="https://discord.com/api/webhooks/123/tok")
        with mock.patch("src.shelfmark_service.worker.HttpClient") as http_client_cls:
            instance = http_client_cls.return_value
            worker._notify("a book arrived")
        http_client_cls.assert_called_once()
        self.assertEqual(http_client_cls.call_args[0][0], "https://discord.com/api/webhooks/123")
        instance.request.assert_called_once_with(
            "tok", method="POST", json_body={"content": "a book arrived"}
        )

    def test_malformed_webhook_url_does_not_raise(self) -> None:
        worker = self.make_worker(discord_webhook_url="https://discord.com")
        worker._notify("hello")  # must not raise

    def test_webhook_failure_does_not_raise(self) -> None:
        worker = self.make_worker(discord_webhook_url="https://discord.com/api/webhooks/123/tok")
        with mock.patch("src.shelfmark_service.worker.HttpClient") as http_client_cls:
            http_client_cls.return_value.request.side_effect = ServiceError("discord-webhook", "boom")
            worker._notify("hello")  # must not raise


class MaybeEnqueueReconcileTests(WorkerTestCase):
    def test_enqueues_when_due_and_nothing_active(self) -> None:
        settings = Settings(download_automation_enabled=True, reconcile_interval_seconds=60.0)
        result = _maybe_enqueue_reconcile(self.database, settings, now=100.0, last_reconcile=0.0)
        self.assertEqual(result, 100.0)
        self.assertTrue(self.database.has_active_job("reconcile_downloads"))

    def test_does_not_enqueue_before_the_interval_elapses(self) -> None:
        settings = Settings(download_automation_enabled=True, reconcile_interval_seconds=60.0)
        result = _maybe_enqueue_reconcile(self.database, settings, now=30.0, last_reconcile=0.0)
        self.assertEqual(result, 0.0)
        self.assertFalse(self.database.has_active_job("reconcile_downloads"))

    def test_does_not_enqueue_a_second_job_while_one_is_active(self) -> None:
        settings = Settings(download_automation_enabled=True, reconcile_interval_seconds=60.0)
        self.database.enqueue("reconcile_downloads", {})
        result = _maybe_enqueue_reconcile(self.database, settings, now=100.0, last_reconcile=0.0)
        self.assertEqual(result, 0.0)  # unchanged, so the next tick retries promptly
        jobs = [j for j in self.database.list_jobs(limit=50) if j.kind == "reconcile_downloads"]
        self.assertEqual(len(jobs), 1)

    def test_disabled_automation_never_enqueues(self) -> None:
        settings = Settings(download_automation_enabled=False, reconcile_interval_seconds=60.0)
        result = _maybe_enqueue_reconcile(self.database, settings, now=1000.0, last_reconcile=0.0)
        self.assertEqual(result, 0.0)
        self.assertFalse(self.database.has_active_job("reconcile_downloads"))


if __name__ == "__main__":
    unittest.main()

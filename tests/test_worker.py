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
from src.shelfmark_service.worker import Worker


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


if __name__ == "__main__":
    unittest.main()

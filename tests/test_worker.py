from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
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
    _maybe_record_liveness,
    _maybe_sweep_retention,
    _release_download_source,
    _split_webhook_url,
    check_liveness_cli,
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


class ReleaseDownloadSourceTests(unittest.TestCase):
    """`_release_download_source` is the fix itself: PR #17's `grab_release`
    called `ProwlarrClient.grab()`, which POSTs to Prowlarr's own
    `/api/v1/search` and lets PROWLARR route the release to whatever download
    client it has configured -- on the live system, one client fixed to
    category `prowlarr`, never `shelfmark-books`, so the reconciler (which
    only watches the latter) never saw it. This function is what lets
    `execute()` skip Prowlarr's routing and hand qBittorrent the release
    directly instead."""

    BASE = "http://prowlarr:9696"

    def test_rewrites_scheme_and_host_but_keeps_path_and_full_query_string(self) -> None:
        # apikey and link both live in the query string and are what actually
        # authorizes the download -- see the docstring on the function under
        # test for why only scheme+host may change.
        release = {
            "downloadUrl": "http://sullivan:9696/1/download?apikey=SECRET-KEY&link=abcDEF123%2F"
        }
        result = _release_download_source(release, self.BASE)
        self.assertEqual(
            result, "http://prowlarr:9696/1/download?apikey=SECRET-KEY&link=abcDEF123%2F"
        )

    def test_rewrites_to_a_custom_configured_base(self) -> None:
        release = {"downloadUrl": "http://sullivan:9696/1/download?apikey=k&link=x"}
        result = _release_download_source(release, "https://100.87.125.19:9696")
        self.assertEqual(result, "https://100.87.125.19:9696/1/download?apikey=k&link=x")

    def test_magnet_url_passes_through_completely_unchanged(self) -> None:
        # A magnet URI has no proxying host in front of it -- nothing to
        # rewrite, and rewriting it would corrupt the info-hash.
        release = {"magnetUrl": "magnet:?xt=urn:btih:abc123&dn=Some+Book"}
        result = _release_download_source(release, self.BASE)
        self.assertEqual(result, "magnet:?xt=urn:btih:abc123&dn=Some+Book")

    def test_download_url_is_preferred_over_magnet_url_when_both_are_present(self) -> None:
        release = {
            "downloadUrl": "http://sullivan:9696/1/download?apikey=k&link=x",
            "magnetUrl": "magnet:?xt=urn:btih:should-not-be-used",
        }
        result = _release_download_source(release, self.BASE)
        self.assertEqual(result, "http://prowlarr:9696/1/download?apikey=k&link=x")

    def test_magnet_url_is_used_when_download_url_is_absent(self) -> None:
        release = {"magnetUrl": "magnet:?xt=urn:btih:fallback"}
        result = _release_download_source(release, self.BASE)
        self.assertEqual(result, "magnet:?xt=urn:btih:fallback")

    def test_missing_both_urls_reports_invalid_payload(self) -> None:
        with self.assertRaises(ShelfmarkError) as ctx:
            _release_download_source({"guid": "abc"}, self.BASE)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PAYLOAD)

    def test_malformed_download_url_reports_invalid_payload_without_leaking_it(self) -> None:
        # "not-a-url" has no scheme/netloc for urlsplit to find -- and it
        # still carries an apikey-shaped query string, so the failure message
        # must describe the problem without ever echoing the value back.
        release = {"downloadUrl": "not-a-url?apikey=SECRET-KEY"}
        with self.assertRaises(ShelfmarkError) as ctx:
            _release_download_source(release, self.BASE)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PAYLOAD)
        self.assertNotIn("SECRET-KEY", ctx.exception.message)
        self.assertNotIn("SECRET-KEY", str(ctx.exception.details))

    def test_malformed_configured_base_reports_provider_not_configured(self) -> None:
        release = {"downloadUrl": "http://sullivan:9696/1/download?apikey=k&link=x"}
        with self.assertRaises(ShelfmarkError) as ctx:
            _release_download_source(release, "not-a-url")
        self.assertEqual(ctx.exception.code, ErrorCode.PROVIDER_NOT_CONFIGURED)


class GrabReleaseErrorCodeTests(WorkerTestCase):
    def _release(self) -> dict[str, object]:
        return {"guid": "abc", "downloadUrl": "http://sullivan:9696/1/download?apikey=k&link=x"}

    def test_without_qbittorrent_url_reports_provider_not_configured(self) -> None:
        worker = self.make_worker()  # qbittorrent_url/credentials all unset
        job = self.make_job("grab_release", {"release": self._release()})
        with self.assertRaises(ShelfmarkError) as ctx:
            worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.PROVIDER_NOT_CONFIGURED)

    def test_without_qbittorrent_credentials_reports_provider_not_configured(self) -> None:
        worker = self.make_worker(qbittorrent_url="http://qbit.internal")  # no key, no user/pass
        job = self.make_job("grab_release", {"release": self._release()})
        with self.assertRaises(ShelfmarkError) as ctx:
            worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.PROVIDER_NOT_CONFIGURED)

    def test_missing_release_object_reports_invalid_payload(self) -> None:
        worker = self.make_worker(qbittorrent_url="http://qbit.internal", qbittorrent_api_key="k")
        job = self.make_job("grab_release", {})  # no release
        with self.assertRaises(ShelfmarkError) as ctx:
            worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PAYLOAD)

    def test_non_dict_release_reports_invalid_payload(self) -> None:
        # NOTE on mutation testing: an empty/absent `release` (the test
        # above) turns out to raise INVALID_PAYLOAD even with the
        # `isinstance(release, dict)` guard deleted, because
        # `_release_download_source` independently rejects a release with
        # neither URL -- that test alone would not have proven this guard
        # does anything. THIS case is the one that actually distinguishes
        # it: a non-dict `release` (a string here) has no `.get()`, and
        # without the guard this becomes an unhandled AttributeError ->
        # ErrorCode.INTERNAL instead of INVALID_PAYLOAD.
        worker = self.make_worker(qbittorrent_url="http://qbit.internal", qbittorrent_api_key="k")
        job = self.make_job("grab_release", {"release": "not-a-dict"})
        with self.assertRaises(ShelfmarkError) as ctx:
            worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PAYLOAD)

    def test_release_missing_both_urls_reports_invalid_payload(self) -> None:
        worker = self.make_worker(qbittorrent_url="http://qbit.internal", qbittorrent_api_key="k")
        job = self.make_job("grab_release", {"release": {"guid": "abc"}})
        with self.assertRaises(ShelfmarkError) as ctx:
            worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.INVALID_PAYLOAD)

    def test_adds_to_qbittorrent_using_the_reconciler_s_own_configured_category(self) -> None:
        # The exact bug: a release grabbed through Prowlarr's own routing
        # landed in category "prowlarr", which the reconciler never watches.
        # Reading `qbittorrent_category` here (the SAME setting
        # `_reconcile_downloads` reads) is what makes that impossible to
        # repeat -- the two can no longer drift apart.
        worker = self.make_worker(
            qbittorrent_url="http://qbit.internal",
            qbittorrent_api_key="k",
            qbittorrent_category="a-custom-category",
        )
        job = self.make_job("grab_release", {"release": self._release()})
        with mock.patch(
            "src.shelfmark_service.worker.QBittorrentClient.login", return_value="api-key"
        ), mock.patch(
            "src.shelfmark_service.worker.QBittorrentClient.add_urls", return_value="Ok."
        ) as add_urls:
            result = worker.execute(job)
        add_urls.assert_called_once_with(
            ["http://prowlarr:9696/1/download?apikey=k&link=x"], category="a-custom-category"
        )
        self.assertTrue(result["submitted"])

    def test_upstream_failure_reports_upstream_unavailable_without_leaking_the_apikey(self) -> None:
        worker = self.make_worker(qbittorrent_url="http://qbit.internal", qbittorrent_api_key="k")
        job = self.make_job("grab_release", {"release": self._release()})
        with mock.patch(
            "src.shelfmark_service.worker.QBittorrentClient.login", return_value="api-key"
        ), mock.patch(
            "src.shelfmark_service.worker.QBittorrentClient.add_urls",
            # Worst case: the upstream error text itself echoes back the
            # secret-bearing URL it was given -- _upstream_failure must still
            # keep it out of the raised ShelfmarkError regardless.
            side_effect=ServiceError("qbittorrent", "rejected apikey=k", status=400),
        ):
            with self.assertRaises(ShelfmarkError) as ctx:
                worker.execute(job)
        self.assertEqual(ctx.exception.code, ErrorCode.UPSTREAM_UNAVAILABLE)
        self.assertNotIn("apikey=k", ctx.exception.message)

    def test_grab_failure_does_not_leak_the_apikey_into_the_stored_job_row_or_manifest(
        self,
    ) -> None:
        """The brief's specific constraint: the apikey lives in the query
        string of EVERY download URL, and a job row plus its manifest are
        both persisted -- so a grab failure must not write it to either."""
        secret = "top-secret-prowlarr-key"
        worker = self.make_worker(
            qbittorrent_url="http://qbit.internal",
            qbittorrent_api_key="k",
            manifest_root=Path(self.tmp.name) / "manifests",
        )
        release = {
            "downloadUrl": f"http://sullivan:9696/1/download?apikey={secret}&link=x"
        }
        self.database.enqueue("grab_release", {"release": release})
        with mock.patch(
            "src.shelfmark_service.worker.QBittorrentClient.login", return_value="api-key"
        ), mock.patch(
            "src.shelfmark_service.worker.QBittorrentClient.add_urls",
            side_effect=ServiceError("qbittorrent", f"rejected: {secret}", status=400),
        ):
            self.assertTrue(worker.run_once())
        failed = self.database.list_jobs(status="failed")
        self.assertEqual(len(failed), 1)
        self.assertNotIn(secret, failed[0].error or "")
        self.assertNotIn(secret, str(failed[0].error_code))
        manifest_path = Path(self.tmp.name) / "manifests" / f"{failed[0].id}.jsonl"
        self.assertNotIn(secret, manifest_path.read_text())


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
    kinds -- see the module docstring on it in worker.py.

    `source` must always be `incoming_root` (never the landed book path) and
    `dest` must always be an explicit, configured media root -- see
    `_chain_transfer_completed`'s docstring for exactly why: an omitted
    `dest` defaults to `source` in `execute()`, which means "organize" does
    nothing but rename in place.
    """

    def test_transfer_completed_with_only_audio_root_queues_one_audio_pass(self) -> None:
        worker = self.make_worker(incoming_root=Path("/incoming"), audio_root=Path("/audiobooks"))
        job = self.make_job(
            "transfer_completed",
            {"remote_path": "Book One", "_reconcile_hash": "h1", "_reconcile_name": "Book One"},
        )
        worker._chain_after_success(job, {"verified": True, "local_path": "/incoming/Book One"})
        queued = self.database.list_jobs(status="queued", limit=50)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].kind, "organize_apply")
        # The incoming ROOT, not the book's own landed subfolder -- see the
        # docstring on _chain_transfer_completed for why pointing source at
        # the book folder itself strips the very name build_plan reads
        # author/title/year from.
        self.assertEqual(queued[0].payload["source"], "/incoming")
        self.assertEqual(queued[0].payload["dest"], "/audiobooks")
        self.assertEqual(queued[0].payload["media"], "audio")
        self.assertEqual(queued[0].payload["_reconcile_hash"], "h1")

    def test_transfer_completed_with_only_ebook_root_queues_one_ebook_pass(self) -> None:
        worker = self.make_worker(incoming_root=Path("/incoming"), ebook_root=Path("/ebooks"))
        job = self.make_job(
            "transfer_completed", {"remote_path": "Book One", "_reconcile_hash": "h1"}
        )
        worker._chain_after_success(job, {"verified": True, "local_path": "/incoming/Book One"})
        queued = self.database.list_jobs(status="queued", limit=50)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].payload["source"], "/incoming")
        self.assertEqual(queued[0].payload["dest"], "/ebooks")
        self.assertEqual(queued[0].payload["media"], "ebook")

    def test_transfer_completed_with_both_roots_queues_one_pass_per_root(self) -> None:
        # A single download can hold both an audiobook and an ebook --
        # build_plan takes exactly one dest per call, so this has to be two
        # separate jobs, not one.
        worker = self.make_worker(
            incoming_root=Path("/incoming"), audio_root=Path("/audiobooks"), ebook_root=Path("/ebooks")
        )
        job = self.make_job(
            "transfer_completed", {"remote_path": "Book One", "_reconcile_hash": "h1"}
        )
        worker._chain_after_success(job, {"verified": True, "local_path": "/incoming/Book One"})
        queued = self.database.list_jobs(status="queued", limit=50)
        self.assertEqual(len(queued), 2)
        by_media = {j.payload["media"]: j for j in queued}
        self.assertEqual(set(by_media), {"audio", "ebook"})
        self.assertEqual(by_media["audio"].payload["dest"], "/audiobooks")
        self.assertEqual(by_media["ebook"].payload["dest"], "/ebooks")
        for j in queued:
            self.assertEqual(j.payload["source"], "/incoming")

    def test_transfer_completed_defaults_source_to_slash_incoming_when_unconfigured(self) -> None:
        # Matches transfer_completed's OWN fallback (local_root =
        # settings.incoming_root or Path("/incoming")) -- the organize pass
        # has to scan the exact directory the transfer actually wrote into.
        worker = self.make_worker(audio_root=Path("/audiobooks"))  # incoming_root unset
        job = self.make_job(
            "transfer_completed", {"remote_path": "Book One", "_reconcile_hash": "h1"}
        )
        worker._chain_after_success(job, {"verified": True, "local_path": "/incoming/Book One"})
        queued = self.database.list_jobs(status="queued", limit=50)
        self.assertEqual(queued[0].payload["source"], "/incoming")

    def test_transfer_completed_with_neither_root_configured_notifies_and_does_not_chain(self) -> None:
        # The exact bug being fixed: no configured destination must stop the
        # chain with a clear message, not fall back to organizing in place.
        worker = self.make_worker(incoming_root=Path("/incoming"))  # no audio_root/ebook_root
        worker._notify = mock.Mock()  # type: ignore[method-assign]
        job = self.make_job(
            "transfer_completed", {"remote_path": "Book One", "_reconcile_hash": "h1", "_reconcile_name": "Book One"}
        )
        worker._chain_after_success(job, {"verified": True, "local_path": "/incoming/Book One"})
        self.assertEqual(self.database.list_jobs(status="queued", limit=50), [])
        worker._notify.assert_called_once()
        message = worker._notify.call_args[0][0]
        self.assertIn("Book One", message)
        self.assertIn("AUDIOBOOKS_ROOT", message)
        self.assertIn("EBOOKS_ROOT", message)

    def test_unverified_transfer_does_not_chain(self) -> None:
        # Defense in depth: execute() already raises VERIFICATION_FAILED
        # rather than returning a result when verify() finds a mismatch, so
        # this path should be unreachable in production -- but organize_apply
        # is destructive, so it is checked again here rather than trusted.
        worker = self.make_worker(incoming_root=Path("/incoming"), audio_root=Path("/audiobooks"))
        job = self.make_job(
            "transfer_completed", {"remote_path": "Book One", "_reconcile_hash": "h1"}
        )
        worker._chain_after_success(job, {"verified": False, "local_path": "/incoming/Book One"})
        self.assertEqual(self.database.list_jobs(status="queued", limit=50), [])

    def test_manual_transfer_job_without_reconcile_marker_does_not_chain(self) -> None:
        # A job submitted through POST /api/v1/transfers/pull has no
        # _reconcile_hash key -- this feature must not start auto-organizing
        # transfers nobody asked it to chain.
        worker = self.make_worker(incoming_root=Path("/incoming"), audio_root=Path("/audiobooks"))
        job = self.make_job("transfer_completed", {"remote_path": "Book One"})
        worker._chain_after_success(job, {"verified": True, "local_path": "/incoming/Book One"})
        self.assertEqual(self.database.list_jobs(status="queued", limit=50), [])

    def test_organize_apply_with_zero_books_does_not_chain_or_notify(self) -> None:
        # The other half of the bug report: a pass whose media type was not
        # present in this torrent (books == 0) must not scan for, or
        # announce, a book that never arrived at that pass's destination.
        worker = self.make_worker(
            audiobookshelf_url="http://abs.internal",
            audiobookshelf_token="tok",
            audiobookshelf_library_id="lib-1",
        )
        worker._notify = mock.Mock()  # type: ignore[method-assign]
        job = self.make_job(
            "organize_apply",
            {
                "source": "/incoming",
                "dest": "/audiobooks",
                "media": "audio",
                "_reconcile_hash": "h1",
                "_reconcile_name": "Book One",
            },
        )
        worker._chain_after_success(job, {"applied": True, "books": 0})
        self.assertEqual(self.database.list_jobs(status="queued", limit=50), [])
        worker._notify.assert_not_called()

    def test_organize_apply_audio_pass_chains_to_library_scan_when_abs_is_configured(self) -> None:
        worker = self.make_worker(
            audiobookshelf_url="http://abs.internal",
            audiobookshelf_token="tok",
            audiobookshelf_library_id="lib-1",
        )
        job = self.make_job(
            "organize_apply",
            {
                "source": "/incoming",
                "dest": "/audiobooks",
                "media": "audio",
                "_reconcile_hash": "h1",
                "_reconcile_name": "Book One",
            },
        )
        worker._chain_after_success(job, {"applied": True, "books": 1})
        queued = self.database.list_jobs(status="queued", limit=50)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].kind, "library_scan")
        self.assertEqual(queued[0].payload["library_id"], "lib-1")
        self.assertEqual(queued[0].payload["_reconcile_hash"], "h1")

    def test_organize_apply_audio_pass_notifies_instead_of_scanning_when_abs_is_not_configured(self) -> None:
        worker = self.make_worker()  # no audiobookshelf_* settings
        worker._notify = mock.Mock()  # type: ignore[method-assign]
        job = self.make_job(
            "organize_apply",
            {
                "source": "/incoming",
                "dest": "/audiobooks",
                "media": "audio",
                "_reconcile_hash": "h1",
                "_reconcile_name": "Book One",
            },
        )
        worker._chain_after_success(job, {"applied": True, "books": 1})
        self.assertEqual(self.database.list_jobs(status="queued", limit=50), [])
        worker._notify.assert_called_once()
        self.assertIn("Book One", worker._notify.call_args[0][0])

    def test_organize_apply_ebook_pass_notifies_without_a_library_scan(self) -> None:
        # Audiobookshelf has no ebook library -- Shelfmark serves ebooks by
        # walking SHELFMARK_EBOOKS_ROOT directly, so an ebook pass must never
        # enqueue library_scan even when Audiobookshelf IS configured.
        worker = self.make_worker(
            audiobookshelf_url="http://abs.internal",
            audiobookshelf_token="tok",
            audiobookshelf_library_id="lib-1",
        )
        worker._notify = mock.Mock()  # type: ignore[method-assign]
        job = self.make_job(
            "organize_apply",
            {
                "source": "/incoming",
                "dest": "/ebooks",
                "media": "ebook",
                "_reconcile_hash": "h1",
                "_reconcile_name": "Book One",
            },
        )
        worker._chain_after_success(job, {"applied": True, "books": 1})
        self.assertEqual(self.database.list_jobs(status="queued", limit=50), [])
        worker._notify.assert_called_once()
        message = worker._notify.call_args[0][0]
        self.assertIn("Book One", message)
        self.assertIn("ebook", message)

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


class LibraryPlacementEndToEndTests(WorkerTestCase):
    """The regression class the bug report asked for: assert on the actual
    destination TREE, not merely that the job reported success. Every prior
    test in this file checked job outcome only, which is exactly how the
    "organized in place, library left empty" bug passed 179 tests."""

    def test_transfer_success_chain_lands_the_book_in_the_audio_library_root(self) -> None:
        incoming = Path(self.tmp.name) / "incoming"
        audio_root = Path(self.tmp.name) / "audiobooks"
        book_dir = incoming / "Ursula K Le Guin - The Dispossessed (1974)"
        book_dir.mkdir(parents=True)
        (book_dir / "01.mp3").write_bytes(b"track one")
        (book_dir / "02.mp3").write_bytes(b"track two")

        worker = self.make_worker(
            incoming_root=incoming,
            audio_root=audio_root,
            manifest_root=Path(self.tmp.name) / "manifests",
        )
        transfer_job = self.make_job(
            "transfer_completed",
            {"remote_path": book_dir.name, "_reconcile_hash": "h1", "_reconcile_name": book_dir.name},
        )
        # Chains the real organize_apply job -- this is the exact call
        # run_once() makes after a real transfer_completed success.
        worker._chain_after_success(
            transfer_job, {"verified": True, "local_path": str(book_dir)}
        )
        organize_job = self.database.claim_next("test-worker")
        assert organize_job is not None
        self.assertEqual(organize_job.kind, "organize_apply")
        result = worker.execute(organize_job)  # the REAL organizer, not mocked

        self.assertEqual(result["books"], 1)
        landed = list(audio_root.rglob("*.mp3"))
        self.assertEqual(len(landed), 2, f"expected 2 tracks under {audio_root}, found {landed}")
        # The folder name carries the metadata build_plan parsed out of the
        # ORIGINAL "Ursula K Le Guin - The Dispossessed (1974)" folder name --
        # the whole point of scanning incoming_root rather than the book path.
        self.assertTrue(
            any("Le Guin" in str(p) for p in landed), f"author missing from destination paths: {landed}"
        )
        self.assertTrue(
            any("Dispossessed" in str(p) for p in landed), f"title missing from destination paths: {landed}"
        )
        # Moved, not copied or left in place: nothing playable remains under
        # incoming once the organize pass has run.
        self.assertEqual(list(incoming.rglob("*.mp3")), [])


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


class MaybeRecordLivenessTests(WorkerTestCase):
    """The idle-worker case per-job heartbeats miss: see worker.py's
    `_maybe_record_liveness` docstring for the throttling rationale."""

    def test_first_call_writes_regardless_of_the_zero_sentinel(self) -> None:
        """`main()` seeds `last_liveness = 0.0` so the very first loop
        iteration always writes -- a fresh deploy's worker must not wait a
        full poll interval before it becomes visible to /readyz."""
        settings = Settings(poll_interval=2.0, worker_id="worker-a")
        result = _maybe_record_liveness(self.database, settings, now=100.0, last_liveness=0.0)
        self.assertEqual(result, 100.0)
        row = self.database.latest_worker_liveness()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["worker_id"], "worker-a")

    def test_does_not_write_again_inside_the_poll_interval(self) -> None:
        settings = Settings(poll_interval=2.0, worker_id="worker-a")
        first = _maybe_record_liveness(self.database, settings, now=100.0, last_liveness=0.0)
        # 1 second later is inside the 2-second poll interval.
        second = _maybe_record_liveness(self.database, settings, now=101.0, last_liveness=first)
        self.assertEqual(second, first)  # timer unchanged: the throttle held

    def test_writes_again_once_the_poll_interval_elapses(self) -> None:
        settings = Settings(poll_interval=2.0, worker_id="worker-a")
        first = _maybe_record_liveness(self.database, settings, now=100.0, last_liveness=0.0)
        second = _maybe_record_liveness(self.database, settings, now=103.0, last_liveness=first)
        self.assertEqual(second, 103.0)

    def test_two_different_worker_ids_each_get_their_own_row(self) -> None:
        _maybe_record_liveness(self.database, Settings(worker_id="worker-a"), now=100.0, last_liveness=0.0)
        _maybe_record_liveness(self.database, Settings(worker_id="worker-b"), now=100.0, last_liveness=0.0)
        with self.database.connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM worker_liveness").fetchone()[0]
        self.assertEqual(count, 2)


class MaybeSweepRetentionTests(WorkerTestCase):
    """`_maybe_sweep_retention` -- the throttle itself; `sweep_job_retention`'s
    own tests (test_service_db.py's JobRetentionSweepTests) cover which rows
    a sweep actually deletes."""

    def _make_old_pipeline_job(self, age_seconds: float) -> str:
        self.database.enqueue("organize_apply", {"source": "/incoming"})
        claimed = self.database.claim_next("worker-a")
        assert claimed is not None
        self.database.complete(claimed.id, "worker-a", {"books": 1})
        aged = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat(timespec="seconds")
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE jobs SET created_at = ?, finished_at = ? WHERE id = ?",
                (aged, aged, claimed.id),
            )
        return claimed.id

    def test_sweeps_when_due(self) -> None:
        job_id = self._make_old_pipeline_job(91 * 24 * 60 * 60)
        settings = Settings(retention_sweep_interval_seconds=3600.0)
        result = _maybe_sweep_retention(self.database, settings, now=3600.0, last_sweep=0.0)
        self.assertEqual(result, 3600.0)
        self.assertIsNone(self.database.get_job(job_id))

    def test_does_not_sweep_before_the_interval_elapses(self) -> None:
        job_id = self._make_old_pipeline_job(91 * 24 * 60 * 60)
        settings = Settings(retention_sweep_interval_seconds=3600.0)
        result = _maybe_sweep_retention(self.database, settings, now=1800.0, last_sweep=0.0)
        self.assertEqual(result, 0.0)  # timer unchanged: the throttle held
        self.assertIsNotNone(self.database.get_job(job_id))

    def test_sweeps_again_once_the_interval_elapses(self) -> None:
        settings = Settings(retention_sweep_interval_seconds=3600.0)
        first = _maybe_sweep_retention(self.database, settings, now=100.0, last_sweep=0.0)
        job_id = self._make_old_pipeline_job(91 * 24 * 60 * 60)
        second = _maybe_sweep_retention(self.database, settings, now=100.0 + 3600.0, last_sweep=first)
        self.assertEqual(second, 100.0 + 3600.0)
        self.assertIsNone(self.database.get_job(job_id))

    def test_settings_windows_reach_the_database_call(self) -> None:
        """A job aged past the DEFAULT 90-day pipeline window, but inside a
        deliberately widened `retention_pipeline_seconds`, must survive --
        proving the Settings fields actually reach `sweep_job_retention`
        rather than the sweep silently using its own hardcoded defaults."""
        job_id = self._make_old_pipeline_job(91 * 24 * 60 * 60)
        settings = Settings(
            retention_sweep_interval_seconds=3600.0,
            retention_pipeline_seconds=365 * 24 * 60 * 60.0,
        )
        _maybe_sweep_retention(self.database, settings, now=3600.0, last_sweep=0.0)
        self.assertIsNotNone(self.database.get_job(job_id))


class CheckLivenessCliTests(unittest.TestCase):
    """`shelfmark-worker-healthcheck` (the Docker healthcheck command,
    wired in pyproject.toml) -- must classify through the exact same
    `Database.worker_liveness_status` /readyz uses. This replaced an inline
    `python -c` one-liner in docker-compose.yml that checked liveness age
    only, with no exception for a job legitimately still running: that
    meant `docker ps` reported `shelfmark-worker` unhealthy during any long
    transfer even after `/readyz` was fixed to say `busy` for the same
    situation. These tests exist specifically to keep that from coming
    back for THIS entry point too."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="shelfmark-healthcheck-cli-test-")
        self.db_path = Path(self.tmp.name) / "shelfmark.db"
        self.database = Database(self.db_path)
        self.database.initialize()
        env_patch = mock.patch.dict(
            "os.environ",
            {
                "SHELFMARK_DB_PATH": str(self.db_path),
                "SHELFMARK_WORKER_LIVENESS_STALE_SECONDS": "60",
                "SHELFMARK_TRANSFER_TIMEOUT_SECONDS": "500",
            },
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.addCleanup(self.tmp.cleanup)

    def _age_liveness(self, worker_id: str) -> None:
        with self.database.connect() as conn:
            conn.execute(
                "UPDATE worker_liveness SET last_seen_at = '2000-01-01T00:00:00+00:00' "
                "WHERE worker_id = ?",
                (worker_id,),
            )

    def test_exits_zero_when_no_worker_has_ever_ticked(self) -> None:
        with self.assertRaises(SystemExit) as ctx:
            check_liveness_cli()
        self.assertEqual(ctx.exception.code, 0)

    def test_exits_zero_for_a_fresh_liveness_row(self) -> None:
        self.database.record_liveness("worker-a")
        with self.assertRaises(SystemExit) as ctx:
            check_liveness_cli()
        self.assertEqual(ctx.exception.code, 0)

    def test_exits_one_for_a_stale_row_with_no_running_job(self) -> None:
        self.database.record_liveness("worker-a")
        self._age_liveness("worker-a")
        with self.assertRaises(SystemExit) as ctx:
            check_liveness_cli()
        self.assertEqual(ctx.exception.code, 1)

    def test_exits_zero_for_a_stale_row_with_a_recently_started_running_job(self) -> None:
        """The exact case that must not regress: a legitimate long transfer
        must not flip `docker ps` to unhealthy."""
        self.database.record_liveness("worker-a")
        self._age_liveness("worker-a")
        self.database.enqueue("transfer_completed", {"remote_path": "Some Book"})
        claimed = self.database.claim_next("worker-a")
        assert claimed is not None
        with self.assertRaises(SystemExit) as ctx:
            check_liveness_cli()
        self.assertEqual(ctx.exception.code, 0)

    def test_exits_one_for_a_stale_row_with_an_old_running_job(self) -> None:
        """A job stuck in `running` past its own timeout ceiling must not
        hide a dead worker from `docker ps` forever."""
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
        with self.assertRaises(SystemExit) as ctx:
            check_liveness_cli()
        self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()


class OrganizeLeavesNoEmptyDirsTests(unittest.TestCase):
    """An automatic import must not leave the release's folder skeleton behind.

    The CLI has always swept these (`remove_empty_dirs` in `run()`), but the
    worker's organize path called `build_plan`/`apply_plan` directly and
    skipped it. After one real download `/incoming` held seven empty
    directories and no files, and they were never cleaned up — one tree per
    book, forever. Harmless in itself, but it makes "is anything still being
    imported?" impossible to answer by looking at the directory.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="shelfmark-sweep-")
        self.root = Path(self.tmp.name)
        self.incoming = self.root / "incoming"
        self.library = self.root / "library"
        self.library.mkdir(parents=True)
        # The real shape: a release folder with a per-book folder inside it.
        book = self.incoming / "Dune Saga - Frank Herbert Collection" / "Dune (1965)"
        book.mkdir(parents=True)
        (book / "Dune - Frank Herbert.epub").write_bytes(b"EPUB")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run(self) -> None:
        database = Database(self.root / "t.db")
        database.initialize()
        settings = Settings(
            database_path=self.root / "t.db",
            manifest_root=self.root / "manifests",
            incoming_root=self.incoming,
            ebook_root=self.library,
        )
        worker = Worker(database, settings)
        database.enqueue(
            "organize_apply",
            {"source": str(self.incoming), "dest": str(self.library), "media": "ebook"},
            actor="test",
        )
        worker.execute(database.claim_next("w"))

    def test_the_release_skeleton_is_swept(self) -> None:
        self._run()
        left = [
            str(p.relative_to(self.incoming))
            for p in self.incoming.rglob("*")
            if p.is_dir() and p.name != "trash"
        ]
        self.assertEqual(left, [], f"empty directories left in incoming: {left}")

    def test_the_book_still_reaches_the_library(self) -> None:
        """The sweep must not be achieved by simply not importing anything."""
        self._run()
        found = sorted(str(p.relative_to(self.library)) for p in self.library.rglob("*.epub"))
        self.assertEqual(found, ["Frank Herbert/1965 - Dune/Dune.epub"])

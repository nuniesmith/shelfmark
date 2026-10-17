"""Retries, live status messages, and the grab race (2026-10-17).

That day a book downloaded on Sullivan and never arrived: the copy to freddy
failed once (an SSH allow-list), the failed stage could never run again, and
the grab had already told the requester the book was "already on the server"
because qBittorrent listed the new torrent a moment after the grab looked.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from fastapi import HTTPException

from src.shelfmark_service import api as api_module
from src.shelfmark_service import status as status_module
from src.shelfmark_service.clients import ServiceError
from src.shelfmark_service.config import Settings
from src.shelfmark_service.db import Database, RetryRefused
from src.shelfmark_service.status import StatusBoard, render
from src.shelfmark_service.worker import Worker, _maybe_retry_failed

HASH = "6dc7a5cc5375c7f8cbf59945193aa767ad57d831"
NAME = "Stephen King + Peter Straub - Other Worlds Than These (m4b)"


class DatabaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="shelfmark-retry-test-")
        self.addCleanup(self.tmp.cleanup)
        self.database = Database(Path(self.tmp.name) / "shelfmark.db")
        self.database.initialize()

    def failed_transfer(self, code: str = "upstream_unavailable", attempt: int | None = None):
        payload = {"remote_path": NAME, "_reconcile_hash": HASH, "_reconcile_name": NAME}
        if attempt is not None:
            payload["_attempt"] = attempt
        self.database.enqueue("transfer_completed", payload, actor="reconciler")
        job = self.database.claim_next("w")
        assert job is not None
        self.database.fail(job.id, "w", "transfer from Sullivan failed: Permission denied (publickey).", code=code)
        return self.database.get_job(job.id)


class RetryJobTests(DatabaseCase):
    def test_a_retry_is_a_new_queued_copy_that_remembers_its_origin(self) -> None:
        failed = self.failed_transfer()
        retry = self.database.retry_job(failed.id, actor="discord:1")
        self.assertNotEqual(retry.id, failed.id)
        self.assertEqual(retry.kind, "transfer_completed")
        self.assertEqual(retry.status, "queued")
        self.assertEqual(retry.payload["_reconcile_hash"], HASH, "the chain must carry on from the retry")
        self.assertEqual(retry.payload["_retry_of"], failed.id)
        self.assertEqual(retry.payload["_attempt"], 2)
        self.assertEqual(self.database.get_job(failed.id).status, "failed", "the failure stays on record")

    def test_only_a_failed_job_can_be_retried(self) -> None:
        job = self.database.enqueue("library_scan", {"library_id": "x"})
        with self.assertRaises(RetryRefused):
            self.database.retry_job(job.id)
        with self.assertRaises(LookupError):
            self.database.retry_job("no-such-job")

    def test_the_same_failure_is_never_retried_twice(self) -> None:
        failed = self.failed_transfer()
        first = self.database.retry_job(failed.id)
        with self.assertRaises(RetryRefused) as ctx:
            self.database.retry_job(failed.id)
        self.assertEqual(ctx.exception.retry_job_id, first.id)

    def test_retried_and_manual_failures_are_not_awaiting_retry(self) -> None:
        # Every claim below happens with exactly one job queued, so FIFO
        # order (created_at has one-second resolution) cannot pick wrong.
        retried = self.failed_transfer()
        waiting = self.failed_transfer()
        self.database.enqueue("organize_apply", {"source": "/incoming"})  # by hand: no marker
        manual = self.database.claim_next("w")
        self.database.fail(manual.id, "w", "source is not a directory", code="source_missing")
        self.database.retry_job(retried.id)
        ids = {job.id for job in self.database.failed_pipeline_jobs_not_retried()}
        self.assertEqual(ids, {waiting.id})

    def test_a_short_id_resolves_only_when_it_is_unique(self) -> None:
        failed = self.failed_transfer()
        self.assertEqual(self.database.resolve_job_id(failed.id[:8]), failed.id)
        self.assertIsNone(self.database.resolve_job_id(failed.id[:5]), "too short to trust")
        self.assertIsNone(self.database.resolve_job_id("zzzzzzzz"))


class ImportStatusTests(DatabaseCase):
    def test_an_unclaimed_torrent_has_no_import_status(self) -> None:
        self.assertIsNone(self.database.import_status(HASH))

    def test_a_claimed_torrent_reports_its_newest_stage(self) -> None:
        claimed = self.database.claim_torrent_import(
            HASH, NAME, "transfer_completed",
            {"remote_path": NAME, "_reconcile_hash": HASH, "_reconcile_name": NAME},
        )
        job = self.database.claim_next("w")
        self.database.fail(job.id, "w", "transfer from Sullivan failed: denied", code="upstream_unavailable")
        status = self.database.import_status(HASH.upper())
        self.assertEqual(status["job_id"], claimed.id)
        self.assertEqual(status["stage"], "transfer_completed")
        self.assertEqual(status["status"], "failed")
        self.assertIn("denied", status["error"])


class AutomaticRetryTests(DatabaseCase):
    def settings(self, **overrides) -> Settings:
        return Settings(retry_check_interval_seconds=60.0, **overrides)  # type: ignore[arg-type]

    def at(self, job, seconds: float) -> datetime:
        return datetime.fromisoformat(job.finished_at) + timedelta(seconds=seconds)

    def retries(self) -> list:
        return [j for j in self.database.list_jobs("queued", 50) if j.payload.get("_retry_of")]

    def test_a_transient_failure_is_retried_after_its_wait(self) -> None:
        failed = self.failed_transfer()
        _maybe_retry_failed(self.database, self.settings(), 100.0, 0.0, wall_now=self.at(failed, 299))
        self.assertEqual(self.retries(), [], "too early: the first wait is 5 minutes")
        _maybe_retry_failed(self.database, self.settings(), 200.0, 0.0, wall_now=self.at(failed, 301))
        [retry] = self.retries()
        self.assertEqual(retry.payload["_retry_of"], failed.id)

    def test_the_last_attempt_is_not_retried(self) -> None:
        failed = self.failed_transfer(attempt=3)
        _maybe_retry_failed(self.database, self.settings(), 100.0, 0.0, wall_now=self.at(failed, 10_000))
        self.assertEqual(self.retries(), [])

    def test_a_failure_that_would_fail_the_same_way_is_not_retried(self) -> None:
        failed = self.failed_transfer(code="source_missing")
        _maybe_retry_failed(self.database, self.settings(), 100.0, 0.0, wall_now=self.at(failed, 10_000))
        self.assertEqual(self.retries(), [])

    def test_an_old_failure_is_left_alone(self) -> None:
        failed = self.failed_transfer()
        _maybe_retry_failed(self.database, self.settings(), 100.0, 0.0, wall_now=self.at(failed, 3 * 86400))
        self.assertEqual(self.retries(), [])

    def test_the_check_runs_at_most_once_an_interval(self) -> None:
        failed = self.failed_transfer()
        last = _maybe_retry_failed(self.database, self.settings(), 30.0, 0.0, wall_now=self.at(failed, 301))
        self.assertEqual(last, 0.0)
        self.assertEqual(self.retries(), [])


class FailureReportingTests(DatabaseCase):
    def make_worker(self) -> Worker:
        worker = Worker(self.database, Settings())  # type: ignore[call-arg]
        worker._status = mock.Mock()
        worker._notify = mock.Mock()  # type: ignore[method-assign]
        return worker

    def test_a_retryable_failure_updates_the_status_line_and_pages_nobody(self) -> None:
        worker = self.make_worker()
        worker._notify_chain_failure(self.failed_transfer(), "Permission denied (publickey).", "upstream_unavailable")
        worker._notify.assert_not_called()
        torrent_hash, _, changes = worker._status.update.call_args[0]
        self.assertEqual(torrent_hash, HASH)
        self.assertEqual(changes["copy"]["state"], "failed")
        self.assertIn("Retrying in 5 min (attempt 2 of 3)", changes["copy"]["detail"])

    def test_an_error_ending_in_a_full_stop_does_not_get_a_second_one(self) -> None:
        worker = self.make_worker()
        worker._notify_chain_failure(self.failed_transfer(), "Permission denied (publickey).", "upstream_unavailable")
        detail = worker._status.update.call_args[0][2]["copy"]["detail"]
        self.assertNotIn("..", detail)
        self.assertTrue(detail.startswith("Permission denied (publickey). Retrying in 5 min"), detail)

    def test_the_final_failure_pages_with_the_retry_command(self) -> None:
        worker = self.make_worker()
        failed = self.failed_transfer(attempt=3)
        worker._notify_chain_failure(failed, "Permission denied (publickey).", "upstream_unavailable")
        worker._notify.assert_called_once()
        notice = worker._notify.call_args[0][0]
        self.assertIn(f"/retry job:{failed.id}", notice)
        self.assertIn("copying to freddy", notice)
        changes = worker._status.update.call_args[0][2]
        self.assertIn("/retry job:", changes["copy"]["detail"])

    def test_a_retry_shows_its_stage_running_again(self) -> None:
        worker = self.make_worker()
        retry = self.database.retry_job(self.failed_transfer().id)
        worker._mark_retry_started(retry)
        changes = worker._status.update.call_args[0][2]
        self.assertEqual(changes["copy"], {"state": "active", "detail": "attempt 2 of 3"})


class GrabRaceTests(DatabaseCase):
    RELEASE = {"guid": "g", "title": NAME, "downloadUrl": "http://sullivan:9696/1/download?apikey=k&link=x"}

    def make_worker(self) -> Worker:
        worker = Worker(
            self.database,
            Settings(qbittorrent_url="http://q", qbittorrent_api_key="k", grab_appear_seconds=5.0),  # type: ignore[call-arg]
        )
        worker._status = mock.Mock()
        return worker

    def test_a_torrent_listed_a_moment_after_the_add_is_reported_added(self) -> None:
        """The live failure: the list straight after the add was empty, so
        the grab went on to "diagnose" its own new torrent as a duplicate."""
        worker = self.make_worker()
        client = mock.Mock()
        client.torrents.side_effect = [[], [], [{"hash": HASH.upper()}]]
        self.database.enqueue("grab_release", {"release": dict(self.RELEASE)})
        job = self.database.claim_next("w")
        with mock.patch("src.shelfmark_service.worker.QBittorrentClient", return_value=client), \
                mock.patch("src.shelfmark_service.worker.time.sleep"):
            result = worker.execute(job)
        self.assertEqual(result["state"], "added")
        self.assertEqual(result["infohash"], HASH)
        worker._status.update.assert_called_once_with(HASH, NAME, {"download": {"state": "active"}})

    def _diagnose(self, worker: Worker, listed: list[dict], before: set[str]):
        raw = b"d4:infod6:lengthi1e4:name4:book12:piece lengthi16384e6:pieces20:" + b"x" * 20 + b"ee"
        ctx = mock.MagicMock()
        ctx.__enter__.return_value.read.return_value = raw
        client = mock.Mock()
        client.torrents.return_value = listed
        from src.shelfmark_service.torrentmeta import parse

        infohash, _ = parse(raw)
        with mock.patch("src.shelfmark_service.worker.urllib.request.urlopen", return_value=ctx):
            return infohash, worker._explain_silent_grab(dict(self.RELEASE), client, "Ok.", before)

    def test_a_hash_that_was_not_there_before_is_this_grabs_own(self) -> None:
        worker = self.make_worker()
        raw_hash = self._diagnose(worker, [], set())[0]
        _, result = self._diagnose(worker, [{"hash": raw_hash, "name": "book"}], set())
        self.assertEqual(result["state"], "added")

    def test_a_real_duplicate_says_what_the_existing_copy_is_doing(self) -> None:
        worker = self.make_worker()
        raw_hash = self._diagnose(worker, [], set())[0]
        listed = [{"hash": raw_hash, "name": "book", "category": "shelfmark-books", "progress": 0.43, "state": "downloading"}]
        _, result = self._diagnose(worker, listed, {raw_hash})
        self.assertEqual(result["state"], "duplicate")
        self.assertEqual(result["progress"], 0.43)
        self.assertEqual(result["torrent_state"], "downloading")
        self.assertNotIn("import", result, "never claimed: no import status to report")


class StatusBoardTests(DatabaseCase):
    URL = "https://discord.com/api/webhooks/123/tok"

    def board(self, **overrides) -> StatusBoard:
        settings = Settings(discord_webhook_url=self.URL, **overrides)  # type: ignore[arg-type]
        return StatusBoard(self.database, settings)

    def test_render_shows_one_line_per_stage(self) -> None:
        text = render(NAME, {
            "download": {"state": "done"},
            "copy": {"state": "failed", "detail": "Permission denied"},
            "organize": {"state": "pending"},
        })
        self.assertEqual(text.splitlines(), [
            f"**{NAME}**",
            "✅ Downloaded",
            "❌ Copying to freddy: Permission denied",
            "▫️ Extracting and filing",
            "▫️ Adding to the library",
        ])

    def test_the_first_update_posts_and_later_ones_edit_the_same_message(self) -> None:
        http = mock.Mock()
        http.request.side_effect = [{"id": "999"}, {}]
        with mock.patch.object(status_module, "HttpClient", return_value=http):
            board = self.board()
            board.update(HASH, NAME, {"download": {"state": "active"}})
            board.update(HASH, "ignored", {"download": {"state": "done"}, "copy": {"state": "active"}})
        post, edit = http.request.call_args_list
        self.assertEqual(post.args[0], "tok")
        self.assertEqual(post.kwargs["params"], {"wait": "true"})
        self.assertEqual(edit.args[0], "tok/messages/999")
        self.assertEqual(edit.kwargs["method"], "PATCH")
        self.assertIn(NAME, edit.kwargs["json_body"]["content"], "the first title stays")
        self.assertEqual(edit.kwargs["json_body"]["allowed_mentions"], {"parse": []})
        self.assertEqual(self.database.get_status_message(HASH)["message_id"], "999")

    def test_a_message_started_mid_pipeline_shows_the_earlier_stages_done(self) -> None:
        """Live 2026-10-17: the first message for a book that failed before
        this shipped was written by its retry, and said "▫️ Downloading"
        above a copy that could only have started after the download."""
        http = mock.Mock()
        http.request.return_value = {"id": "1"}
        with mock.patch.object(status_module, "HttpClient", return_value=http):
            self.board().update(HASH, NAME, {"copy": {"state": "failed", "detail": "denied"}})
        lines = http.request.call_args.kwargs["json_body"]["content"].splitlines()
        self.assertEqual(lines[1], "✅ Downloaded")
        self.assertEqual(lines[2], "❌ Copying to freddy: denied")
        self.assertEqual(lines[3], "▫️ Extracting and filing", "later stages stay untouched")

    def test_a_message_deleted_in_discord_is_posted_again(self) -> None:
        self.database.save_status_message(HASH, NAME, "999", {})
        http = mock.Mock()
        http.request.side_effect = [ServiceError("discord-webhook", "gone", status=404), {"id": "1000"}]
        with mock.patch.object(status_module, "HttpClient", return_value=http):
            self.board().update(HASH, NAME, {"copy": {"state": "active"}})
        self.assertEqual(self.database.get_status_message(HASH)["message_id"], "1000")

    def test_discord_trouble_never_reaches_the_caller(self) -> None:
        http = mock.Mock()
        http.request.side_effect = RuntimeError("discord is down")
        with mock.patch.object(status_module, "HttpClient", return_value=http), \
                self.assertLogs("shelfmark.status", level="WARNING"):
            self.board().update(HASH, NAME, {"copy": {"state": "active"}})

    def test_switched_off_or_without_a_webhook_it_does_nothing(self) -> None:
        with mock.patch.object(status_module, "HttpClient") as http:
            self.board(status_messages_enabled=False).update(HASH, NAME, {"copy": {"state": "active"}})
            StatusBoard(self.database, Settings()).update(HASH, NAME, {"copy": {"state": "active"}})  # type: ignore[call-arg]
        http.assert_not_called()


class RetryEndpointTests(DatabaseCase):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(api_module, "database", self.database)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_failed_job_is_retried_by_its_short_id(self) -> None:
        failed = self.failed_transfer()
        response = api_module.retry_job(failed.id[:8], actor="discord:1")
        self.assertEqual(response["kind"], "transfer_completed")
        self.assertEqual(response["status"], "queued")

    def test_a_second_retry_is_a_conflict_that_names_the_first(self) -> None:
        failed = self.failed_transfer()
        first = api_module.retry_job(failed.id, actor="t")
        with self.assertRaises(HTTPException) as ctx:
            api_module.retry_job(failed.id, actor="t")
        self.assertEqual(ctx.exception.status_code, 409)
        self.assertEqual(ctx.exception.detail["retry_job_id"], first["id"])

    def test_a_job_shows_its_book_but_never_its_payload(self) -> None:
        from src.shelfmark_service.api import _job_response

        failed = self.failed_transfer()
        response = _job_response(self.database.retry_job(failed.id))
        self.assertEqual(response["name"], NAME)
        self.assertEqual(response["retry_of"], failed.id)
        self.assertEqual(response["attempt"], 2)
        grab = self.database.enqueue(
            "grab_release", {"release": {"downloadUrl": "http://p/dl?apikey=SECRET"}}
        )
        self.assertNotIn("SECRET", repr(_job_response(grab)))

    def test_an_unknown_job_is_not_found(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            api_module.retry_job("deadbeef", actor="t")
        self.assertEqual(ctx.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()

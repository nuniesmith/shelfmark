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
        """Opting OUT explicitly still searches every category.

        This used to be the default, and that was the bug: an unfiltered
        `/release-search dune` returned "Dune Part Two 2024 BluRay" and a Car
        SOS episode about a dune buggy. Searching everything is now something
        you have to ask for, not something you get by omission."""
        fake = FakeProwlarr()
        with mock.patch.object(api_module, "_prowlarr_client", return_value=fake):
            api_module.release_search(
                q="dune", search_type=None, categories=None, book_only=False,
                limit=50, offset=0, _actor="test",
            )
        self.assertIsNone(fake.calls[0]["categories"])

    def test_release_search_defaults_to_books(self) -> None:
        """The DEFAULT, not a value any caller passes.

        Every other test here names `book_only` explicitly, so all of them
        passed while the default was False and `/release-search` returned
        Blu-rays. The declared default is the thing that was wrong, so it is
        the thing to assert on.
        """
        default = inspect.signature(api_module.release_search).parameters["book_only"].default
        self.assertIs(default.default, True, "/release-search must find books unless told otherwise")

        # And that the default actually routes to the book categories.
        fake = FakeProwlarr()
        with mock.patch.object(api_module, "_prowlarr_client", return_value=fake):
            api_module.release_search(
                q="dune", search_type=None, categories=None, book_only=default.default,
                limit=50, offset=0, _actor="test",
            )
        self.assertEqual(
            fake.calls[0]["categories"], list(api_module.settings.prowlarr_book_categories)
        )

    def test_explicit_categories_override_book_only(self) -> None:
        fake = FakeProwlarr()
        with mock.patch.object(api_module, "_prowlarr_client", return_value=fake):
            api_module.release_search(
                q="dune", search_type=None, categories=[7060], book_only=True,
                limit=50, offset=0, _actor="test",
            )
        self.assertEqual(fake.calls[0]["categories"], [7060])


class MediaTypeCategoryTests(unittest.TestCase):
    """`/request type:audiobook` was previously impossible: `book_only`
    (and, before this change, every code path reaching this route) only ever
    applied PROWLARR_BOOK_CATEGORIES (7000), which is verified to return zero
    audiobooks — they live under 3030/100064. `media_type` is the new,
    additive parameter that lets a caller ask for either bucket by name
    instead of memorizing category ids.
    """

    def test_media_type_audiobook_applies_the_audiobook_categories(self) -> None:
        fake = FakeProwlarr()
        with mock.patch.object(api_module, "_prowlarr_client", return_value=fake):
            api_module.release_search(
                q="dune", search_type=None, categories=None, media_type="audiobook",
                book_only=True, limit=50, offset=0, _actor="test",
            )
        self.assertEqual(
            fake.calls[0]["categories"], list(api_module.settings.prowlarr_audiobook_categories)
        )

    def test_media_type_ebook_applies_the_book_categories(self) -> None:
        fake = FakeProwlarr()
        with mock.patch.object(api_module, "_prowlarr_client", return_value=fake):
            api_module.release_search(
                q="dune", search_type=None, categories=None, media_type="ebook",
                book_only=True, limit=50, offset=0, _actor="test",
            )
        self.assertEqual(
            fake.calls[0]["categories"], list(api_module.settings.prowlarr_book_categories)
        )

    def test_media_type_audiobook_wins_even_when_book_only_is_false(self) -> None:
        """`/request` always sends media_type; book_only is the OLDER, more
        generic flag. If book_only's False branch were checked first (or
        media_type merely OR'd in), passing book_only=False alongside
        media_type would silently drop the audiobook filter and search every
        category again -- the exact bug `book_only` itself was added to fix,
        recurring through the new parameter instead of the old one."""
        fake = FakeProwlarr()
        with mock.patch.object(api_module, "_prowlarr_client", return_value=fake):
            api_module.release_search(
                q="dune", search_type=None, categories=None, media_type="audiobook",
                book_only=False, limit=50, offset=0, _actor="test",
            )
        self.assertEqual(
            fake.calls[0]["categories"], list(api_module.settings.prowlarr_audiobook_categories)
        )

    def test_explicit_categories_override_media_type(self) -> None:
        fake = FakeProwlarr()
        with mock.patch.object(api_module, "_prowlarr_client", return_value=fake):
            api_module.release_search(
                q="dune", search_type=None, categories=[42], media_type="audiobook",
                book_only=True, limit=50, offset=0, _actor="test",
            )
        self.assertEqual(fake.calls[0]["categories"], [42])


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


class ConsumeTokenTests(unittest.TestCase):
    """`_consume_token` is the pure decision `RateLimiter.check` delegates
    to -- driven with explicit `now` values so nothing here sleeps, the same
    reason `is_permitted` in discord_bot.py is tested as a plain function."""

    def test_a_full_bucket_allows_a_request_and_spends_one_token(self) -> None:
        bucket = api_module._RateLimitBucket(tokens=5.0, updated_at=0.0)
        retry_after = api_module._consume_token(bucket, now=0.0, capacity=5.0, refill_per_second=1.0)
        self.assertIsNone(retry_after)
        self.assertAlmostEqual(bucket.tokens, 4.0)

    def test_an_empty_bucket_is_refused_with_the_exact_wait_time(self) -> None:
        bucket = api_module._RateLimitBucket(tokens=0.0, updated_at=0.0)
        retry_after = api_module._consume_token(bucket, now=0.0, capacity=5.0, refill_per_second=0.5)
        # One token needed / 0.5 tokens-per-second refill = 2 seconds.
        self.assertAlmostEqual(retry_after, 2.0)

    def test_refill_over_a_long_elapsed_time_is_capped_at_capacity(self) -> None:
        """A very stale bucket must not accumulate MORE than `capacity`
        tokens -- otherwise an actor idle for an hour could burst far past
        the configured limit the instant they return."""
        bucket = api_module._RateLimitBucket(tokens=0.0, updated_at=0.0)
        retry_after = api_module._consume_token(bucket, now=1000.0, capacity=3.0, refill_per_second=1.0)
        self.assertIsNone(retry_after)
        self.assertAlmostEqual(bucket.tokens, 2.0)  # capacity(3) - the 1 just spent

    def test_waiting_the_full_reported_time_allows_the_next_request(self) -> None:
        bucket = api_module._RateLimitBucket(tokens=1.0, updated_at=0.0)
        self.assertIsNone(api_module._consume_token(bucket, now=0.0, capacity=1.0, refill_per_second=1.0))
        retry_after = api_module._consume_token(bucket, now=0.1, capacity=1.0, refill_per_second=1.0)
        self.assertIsNotNone(retry_after)
        allowed = api_module._consume_token(
            bucket, now=0.1 + retry_after, capacity=1.0, refill_per_second=1.0
        )
        self.assertIsNone(allowed)


class RateLimiterTests(unittest.TestCase):
    """`RateLimiter.check` -- what every route's dependency calls through
    `_enforce_rate_limit`. Uses a caller-advanced fake clock (`self._now`)
    instead of a real one so nothing here sleeps."""

    def setUp(self) -> None:
        self._now = 0.0
        self.limiter = api_module.RateLimiter(clock=lambda: self._now)

    def test_requests_within_capacity_all_succeed(self) -> None:
        for _ in range(3):
            self.assertIsNone(
                self.limiter.check("read", "discord:1:2:3", capacity=3, refill_per_second=1.0)
            )

    def test_the_request_past_capacity_is_refused(self) -> None:
        for _ in range(3):
            self.limiter.check("read", "discord:1:2:3", capacity=3, refill_per_second=1.0)
        retry_after = self.limiter.check("read", "discord:1:2:3", capacity=3, refill_per_second=1.0)
        self.assertIsNotNone(retry_after)

    def test_a_different_actor_has_an_independent_budget(self) -> None:
        for _ in range(3):
            self.limiter.check("read", "discord:1:2:3", capacity=3, refill_per_second=1.0)
        self.assertIsNone(
            self.limiter.check("read", "discord:9:9:9", capacity=3, refill_per_second=1.0)
        )

    def test_the_action_tier_for_the_same_actor_is_an_independent_budget(self) -> None:
        """Reads and actions must not share one counter -- exhausting a
        search budget must never block a grab for the same person, and vice
        versa, since the two tiers exist because their real costs differ by
        orders of magnitude."""
        for _ in range(3):
            self.limiter.check("read", "discord:1:2:3", capacity=3, refill_per_second=1.0)
        self.assertIsNone(
            self.limiter.check("action", "discord:1:2:3", capacity=3, refill_per_second=1.0)
        )


class RateLimiterEvictionTests(unittest.TestCase):
    """The bucket table is keyed on the actor, and the actor is whatever
    X-Shelfmark-Actor says once the bearer token checks out -- so without
    eviction it grows without bound for the life of the container."""

    def setUp(self) -> None:
        self._now = 0.0
        self.limiter = api_module.RateLimiter(
            clock=lambda: self._now, max_actors=4, idle_eviction_seconds=3600.0
        )

    def _check(self, actor: str) -> float | None:
        return self.limiter.check("read", actor, capacity=2, refill_per_second=1.0)

    def test_the_table_never_grows_past_the_cap(self) -> None:
        for index in range(50):
            self._check(f"discord:{index}:1:1")
            self._now += 0.001
        self.assertLessEqual(len(self.limiter._buckets), 4)

    def test_an_idle_bucket_is_dropped_before_a_recent_one(self) -> None:
        self._check("idle")
        self._now += 7200.0  # two hours: "idle" has long since refilled
        for index in range(3):
            self._check(f"recent:{index}")
        self._check("new-arrival")
        self.assertNotIn(("read", "idle"), self.limiter._buckets)
        self.assertIn(("read", "recent:2"), self.limiter._buckets)

    def test_when_every_bucket_is_recent_the_stalest_is_the_one_dropped(self) -> None:
        """The idle cutoff frees nothing here -- every actor was seen
        seconds ago -- so the table is at its cap with nothing safely
        droppable, and the choice of WHICH to drop is the whole behaviour.
        Taking the freshest would evict whoever is mid-session."""
        for index in range(4):
            self._check(f"actor:{index}")
            self._now += 1.0
        self._check("new-arrival")
        self.assertNotIn(("read", "actor:0"), self.limiter._buckets)
        for index in (1, 2, 3):
            self.assertIn(("read", f"actor:{index}"), self.limiter._buckets)

    def test_a_spent_bucket_survives_while_the_table_has_room(self) -> None:
        """Eviction must not be a way to refill your own bucket early: as
        long as the table is under its cap, a rate-limited actor keeps the
        empty bucket that is currently refusing them."""
        self._check("heavy")
        self._check("heavy")
        self.assertIsNotNone(self._check("heavy"))
        for index in range(2):
            self._check(f"other:{index}")
        self._now += 0.5
        self.assertIsNotNone(self._check("heavy"))


class EnforceRateLimitTests(unittest.TestCase):
    """`_enforce_rate_limit` -- what `_read_actor`/`_action_actor` call.
    Patches the module-level `settings` and `_rate_limiter` (the same
    pattern `ReadyzWorkerLivenessTests` above uses for `database`), so this
    never touches the real process-wide limiter or a real clock."""

    def setUp(self) -> None:
        self._now = 0.0
        limiter = api_module.RateLimiter(clock=lambda: self._now)
        test_settings = Settings(
            rate_limit_read_max_requests=2,
            rate_limit_read_window_seconds=10.0,
            rate_limit_action_max_requests=1,
            rate_limit_action_window_seconds=10.0,
        )
        limiter_patch = mock.patch.object(api_module, "_rate_limiter", limiter)
        settings_patch = mock.patch.object(api_module, "settings", test_settings)
        limiter_patch.start()
        settings_patch.start()
        self.addCleanup(limiter_patch.stop)
        self.addCleanup(settings_patch.stop)

    def test_reads_up_to_the_configured_limit_are_allowed(self) -> None:
        api_module._enforce_rate_limit("read", "discord:1:2:3")
        api_module._enforce_rate_limit("read", "discord:1:2:3")  # limit is 2 -- both succeed

    def test_the_read_past_the_limit_raises_429_with_a_retry_time(self) -> None:
        api_module._enforce_rate_limit("read", "discord:1:2:3")
        api_module._enforce_rate_limit("read", "discord:1:2:3")
        with self.assertRaises(HTTPException) as ctx:
            api_module._enforce_rate_limit("read", "discord:1:2:3")
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertEqual(ctx.exception.detail["error"], "rate_limited")
        self.assertGreater(ctx.exception.detail["retry_after_seconds"], 0)
        self.assertIn("Retry-After", ctx.exception.headers)

    def test_the_action_tier_has_its_own_much_tighter_limit(self) -> None:
        api_module._enforce_rate_limit("action", "discord:1:2:3")  # limit is 1 -- succeeds
        with self.assertRaises(HTTPException) as ctx:
            api_module._enforce_rate_limit("action", "discord:1:2:3")
        self.assertEqual(ctx.exception.status_code, 429)

    def test_exhausting_reads_does_not_touch_the_same_actors_action_budget(self) -> None:
        api_module._enforce_rate_limit("read", "discord:1:2:3")
        api_module._enforce_rate_limit("read", "discord:1:2:3")
        with self.assertRaises(HTTPException):
            api_module._enforce_rate_limit("read", "discord:1:2:3")
        api_module._enforce_rate_limit("action", "discord:1:2:3")  # untouched by the read exhaustion

    def test_local_actor_is_exempt_no_matter_how_many_calls(self) -> None:
        """`local` is `_actor()`'s fallback when SHELFMARK_API_TOKEN isn't
        set at all -- the operator's own private-network access, not a
        Discord user. Exempted entirely rather than merely generous."""
        for _ in range(50):
            api_module._enforce_rate_limit("read", "local")
            api_module._enforce_rate_limit("action", "local")

    def test_bearer_actor_is_exempt_no_matter_how_many_calls(self) -> None:
        """`bearer` is `_actor()`'s fallback for a valid bearer token with no
        X-Shelfmark-Actor header -- a manual curl or monitoring script, not
        the Discord bot (which always sets that header)."""
        for _ in range(50):
            api_module._enforce_rate_limit("read", "bearer")
            api_module._enforce_rate_limit("action", "bearer")

    def test_a_custom_actor_label_is_not_exempt(self) -> None:
        """Only the exact sentinel strings `_actor()` itself falls back to
        are exempt -- NOT any actor that merely fails to start with
        'discord:'. X-Shelfmark-Actor is caller-supplied once the bearer
        token checks out, so a blanket 'not discord-prefixed' exemption
        would let a script dodge the limiter just by naming itself anything
        other than a discord:... string."""
        api_module._enforce_rate_limit("action", "cron-script")
        with self.assertRaises(HTTPException):
            api_module._enforce_rate_limit("action", "cron-script")


class RateLimitedErrorTests(unittest.TestCase):
    """`_rate_limited_error` builds the 429 body/headers directly -- this is
    what discord_bot._rate_limit_wait_text parses on the other end."""

    def test_the_wait_time_is_rounded_up_not_down(self) -> None:
        """41.2s reported as 41s would let a retry land BEFORE a token is
        actually available -- rounding up is what keeps the promise in the
        message true."""
        exc = api_module._rate_limited_error(41.2, "read")
        self.assertEqual(exc.detail["retry_after_seconds"], 42)
        self.assertEqual(exc.headers["Retry-After"], "42")

    def test_read_tier_message_says_searches(self) -> None:
        exc = api_module._rate_limited_error(5.0, "read")
        self.assertIn("searches", exc.detail["message"])

    def test_action_tier_message_says_actions_not_searches(self) -> None:
        exc = api_module._rate_limited_error(5.0, "action")
        self.assertIn("actions", exc.detail["message"])
        self.assertNotIn("searches", exc.detail["message"])

    def test_retry_after_is_never_advertised_as_zero(self) -> None:
        """A 0s wait in the message would be actively misleading -- the
        caller was JUST refused, so telling them to retry immediately is a
        promise this code cannot keep."""
        exc = api_module._rate_limited_error(0.0, "read")
        self.assertEqual(exc.detail["retry_after_seconds"], 1)

    def test_status_code_is_429(self) -> None:
        exc = api_module._rate_limited_error(1.0, "action")
        self.assertEqual(exc.status_code, 429)


class RateLimitWiringTests(unittest.TestCase):
    """WHICH tier each route is wired to -- a partition, not a hand-picked
    list: every GET route but /healthz and /readyz (which take no actor
    dependency at all, so they can NEVER be rate-limited) depends on
    `_read_actor`; every mutating route depends on `_action_actor`. Checked
    via the `Depends` object's own `.dependency` instead of by calling each
    route (most need a configured client/database to run at all) -- the same
    reason `test_release_search_defaults_to_books` above inspects a
    parameter default rather than invoking the route for that fact."""

    _READ_ROUTES = {
        "library_search": "_actor",
        "library_item": "_actor",
        "release_search": "_actor",
        "ebook_search": "_actor",
        "ebook_download": "actor",
        "downloads": "_actor",
        "list_jobs": "_actor",
        "get_job": "_actor",
    }
    _ACTION_ROUTES = {
        "update_metadata": "actor",
        "match_metadata": "actor",
        "scan_library": "actor",
        "grab_release": "actor",
        "pull_transfer": "actor",
        "create_job": "actor",
        "cancel_job": "actor",
    }

    def test_every_read_route_depends_on_read_actor(self) -> None:
        for func_name, param_name in self._READ_ROUTES.items():
            func = getattr(api_module, func_name)
            default = inspect.signature(func).parameters[param_name].default
            self.assertIs(
                default.dependency,
                api_module._read_actor,
                f"{func_name} must depend on _read_actor",
            )

    def test_every_action_route_depends_on_action_actor(self) -> None:
        for func_name, param_name in self._ACTION_ROUTES.items():
            func = getattr(api_module, func_name)
            default = inspect.signature(func).parameters[param_name].default
            self.assertIs(
                default.dependency,
                api_module._action_actor,
                f"{func_name} must depend on _action_actor",
            )

    def test_every_route_is_accounted_for_in_exactly_one_tier(self) -> None:
        """Guards the partition itself: 15 routes total (matching the
        brief), no overlap, nothing missing."""
        read = set(self._READ_ROUTES)
        action = set(self._ACTION_ROUTES)
        self.assertEqual(len(read & action), 0)
        self.assertEqual(len(read) + len(action), 15)

    def test_healthz_and_readyz_take_no_actor_dependency_at_all(self) -> None:
        """Not just 'a generous limit' -- these two never call
        _actor/_read_actor/_action_actor in the first place, so a monitoring
        probe (Uptime Kuma polls /readyz every 60s) cannot be rate-limited by
        construction, not by a case in this code remembering to skip it."""
        self.assertEqual(dict(inspect.signature(api_module.healthz).parameters), {})
        self.assertEqual(dict(inspect.signature(api_module.readyz).parameters), {})


# One real Audiobookshelf `/api/libraries/{id}/search` response, trimmed:
# an OBJECT whose book entries are each wrapped in a `libraryItem`, with
# five sibling keys that are not results at all.
ABS_SEARCH_PAYLOAD = {
    "book": [
        {"libraryItem": {"id": "item-1", "media": {"metadata": {"title": "Dune"}}}},
        {"libraryItem": {"id": "item-2", "media": {"metadata": {"title": "Dune Messiah"}}}},
    ],
    "narrators": [],
    "tags": [],
    "genres": [],
    "series": [],
    "authors": [],
}

# ...and one `/api/libraries/{id}/items` response: a DIFFERENT shape, whose
# items are bare, not wrapped.
ABS_ITEMS_PAYLOAD = {
    "results": [
        {"id": "item-1", "media": {"metadata": {"title": "Dune"}}},
        {"id": "item-2", "media": {"metadata": {"title": "Revelation Space"}}},
    ],
    "total": 190,
    "page": 0,
}


class LibrarySearchShapeTests(unittest.TestCase):
    """`/library type:audiobook` answered "No matching library items found."
    for a library of 190 books, for every query, since the day it shipped.

    The route returned Audiobookshelf's own payload untouched, so the bot
    received `{"results": {"book": [...], "authors": [...]}}` — and
    `_result_list` looks for a LIST under "results" and then for a
    top-level "book", so it found neither and returned nothing. Nothing
    errored anywhere; it just always said there was nothing there.
    """

    def setUp(self) -> None:
        self.client = mock.Mock()
        self.client.search.return_value = ABS_SEARCH_PAYLOAD
        self.client.list_items.return_value = ABS_ITEMS_PAYLOAD
        patcher = mock.patch.object(api_module, "_abs_client", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_search_returns_a_flat_list_of_library_items(self) -> None:
        payload = api_module.library_search(q="dune", limit=25, _actor="local")
        self.assertEqual([item["id"] for item in payload["results"]], ["item-1", "item-2"])

    def test_the_bot_can_actually_read_what_this_route_returns(self) -> None:
        """The bug lived in the SEAM, not in either side: the route was
        reasonable JSON and `_result_list` was a reasonable unwrapper, and
        together they produced nothing. Asserting the route's shape alone
        would not have caught it, so this crosses the boundary on purpose."""
        from src.shelfmark_service.discord_bot import _result_list

        payload = api_module.library_search(q="dune", limit=25, _actor="local")
        self.assertEqual(len(_result_list(payload)), 2)

    def test_an_empty_query_lists_the_library_instead_of_searching(self) -> None:
        payload = api_module.library_search(q="", limit=25, _actor="local")
        self.client.search.assert_not_called()
        self.client.list_items.assert_called_once()
        self.assertEqual([item["id"] for item in payload["results"]], ["item-1", "item-2"])

    def test_browsing_is_ordered_the_way_the_library_reads_on_disk(self) -> None:
        api_module.library_search(q="", limit=25, _actor="local")
        self.assertEqual(
            self.client.list_items.call_args.kwargs["sort"], "media.metadata.authorName"
        )

    def test_a_search_payload_missing_its_book_key_is_not_an_error(self) -> None:
        self.client.search.return_value = {"authors": [], "series": []}
        self.assertEqual(api_module.library_search(q="x", limit=25, _actor="local")["results"], [])


class BrowseLimitFitsEveryRouteTests(unittest.TestCase):
    """The bot asks for `_BROWSE_LIMIT` results on a browse. A route whose
    own ceiling sits below that does not trim the request — FastAPI rejects
    it with a 422, and the command dies outright. The two numbers live in
    different files and moved independently once already: raising the
    browse limit to 500 left the ebook route capped at 200.
    """

    @staticmethod
    def _ceiling(route) -> int:
        """The `le=` a route declares on its `limit`, read off the real
        route object rather than restated here — restating it is what let
        the two numbers drift in the first place. FastAPI keeps the bound
        in `Query.metadata` as an annotated-types `Le`, not as an
        attribute."""
        for parameter in inspect.signature(route).parameters.values():
            if parameter.name != "limit":
                continue
            for constraint in parameter.default.metadata:
                if hasattr(constraint, "le"):
                    return constraint.le
            raise AssertionError(f"{route.__name__}'s limit declares no upper bound")
        raise AssertionError(f"{route.__name__} has no limit parameter")

    def test_every_browsed_route_accepts_the_browse_limit(self) -> None:
        from src.shelfmark_service.discord_bot import _BROWSE_LIMIT

        for route in (api_module.library_search, api_module.ebook_search):
            with self.subTest(route=route.__name__):
                self.assertGreaterEqual(self._ceiling(route), _BROWSE_LIMIT)

    def test_the_browse_limit_clears_the_library_as_it_stands(self) -> None:
        """190 audiobooks on the day browsing shipped. A limit below that
        shows part of the shelf and says nothing about the rest — which is
        the exact failure browsing exists to remove."""
        from src.shelfmark_service.discord_bot import _BROWSE_LIMIT

        self.assertGreater(_BROWSE_LIMIT, 190)


class EbookBrowseTests(unittest.TestCase):
    """`/library type:ebook` with no query has to list the shelf. The route
    used to declare `q` with `min_length=1`, so the only way to find out what
    was on the server was to already know a word that appears in a title."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        for author, year, title in (
            ("Frank Herbert", 1965, "Dune"),
            ("Frank Herbert", 1969, "Dune Messiah"),
            ("Ursula K Le Guin", 1968, "A Wizard of Earthsea"),
        ):
            book = root / author / f"{year} - {title}"
            book.mkdir(parents=True)
            (book / f"{title}.epub").write_bytes(b"epub")
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(api_module, "_ebook_root", return_value=root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_an_empty_query_returns_every_book(self) -> None:
        results = api_module.ebook_search(q="", limit=50, _actor="local")["results"]
        self.assertEqual(len(results), 3)

    def test_a_query_still_narrows(self) -> None:
        results = api_module.ebook_search(q="earthsea", limit=50, _actor="local")["results"]
        self.assertEqual([book["author"] for book in results], ["Ursula K Le Guin"])

    def test_the_limit_still_applies_to_a_browse(self) -> None:
        results = api_module.ebook_search(q="", limit=2, _actor="local")["results"]
        self.assertEqual(len(results), 2)


if __name__ == "__main__":
    unittest.main()

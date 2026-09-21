from __future__ import annotations

import unittest
from unittest import mock

from src.shelfmark_service.clients import ServiceError
from src.shelfmark_service.discord_bot import (
    _clamp_page,
    _ebook_label,
    _has_next_page,
    _has_previous_page,
    _human_size,
    _int_set,
    _job_status_message,
    _library_query,
    _max_attachment_bytes,
    _needs_confirmation,
    _numbered_lines,
    _page_count,
    _page_position_text,
    _page_slice,
    _rate_limit_message,
    _rate_limit_wait_text,
    _request_query,
    _resolve_page_item,
    _too_large,
    blocking_problems,
    is_permitted,
)


class PermissionTests(unittest.TestCase):
    """The allow-list must fail CLOSED.

    It used to return True when no roles were configured, so a bot deployed
    before its role IDs were filled in let every member of the server run every
    command — `/organize-preview` among them, which takes an arbitrary absolute
    path and reports what is at it. Nothing looked wrong: the bot answered, and
    the access control appeared to be in force because it was sitting in the
    config waiting for a value.
    """

    def test_empty_allow_list_permits_nobody(self) -> None:
        self.assertFalse(is_permitted([111, 222], set()))
        self.assertFalse(is_permitted([], set()))

    def test_a_matching_role_is_permitted(self) -> None:
        self.assertTrue(is_permitted([111, 222], {222}))

    def test_a_non_matching_role_is_refused(self) -> None:
        self.assertFalse(is_permitted([111, 222], {999}))

    def test_a_member_with_no_roles_is_refused(self) -> None:
        self.assertFalse(is_permitted([], {222}))


class RoleParsingTests(unittest.TestCase):
    def test_ids_parse_with_surrounding_whitespace(self) -> None:
        self.assertEqual(_int_set(" 111 , 222 ,"), {111, 222})

    def test_unset_is_empty_rather_than_an_error(self) -> None:
        self.assertEqual(_int_set(None), set())
        self.assertEqual(_int_set(""), set())

    def test_a_malformed_id_is_rejected_not_silently_dropped(self) -> None:
        """Dropping it would narrow the allow-list without saying so; the
        caller turns this into a refuse-everything state with a log line."""
        with self.assertRaises(ValueError):
            _int_set("111,not-an-id")


class StartupTests(unittest.TestCase):
    """Missing configuration must not crash-loop.

    The container runs under `restart: unless-stopped`, so an exit becomes an
    immediate restart. Raising on a missing token produced a container that
    died and respawned forever, scrolling the one useful message out of the
    log — against the rule that a service with a missing credential should sit
    and wait for the next deployment.
    """

    def test_both_tokens_present_is_no_problem(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"DISCORD_BOT_TOKEN": "t", "SHELFMARK_API_TOKEN": "a"},
            clear=True,
        ):
            self.assertEqual(blocking_problems(), [])

    def test_each_missing_token_is_reported_by_name(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            problems = blocking_problems()
        self.assertEqual(len(problems), 2)
        self.assertTrue(any("DISCORD_BOT_TOKEN" in p for p in problems))
        self.assertTrue(any("SHELFMARK_API_TOKEN" in p for p in problems))

    def test_whitespace_only_counts_as_missing(self) -> None:
        """An unset GitHub secret writes an empty value, not an absent key."""
        with mock.patch.dict(
            "os.environ",
            {"DISCORD_BOT_TOKEN": "   ", "SHELFMARK_API_TOKEN": "a"},
            clear=True,
        ):
            problems = blocking_problems()
        self.assertEqual(len(problems), 1)
        self.assertIn("DISCORD_BOT_TOKEN", problems[0])

    def test_an_unconfigured_allow_list_does_not_block_startup(self) -> None:
        """Deliberately not a blocking problem.

        A bot that connects and refuses each command with a message saying why
        is far easier to diagnose from Discord than a container that never
        appears at all.
        """
        with mock.patch.dict(
            "os.environ",
            {"DISCORD_BOT_TOKEN": "t", "SHELFMARK_API_TOKEN": "a"},
            clear=True,
        ):
            self.assertEqual(blocking_problems(), [])


class JobStatusMessageTests(unittest.TestCase):
    """`/job` used to show only status and attempts — never the failure code
    or even the free-text reason. Split into a plain function for the same
    reason `is_permitted` is: it is checkable without a Discord interaction
    object graph, on the exact thing this module exists to fix."""

    def test_succeeded_job_has_no_code_line(self) -> None:
        message = _job_status_message({"id": "abc", "status": "succeeded", "attempts": 1}, "abc")
        self.assertNotIn("Code:", message)

    def test_queued_job_has_no_code_line(self) -> None:
        message = _job_status_message({"id": "abc", "status": "queued", "attempts": 0}, "abc")
        self.assertNotIn("Code:", message)

    def test_failed_job_shows_code_and_error(self) -> None:
        message = _job_status_message(
            {
                "id": "abc",
                "status": "failed",
                "attempts": 2,
                "code": "source_missing",
                "error": "source is not a directory: /incoming/x",
            },
            "abc",
        )
        self.assertIn("Code: `source_missing`", message)
        self.assertIn("Error: source is not a directory: /incoming/x", message)

    def test_legacy_failed_job_without_a_code_shows_unknown_not_a_blank(self) -> None:
        """A job that failed before migration 2 added the column has
        `code: None` from the API. The line has to say so plainly rather than
        silently disappearing, which would look identical to "this job never
        failed"."""
        message = _job_status_message(
            {"id": "abc", "status": "failed", "attempts": 1, "code": None, "error": "boom"}, "abc"
        )
        self.assertIn("Code: `unknown`", message)

    def test_cancelled_job_shows_its_code_without_an_error_line(self) -> None:
        message = _job_status_message(
            {"id": "abc", "status": "cancelled", "attempts": 1, "code": "cancelled", "error": None},
            "abc",
        )
        self.assertIn("Code: `cancelled`", message)
        self.assertNotIn("Error:", message)
class AttachmentLimitTests(unittest.TestCase):
    """The 10 MB Discord default must stay overridable for a boosted server."""

    def test_unset_falls_back_to_the_ten_megabyte_default(self) -> None:
        self.assertEqual(_max_attachment_bytes(None), 10_000_000)
        self.assertEqual(_max_attachment_bytes(""), 10_000_000)

    def test_a_boosted_server_can_raise_the_limit(self) -> None:
        self.assertEqual(_max_attachment_bytes("50"), 50_000_000)

    def test_garbage_input_is_rejected_not_silently_unlimited(self) -> None:
        """Falling back to "no limit" here would let an oversized upload through
        and fail as a confusing Discord exception instead of the clear message
        the size guard is there to produce."""
        with self.assertRaises(ValueError):
            _max_attachment_bytes("not-a-number")

    def test_human_size_reads_in_the_units_the_message_promises(self) -> None:
        self.assertEqual(_human_size(500), "500 B")
        self.assertEqual(_human_size(12_500_000), "12.5 MB")


class TooLargeTests(unittest.TestCase):
    """Checked BEFORE the fetch, so a Discord upload can never even be attempted
    for a file that would be refused — see EbookView._callback."""

    def test_a_file_under_the_limit_is_not_too_large(self) -> None:
        self.assertFalse(_too_large(5_000_000, 10_000_000))

    def test_a_file_over_the_limit_is_too_large(self) -> None:
        self.assertTrue(_too_large(15_000_000, 10_000_000))

    def test_a_missing_size_is_not_treated_as_too_large(self) -> None:
        self.assertFalse(_too_large(None, 10_000_000))


class NeedsConfirmationTests(unittest.TestCase):
    """Whether a Grab press must stop for confirmation before queuing.

    Deliberately the OPPOSITE of TooLargeTests' missing-size case below: a
    grab has no second, real-bytes check afterward (unlike EbookView, which
    re-checks the actual fetched size before sending) -- the job goes straight
    to a remote worker and its real size is never seen again here. A missing
    size is exactly what the mis-ranked, wrongly-categorized release that
    motivated this guard can have (a real search for "the stand" surfaced a
    26 GB "Westerns ... GraphicAudio Collection" ranked above the book
    actually searched for), so it must not be waved through the way
    `_too_large` waves it through for an attachment.
    """

    def test_a_release_under_the_threshold_needs_no_confirmation(self) -> None:
        self.assertFalse(_needs_confirmation(2_813_000_000, 5_000_000_000))

    def test_a_release_over_the_threshold_needs_confirmation(self) -> None:
        # The 26 GB collection release that outranked an actual search for
        # "the stand" by matching "Stand-Alone".
        self.assertTrue(_needs_confirmation(26_736_000_000, 5_000_000_000))

    def test_exactly_at_the_threshold_needs_no_confirmation(self) -> None:
        self.assertFalse(_needs_confirmation(5_000_000_000, 5_000_000_000))

    def test_a_missing_size_needs_confirmation(self) -> None:
        """The opposite call from `_too_large`'s: there is no second,
        real-bytes check downstream of a grab, so an unusable size must be
        treated as the risky case, not waved through."""
        self.assertTrue(_needs_confirmation(None, 5_000_000_000))

    def test_a_non_numeric_size_needs_confirmation(self) -> None:
        self.assertTrue(_needs_confirmation("unknown", 5_000_000_000))


class EbookLabelTests(unittest.TestCase):
    def test_label_includes_title_author_and_size(self) -> None:
        label = _ebook_label({"title": "Dune", "author": "Frank Herbert", "size": 2_500_000})
        self.assertIn("Dune", label)
        self.assertIn("Frank Herbert", label)
        self.assertIn("2.5 MB", label)

    def test_missing_author_is_left_out_rather_than_shown_as_none(self) -> None:
        label = _ebook_label({"title": "Dune", "size": 2_500_000})
        self.assertNotIn("None", label)


class LibraryQueryTests(unittest.TestCase):
    """`/library type:<choice> query:<text>` used to be two separate commands
    (`/library-search` hit Audiobookshelf, `/ebook-search` hit the on-disk
    root) so the wife had to know where her own books live before she could
    search for them. `_library_query` is the one function that now makes
    that call for her -- if the branch were flipped, `/library
    type:audiobook` would silently start walking the ebooks directory (or
    vice versa) while every other part of the command kept working, which is
    why this is asserted directly rather than only exercised end-to-end."""

    def test_audiobook_hits_audiobookshelf(self) -> None:
        endpoint, params = _library_query("audiobook", "dune")
        self.assertEqual(endpoint, "/api/v1/library/search")
        # 25, not the API's own default of 12: enough for 5 full pages of
        # 5 now that /library type:audiobook results are paged rather than
        # cut off at the old, unpaged 10.
        self.assertEqual(params, {"q": "dune", "limit": 25})

    def test_ebook_hits_the_on_disk_root(self) -> None:
        endpoint, params = _library_query("ebook", "dune")
        self.assertEqual(endpoint, "/api/v1/ebooks/search")
        # 25 is api.ebook_search's own ceiling (`le=25`) -- raised from 10
        # now that pagination makes a result past the old cutoff reachable.
        self.assertEqual(params, {"q": "dune", "limit": 25})


class RequestQueryTests(unittest.TestCase):
    """`/request type:<choice> query:<text>` replaces `/ebook-request` and
    `/release-search`, which had drifted into being the same call
    (`/api/v1/releases/search`, `book_only=true`) with no real difference
    left. `_request_query` is what now tells the API route which category
    bucket to apply via `media_type` -- see api.release_search. If `kind`
    were passed through under the wrong key (or dropped), the API would fall
    back to book_only's default and an audiobook request would silently
    search ebook categories instead, the exact gap this whole change closes.
    """

    def test_audiobook_sends_the_audiobook_media_type(self) -> None:
        endpoint, params = _request_query("audiobook", "dune")
        self.assertEqual(endpoint, "/api/v1/releases/search")
        self.assertEqual(params["media_type"], "audiobook")
        self.assertEqual(params["q"], "dune")

    def test_fetch_limit_is_50_not_the_old_25(self) -> None:
        """Raised so a mis-ranked real result landing past the old 25-item
        fetch (as "the stand" landed at position 3 of the 5 that used to be
        SHOWN) is at least fetched -- pagination is what then makes it
        reachable, but only if it was fetched to begin with."""
        _endpoint, params = _request_query("audiobook", "dune")
        self.assertEqual(params["limit"], 50)

    def test_ebook_sends_the_ebook_media_type(self) -> None:
        _endpoint, params = _request_query("ebook", "dune")
        self.assertEqual(params["media_type"], "ebook")


class PageCountTests(unittest.TestCase):
    def test_an_exact_multiple_of_the_page_size_divides_evenly(self) -> None:
        self.assertEqual(_page_count(25, page_size=5), 5)

    def test_a_partial_last_page_rounds_up(self) -> None:
        """23 items at 5 per page is 5 pages, not 4 -- the last one holds
        only 3, but it still needs a page to be reachable on."""
        self.assertEqual(_page_count(23, page_size=5), 5)

    def test_zero_items_is_still_one_page_not_zero(self) -> None:
        """A page number always has somewhere valid to land -- see the
        function's docstring; this case is defensive, since the "no
        results" message is sent before any paged view is built."""
        self.assertEqual(_page_count(0, page_size=5), 1)


class ClampPageTests(unittest.TestCase):
    def test_a_negative_page_clamps_to_zero(self) -> None:
        self.assertEqual(_clamp_page(-1, total=23, page_size=5), 0)

    def test_a_page_past_the_end_clamps_to_the_last_page(self) -> None:
        # 23 items -> 5 pages -> last valid 0-indexed page is 4.
        self.assertEqual(_clamp_page(99, total=23, page_size=5), 4)

    def test_an_in_range_page_is_left_alone(self) -> None:
        self.assertEqual(_clamp_page(2, total=23, page_size=5), 2)


class PageSliceTests(unittest.TestCase):
    def test_the_first_page_is_the_first_five_items(self) -> None:
        items = list(range(23))
        self.assertEqual(_page_slice(items, page=0, page_size=5), [0, 1, 2, 3, 4])

    def test_the_second_page_is_items_five_through_nine(self) -> None:
        """This is the exact slice a page-2 embed must list as "6.", "7.",
        etc -- see NumberedLinesTests and ResolvePageItemTests below for the
        button side of the same requirement."""
        items = list(range(23))
        self.assertEqual(_page_slice(items, page=1, page_size=5), [5, 6, 7, 8, 9])

    def test_a_partial_last_page_returns_only_the_remainder(self) -> None:
        items = list(range(23))
        self.assertEqual(_page_slice(items, page=4, page_size=5), [20, 21, 22])


class ResolvePageItemTests(unittest.TestCase):
    """`_resolve_page_item` is the ONE function every Grab/Send button
    callback resolves its target through instead of indexing its stored
    list directly -- see its docstring for the exact failure mode: a button
    relabelled "Grab 6" on page 2 that still silently points at item 0
    because the index it acts on was fixed when the view was first built
    and never recomputed against the current page.
    """

    def test_page_twos_first_slot_resolves_to_the_sixth_item_not_the_first(self) -> None:
        # Distinct dict markers rather than ints, so a wrong answer names
        # WHICH item came back instead of just a wrong number -- matching
        # how a real release/book list is shaped.
        items = [{"title": f"item-{i}"} for i in range(12)]
        # Page 2 is page=1 (0-indexed); its local slot 0 is displayed as
        # "6." in the embed and labelled "Grab 6" -- it must resolve to
        # items[5], the sixth item, not items[0].
        self.assertIs(_resolve_page_item(items, page=1, local_index=0), items[5])

    def test_page_ones_first_slot_still_resolves_to_the_first_item(self) -> None:
        items = [{"title": f"item-{i}"} for i in range(12)]
        self.assertIs(_resolve_page_item(items, page=0, local_index=0), items[0])

    def test_page_twos_last_slot_resolves_to_the_tenth_item(self) -> None:
        items = [{"title": f"item-{i}"} for i in range(12)]
        self.assertIs(_resolve_page_item(items, page=1, local_index=4), items[9])

    def test_a_slot_past_a_partial_last_pages_remainder_resolves_to_none(self) -> None:
        # 7 items -> page 1 (the second page) holds only items[5] and
        # items[6] -- slots 2, 3, 4 have nothing, so their buttons must be
        # disabled rather than resolve to whatever used to occupy them on
        # a fuller page.
        items = [{"title": f"item-{i}"} for i in range(7)]
        self.assertIsNone(_resolve_page_item(items, page=1, local_index=2))
        self.assertIs(_resolve_page_item(items, page=1, local_index=1), items[6])


class HasPreviousNextPageTests(unittest.TestCase):
    def test_page_zero_has_no_previous(self) -> None:
        self.assertFalse(_has_previous_page(0))

    def test_page_one_has_a_previous(self) -> None:
        self.assertTrue(_has_previous_page(1))

    def test_the_last_page_has_no_next(self) -> None:
        # 23 items, 5 per page -> last 0-indexed page is 4.
        self.assertFalse(_has_next_page(4, total=23, page_size=5))

    def test_an_earlier_page_has_a_next(self) -> None:
        self.assertTrue(_has_next_page(0, total=23, page_size=5))


class PagePositionTextTests(unittest.TestCase):
    """"6-10 of 25" -- the position text a page turn must always show, so
    paging never happens blind."""

    def test_the_first_page_reads_one_through_five(self) -> None:
        self.assertEqual(_page_position_text(0, total=25, page_size=5), "1-5 of 25")

    def test_the_second_page_reads_six_through_ten(self) -> None:
        self.assertEqual(_page_position_text(1, total=25, page_size=5), "6-10 of 25")

    def test_a_partial_last_page_reads_only_its_remainder(self) -> None:
        """23 items must read "21-23 of 23", not "21-25 of 23" -- the end
        of the range is clamped to the actual total."""
        self.assertEqual(_page_position_text(4, total=23, page_size=5), "21-23 of 23")

    def test_no_results_reads_zero_of_zero(self) -> None:
        self.assertEqual(_page_position_text(0, total=0, page_size=5), "0 of 0")


class NumberedLinesTests(unittest.TestCase):
    def test_numbering_starts_at_the_given_start_not_always_one(self) -> None:
        """Page 2's embed must read "6. ...", "7. ...", to agree with the
        "Grab 6"/"Grab 7" buttons beside it (see ResolvePageItemTests) --
        restarting the count at 1 every page would make the embed and the
        buttons disagree about which item is which."""
        items = [{"title": "Sixth"}, {"title": "Seventh"}]
        rendered = _numbered_lines(items, start=6, label_fn=lambda item: item["title"])
        self.assertEqual(rendered, "6. Sixth\n7. Seventh")

    def test_the_first_pages_numbering_still_starts_at_one(self) -> None:
        items = [{"title": "First"}]
        rendered = _numbered_lines(items, start=1, label_fn=lambda item: item["title"])
        self.assertEqual(rendered, "1. First")


class RateLimitWaitTextTests(unittest.TestCase):
    """`_rate_limit_wait_text` reads the exact JSON shape
    `api._rate_limited_error` produces (see api.py) -- these bodies are the
    contract between the two modules, so they're spelled out literally here
    rather than built through a fake HTTP round trip."""

    def test_a_short_wait_is_reported_in_seconds(self) -> None:
        body = '{"detail": {"retry_after_seconds": 42, "message": "..."}}'
        self.assertEqual(_rate_limit_wait_text(body), "Try again in 42 seconds.")

    def test_a_single_second_is_not_pluralized(self) -> None:
        body = '{"detail": {"retry_after_seconds": 1, "message": "..."}}'
        self.assertEqual(_rate_limit_wait_text(body), "Try again in 1 second.")

    def test_a_minute_or_more_is_reported_in_minutes_not_seconds(self) -> None:
        body = '{"detail": {"retry_after_seconds": 125, "message": "..."}}'
        self.assertEqual(_rate_limit_wait_text(body), "Try again in 2 minutes.")

    def test_exactly_one_minute_is_not_pluralized(self) -> None:
        body = '{"detail": {"retry_after_seconds": 60, "message": "..."}}'
        self.assertEqual(_rate_limit_wait_text(body), "Try again in 1 minute.")

    def test_malformed_json_falls_back_to_a_generic_message(self) -> None:
        """A malformed or unexpected body must not raise a SECOND exception
        from inside code that is already handling one -- this is called
        from an `except ServiceError` block."""
        self.assertEqual(_rate_limit_wait_text("not json"), "Try again in a minute.")

    def test_a_body_with_no_retry_after_falls_back_to_a_generic_message(self) -> None:
        self.assertEqual(_rate_limit_wait_text('{"detail": "no retry_after here"}'), "Try again in a minute.")


class RateLimitMessageTests(unittest.TestCase):
    """`_rate_limit_message` -- the full sentence a Discord follow-up sends
    for a 429. `kind` distinguishes "you have run too many searches" from
    "...too many grabs" so every command names what it was doing."""

    def test_names_the_kind_and_includes_the_wait_time(self) -> None:
        exc = ServiceError(
            "shelfmark-api",
            '{"detail": {"retry_after_seconds": 90, "message": "..."}}',
            status=429,
        )
        message = _rate_limit_message("searches", exc)
        self.assertEqual(message, "You have run too many searches. Try again in 2 minutes.")

    def test_a_different_kind_reads_correctly_too(self) -> None:
        exc = ServiceError(
            "shelfmark-api",
            '{"detail": {"retry_after_seconds": 5, "message": "..."}}',
            status=429,
        )
        message = _rate_limit_message("grabs", exc)
        self.assertEqual(message, "You have run too many grabs. Try again in 5 seconds.")


if __name__ == "__main__":
    unittest.main()

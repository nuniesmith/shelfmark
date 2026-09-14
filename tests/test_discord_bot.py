from __future__ import annotations

import unittest
from unittest import mock

from src.shelfmark_service.discord_bot import (
    _ebook_label,
    _human_size,
    _int_set,
    _job_status_message,
    _max_attachment_bytes,
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


class EbookLabelTests(unittest.TestCase):
    def test_label_includes_title_author_and_size(self) -> None:
        label = _ebook_label({"title": "Dune", "author": "Frank Herbert", "size": 2_500_000})
        self.assertIn("Dune", label)
        self.assertIn("Frank Herbert", label)
        self.assertIn("2.5 MB", label)

    def test_missing_author_is_left_out_rather_than_shown_as_none(self) -> None:
        label = _ebook_label({"title": "Dune", "size": 2_500_000})
        self.assertNotIn("None", label)


if __name__ == "__main__":
    unittest.main()

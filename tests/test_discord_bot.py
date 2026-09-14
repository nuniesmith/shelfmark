from __future__ import annotations

import unittest
from unittest import mock

from src.shelfmark_service.discord_bot import (
    _int_set,
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


if __name__ == "__main__":
    unittest.main()

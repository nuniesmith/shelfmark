from __future__ import annotations

import unittest
from unittest import mock

from src.shelfmark_service.config import Settings


class ProwlarrBookCategoriesTests(unittest.TestCase):
    """The one configured indexer advertises 7000/7010/7030/7050, not 7020 —
    filtering to 7020 alone would return zero results every time, so the
    default here has to be the general Books bucket, not "EBook" specifically.
    """

    def test_default_is_the_general_books_category(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(Settings.from_env().prowlarr_book_categories, (7000,))

    def test_the_category_list_is_configurable(self) -> None:
        with mock.patch.dict(
            "os.environ", {"PROWLARR_BOOK_CATEGORIES": "7000, 7010, 7030"}, clear=True
        ):
            self.assertEqual(Settings.from_env().prowlarr_book_categories, (7000, 7010, 7030))

    def test_malformed_categories_are_rejected_not_silently_dropped(self) -> None:
        with mock.patch.dict(
            "os.environ", {"PROWLARR_BOOK_CATEGORIES": "7000,not-a-number"}, clear=True
        ):
            with self.assertRaises(ValueError):
                Settings.from_env()


class ProwlarrAudiobookCategoriesTests(unittest.TestCase):
    """Measured against the live indexer (2026-09-16): 3030 (Audio/Audiobook,
    standard Newznab) and 100064 (this indexer's own AudioBook category) both
    return audiobooks; book_only's 7000 alone returns zero across a 103-result
    sample. 100064 is indexer-specific, so — like prowlarr_book_categories —
    this has to stay configurable rather than hardcoded.
    """

    def test_default_is_the_measured_audiobook_categories(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(Settings.from_env().prowlarr_audiobook_categories, (3030, 100064))

    def test_the_category_list_is_configurable(self) -> None:
        with mock.patch.dict(
            "os.environ", {"PROWLARR_AUDIOBOOK_CATEGORIES": "3030, 100064, 100065"}, clear=True
        ):
            self.assertEqual(
                Settings.from_env().prowlarr_audiobook_categories, (3030, 100064, 100065)
            )

    def test_malformed_categories_are_rejected_not_silently_dropped(self) -> None:
        with mock.patch.dict(
            "os.environ", {"PROWLARR_AUDIOBOOK_CATEGORIES": "3030,not-a-number"}, clear=True
        ):
            with self.assertRaises(ValueError):
                Settings.from_env()


class DownloadAutomationSettingsTests(unittest.TestCase):
    """The reconciler's off switch and timing knobs -- defaults must keep
    automation ON (per the brief: "an off switch, defaulting to ON")."""

    def test_automation_defaults_to_enabled(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertTrue(Settings.from_env().download_automation_enabled)

    def test_automation_can_be_disabled(self) -> None:
        with mock.patch.dict(
            "os.environ", {"SHELFMARK_DOWNLOAD_AUTOMATION_ENABLED": "false"}, clear=True
        ):
            self.assertFalse(Settings.from_env().download_automation_enabled)

    def test_reconcile_interval_default(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(Settings.from_env().reconcile_interval_seconds, 60.0)

    def test_reconcile_interval_is_configurable(self) -> None:
        with mock.patch.dict(
            "os.environ", {"SHELFMARK_RECONCILE_INTERVAL_SECONDS": "15"}, clear=True
        ):
            self.assertEqual(Settings.from_env().reconcile_interval_seconds, 15.0)

    def test_qbittorrent_category_defaults_to_the_downloads_endpoint_default(self) -> None:
        # Keeping these in sync means a reconciler pass and a manual
        # /downloads look at the same category unless both are overridden.
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(Settings.from_env().qbittorrent_category, "shelfmark-books")

    def test_qbittorrent_category_is_configurable(self) -> None:
        with mock.patch.dict("os.environ", {"QBITTORRENT_CATEGORY": "other-books"}, clear=True):
            self.assertEqual(Settings.from_env().qbittorrent_category, "other-books")

    def test_qbittorrent_prowlarr_base_url_defaults_to_the_shared_docker_network_name(self) -> None:
        # Verified live: `prowlarr:9696` answers from inside the qBittorrent
        # container on the shared `sullivan_download` network; Prowlarr's own
        # `sullivan:9696` hostname does not. Matches the container-name
        # convention PROWLARR_URL already uses in .env.example.
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(
                Settings.from_env().qbittorrent_prowlarr_base_url, "http://prowlarr:9696"
            )

    def test_qbittorrent_prowlarr_base_url_is_configurable(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"QBITTORRENT_PROWLARR_BASE_URL": "http://100.87.125.19:9696"},
            clear=True,
        ):
            self.assertEqual(
                Settings.from_env().qbittorrent_prowlarr_base_url, "http://100.87.125.19:9696"
            )

    def test_discord_webhook_url_defaults_to_none(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertIsNone(Settings.from_env().discord_webhook_url)

    def test_discord_webhook_url_is_configurable(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"SHELFMARK_DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/1/tok"},
            clear=True,
        ):
            self.assertEqual(
                Settings.from_env().discord_webhook_url,
                "https://discord.com/api/webhooks/1/tok",
            )


class DiscordLargeReleaseThresholdTests(unittest.TestCase):
    """A release at/over this many MB is confirmed, not queued immediately,
    on a Discord Grab press -- see ReleaseView/_needs_confirmation in
    discord_bot.py. Default picked well above the longest single audiobook
    seen in practice (Stephen King's THE STAND, unabridged, is 2813 MB) and
    well below the 26 GB mis-ranked collection release that motivated the
    guard in the first place.
    """

    def test_default_is_five_thousand_megabytes(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertEqual(Settings.from_env().discord_large_release_threshold_mb, 5000.0)

    def test_threshold_is_configurable(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"SHELFMARK_DISCORD_LARGE_RELEASE_THRESHOLD_MB": "8000"},
            clear=True,
        ):
            self.assertEqual(
                Settings.from_env().discord_large_release_threshold_mb, 8000.0
            )

    def test_malformed_threshold_is_rejected_not_silently_dropped(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"SHELFMARK_DISCORD_LARGE_RELEASE_THRESHOLD_MB": "not-a-number"},
            clear=True,
        ):
            with self.assertRaises(ValueError):
                Settings.from_env()


class RateLimitSettingsTests(unittest.TestCase):
    """Defaults are deliberately generous per the brief -- a limit that
    fires during normal two-user use is worse than none. Reads (30/60s)
    cover several `/request`/`/library` searches in a sitting (each costs
    exactly one API call -- paging is client-side); actions (10/60s) are
    tighter because a grab reaches IPTorrents through Prowlarr's proxy and
    queues real bandwidth/seeding obligations. See api._enforce_rate_limit's
    docstring for the full reasoning."""

    def test_read_defaults_are_thirty_per_minute(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            settings = Settings.from_env()
            self.assertEqual(settings.rate_limit_read_max_requests, 30)
            self.assertEqual(settings.rate_limit_read_window_seconds, 60.0)

    def test_action_defaults_are_ten_per_minute(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True):
            settings = Settings.from_env()
            self.assertEqual(settings.rate_limit_action_max_requests, 10)
            self.assertEqual(settings.rate_limit_action_window_seconds, 60.0)

    def test_all_four_knobs_are_configurable(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {
                "SHELFMARK_RATE_LIMIT_READ_MAX_REQUESTS": "5",
                "SHELFMARK_RATE_LIMIT_READ_WINDOW_SECONDS": "30",
                "SHELFMARK_RATE_LIMIT_ACTION_MAX_REQUESTS": "2",
                "SHELFMARK_RATE_LIMIT_ACTION_WINDOW_SECONDS": "120",
            },
            clear=True,
        ):
            settings = Settings.from_env()
            self.assertEqual(settings.rate_limit_read_max_requests, 5)
            self.assertEqual(settings.rate_limit_read_window_seconds, 30.0)
            self.assertEqual(settings.rate_limit_action_max_requests, 2)
            self.assertEqual(settings.rate_limit_action_window_seconds, 120.0)

    def test_a_zero_or_negative_max_requests_is_floored_at_one(self) -> None:
        """A typo'd 0 must not silently build a limiter that refuses every
        single request forever -- floored the same way
        circuit_breaker_failure_threshold is."""
        with mock.patch.dict(
            "os.environ", {"SHELFMARK_RATE_LIMIT_ACTION_MAX_REQUESTS": "0"}, clear=True
        ):
            self.assertEqual(Settings.from_env().rate_limit_action_max_requests, 1)

    def test_malformed_max_requests_is_rejected_not_silently_dropped(self) -> None:
        with mock.patch.dict(
            "os.environ", {"SHELFMARK_RATE_LIMIT_READ_MAX_REQUESTS": "not-a-number"}, clear=True
        ):
            with self.assertRaises(ValueError):
                Settings.from_env()


if __name__ == "__main__":
    unittest.main()

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


if __name__ == "__main__":
    unittest.main()

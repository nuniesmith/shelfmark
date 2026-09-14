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


if __name__ == "__main__":
    unittest.main()

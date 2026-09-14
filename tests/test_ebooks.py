from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from src.shelfmark_service.ebooks import EbookNotFound, list_ebooks, resolve_ebook


def _touch(path: Path, content: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


class ListEbooksTests(unittest.TestCase):
    """SHELFMARK_EBOOKS_ROOT is empty in production; these fixtures stand in."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        # Organized layout: same book, two formats — must collapse to one result.
        _touch(self.root / "Author One" / "2020 - Book Alpha" / "Book Alpha.epub")
        _touch(self.root / "Author One" / "2020 - Book Alpha" / "Book Alpha.pdf")
        # A different book by a different author.
        _touch(self.root / "Author Two" / "2019 - Book Beta" / "Book Beta.mobi")
        # Two unrelated loose files dropped straight in the root.
        _touch(self.root / "loose-one.epub")
        _touch(self.root / "loose-two.epub")
        # Must never show up as a result.
        _touch(self.root / "Author One" / "2020 - Book Alpha" / "cover.jpg")
        _touch(self.root / "notes.txt")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_multiple_formats_of_one_book_collapse_to_one_result(self) -> None:
        results = list_ebooks(self.root, "Book Alpha")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].ext, ".epub")  # EBOOK_PREF ranks epub above pdf

    def test_loose_files_with_different_stems_are_not_merged(self) -> None:
        """Grouping by folder alone would hide one of these from every search.

        Both files share a parent directory (the root itself) but have
        different stems, so they are different books and must both appear.
        """
        results = list_ebooks(self.root, "loose")
        titles = {r.title for r in results}
        self.assertEqual(titles, {"loose-one", "loose-two"})

    def test_search_matches_on_title_or_author(self) -> None:
        results = list_ebooks(self.root, "beta")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].author, "Author Two")

        results = list_ebooks(self.root, "Author Two")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].title, "2019 - Book Beta")

    def test_non_ebook_files_are_never_returned(self) -> None:
        results = list_ebooks(self.root, "")
        relpaths = {r.relpath for r in results}
        self.assertFalse(any(p.endswith((".jpg", ".txt")) for p in relpaths))

    def test_empty_root_returns_no_results_rather_than_raising(self) -> None:
        empty = Path(self._tmp.name) / "does-not-exist"
        self.assertEqual(list_ebooks(empty, ""), [])

    def test_ids_are_opaque_not_the_raw_path(self) -> None:
        results = list_ebooks(self.root, "beta")
        ebook_id = results[0].id
        self.assertNotIn("Beta", ebook_id)
        self.assertNotIn("/", ebook_id)


class ResolveEbookSecurityTests(unittest.TestCase):
    """The download path takes an id from a Discord user and returns bytes.

    A path traversal here would read arbitrary files off the server, so this
    proves the two obvious attack shapes are refused, not just untested.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name) / "ebooks"
        _touch(self.root / "Author One" / "2020 - Book Alpha" / "Book Alpha.epub")
        self.secret = Path(self._tmp.name) / "secret.txt"
        self.secret.write_text("do not serve me")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_valid_id_resolves_to_the_real_file(self) -> None:
        [entry] = list_ebooks(self.root, "")
        resolved = resolve_ebook(self.root, entry.id)
        self.assertEqual(resolved, (self.root / "Author One" / "2020 - Book Alpha" / "Book Alpha.epub").resolve())

    def test_relative_traversal_id_is_refused(self) -> None:
        traversal = os.path.relpath(self.secret, self.root)
        with self.assertRaises(EbookNotFound):
            resolve_ebook(self.root, traversal)

    def test_absolute_path_id_is_refused(self) -> None:
        with self.assertRaises(EbookNotFound):
            resolve_ebook(self.root, str(self.secret))

    def test_unknown_id_is_refused(self) -> None:
        with self.assertRaises(EbookNotFound):
            resolve_ebook(self.root, "0" * 24)

    def test_symlink_escaping_the_root_is_refused(self) -> None:
        """Hashing the id closes off path-joining, but a symlink is still on disk.

        _iter_ebook_files finds this symlink by its OWN name ("escape.epub"),
        so it gets a normal-looking id. resolve_ebook must still refuse it
        once `.resolve()` shows the real target sits outside the root.
        """
        link = self.root / "Author One" / "2020 - Book Alpha" / "escape.epub"
        os.symlink(self.secret, link)
        escaping_id = [e for e in list_ebooks(self.root, "escape")][0].id
        with self.assertRaises(EbookNotFound):
            resolve_ebook(self.root, escaping_id)


if __name__ == "__main__":
    unittest.main()

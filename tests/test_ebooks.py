from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from src.shelfmark_service.ebooks import (
    EbookNotFound,
    _ebook_id,
    list_ebooks,
    resolve_ebook,
)


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
        # Derive the id directly rather than from list_ebooks. The listing now
        # filters escaping symlinks out, so taking the id from there would
        # make this test silently stop exercising the resolver at all — it
        # would pass because there was nothing to resolve, not because the
        # resolver refused. This guard has to hold on its own.
        escaping_id = _ebook_id(self.root, link)
        with self.assertRaises(EbookNotFound):
            resolve_ebook(self.root, escaping_id)



class ListingAndResolverAgreeTests(unittest.TestCase):
    """What is offered and what can be fetched must be the same set.

    They were not. A symlink inside the library pointing outside it was
    INCLUDED in search results and then REFUSED at download — the reader was
    shown a book, tapped Send, and got a failure for something the bot had
    just told her it had. Nothing unsafe ever left the box; the resolver held.
    But a dead result nobody can explain is its own kind of broken, and the
    fix is that the two can no longer disagree.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="shelfmark-agree-")
        self.root = Path(self.tmp.name) / "ebooks"
        (self.root / "Real Author").mkdir(parents=True)
        (self.root / "Real Author" / "A Real Book.epub").write_bytes(b"EPUB")

        outside = Path(self.tmp.name) / "elsewhere"
        outside.mkdir()
        self.secret = outside / "secret.epub"
        self.secret.write_bytes(b"TOP SECRET")
        (self.root / "Real Author" / "sneaky.epub").symlink_to(self.secret)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_an_escaping_symlink_is_not_offered_at_all(self) -> None:
        titles = [book.title for book in list_ebooks(self.root, "")]
        self.assertEqual(titles, ["A Real Book"])
        self.assertNotIn("sneaky", " ".join(titles).casefold())

    def test_everything_offered_can_actually_be_fetched(self) -> None:
        """The property that was violated, stated directly."""
        for book in list_ebooks(self.root, ""):
            with self.subTest(book=book.title):
                self.assertEqual(resolve_ebook(self.root, book.id).read_bytes(), b"EPUB")

    def test_a_non_id_is_refused_however_it_is_shaped(self) -> None:
        """Note what this does NOT prove.

        It exercises the opaque-id layer — none of these strings hash to a
        real file, so none match. It does not reach resolve_ebook's own
        containment check, which _iter_ebook_files now filters ahead of, and
        which consequently no test can discriminate. That branch is a
        deliberate backstop, not covered code; see the comment on it.
        """
        for candidate in ("sneaky.epub", str(self.secret), "../elsewhere/secret.epub"):
            with self.subTest(candidate=candidate):
                with self.assertRaises(EbookNotFound):
                    resolve_ebook(self.root, candidate)

if __name__ == "__main__":
    unittest.main()

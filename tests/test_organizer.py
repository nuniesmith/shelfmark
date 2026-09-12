from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from src.main import apply_plan, build_plan, main


class OrganizerSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="shelfmark-test-")
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def touch(self, path: Path, content: bytes = b"test") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def plan(self, source: Path, dest: Path | None = None, **kwargs):
        media_mode = kwargs.pop("media_mode", "both")
        return build_plan(
            source=source,
            dest=dest or self.root / "library",
            trash=source / "trash",
            folder_format="year-title",
            keep_names=False,
            include_non_cover_images=False,
            media_mode=media_mode,
            **kwargs,
        )

    def test_mixed_audio_and_ebook_are_separate_books(self) -> None:
        source = self.root / "mixed"
        book = source / "Some Author - The Book (2001)"
        self.touch(book / "01.mp3", b"audio")
        self.touch(book / "The Book.epub", b"ebook")

        result = self.plan(source)

        self.assertEqual(len(result.books), 2)
        kinds = [{op.kind for op in item.tracks} for item in result.books]
        self.assertIn({"track"}, kinds)
        self.assertIn({"ebook"}, kinds)
        self.assertFalse(any(op.kind == "ebook" and op.src.suffix == ".mp3" for item in result.books for op in item.tracks))

    def test_audiobookshelf_sidecars_are_preserved(self) -> None:
        source = self.root / "managed"
        book = source / "Some Author - The Book (2001)"
        self.touch(book / "01.mp3")
        self.touch(book / "metadata.json", b"{}")
        self.touch(book / "metadata.opf", b"<package />")
        self.touch(book / "metadata.db", b"db")

        result = self.plan(source, media_mode="audio")

        extras = [op for item in result.books for op in item.extras]
        self.assertEqual({op.kind for op in extras}, {"metadata"})
        self.assertEqual({op.src.name for op in extras}, {"metadata.json", "metadata.opf", "metadata.db"})
        self.assertFalse(result.trash)

    def test_unknown_sidecar_is_left_for_review_by_default(self) -> None:
        source = self.root / "review"
        book = source / "Some Author - The Book (2001)"
        self.touch(book / "01.mp3")
        self.touch(book / "notes.doc", b"review")

        result = self.plan(source, media_mode="audio")

        self.assertFalse(result.trash)
        self.assertTrue(any("notes.doc" in warning for warning in result.warnings))

        trash_result = self.plan(source, media_mode="audio", trash_unknown=True)
        self.assertTrue(
            any(
                op.src.name == "notes.doc"
                for item in trash_result.books
                for op in item.extras
                if op.kind == "trash"
            )
        )

    def test_multipart_sets_are_isolated_by_parent_directory(self) -> None:
        source = self.root / "multipart"
        for folder in (source / "first", source / "second"):
            self.touch(folder / "Book.part01.rar")
            self.touch(folder / "Book.part02.rar")

        result = self.plan(source)

        self.assertEqual(len(result.extracts), 2)
        self.assertEqual(len(result.trash), 2)
        self.assertEqual({op.src.parent.name for op in result.extracts}, {"first", "second"})

    def test_broken_archive_fails_without_trashing_source(self) -> None:
        source = self.root / "broken"
        archive = source / "broken.zip"
        self.touch(archive, b"not a zip")

        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main([str(source), "--apply", "--yes"])

        self.assertEqual(code, 1)
        self.assertTrue(archive.exists())
        self.assertFalse((source / "trash" / "broken.zip").exists())

    def test_copy_retry_does_not_create_duplicate_file(self) -> None:
        source = self.root / "copy-source"
        dest = self.root / "copy-dest"
        self.touch(source / "Some Author - The Book (2001)" / "01.mp3", b"audio")

        first = self.plan(source, dest, media_mode="audio")
        apply_plan(first, source / "trash", dry_run=False, copy=True)
        second = self.plan(source, dest, media_mode="audio")
        apply_plan(second, source / "trash", dry_run=False, copy=True)

        files = sorted(path.name for path in dest.rglob("*.mp3"))
        self.assertEqual(files, ["01.mp3"])


if __name__ == "__main__":
    unittest.main()

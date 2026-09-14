from __future__ import annotations

import contextlib
import errno
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from src import main as main_module
from src.main import (
    STAGING_DIR_NAME,
    UnsafeArchive,
    apply_plan,
    build_plan,
    copy_file,
    extract_archive,
    is_junk_file,
    main,
    move_file,
)


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


class IsolatedExtractionTests(unittest.TestCase):
    """Extraction stages, validates, then moves into place in one step.

    The property under test is not "extraction works" but "a FAILED extraction
    leaves the source exactly as it found it". Extraction used to write
    straight into the dump beside the archive, and the organiser re-scans the
    dump the moment extracting finishes — so fragments of a half-opened archive
    were picked up and filed as a book, with nothing reported as wrong.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="shelfmark-extract-")
        self.root = Path(self.tmp.name)
        self.source = self.root / "dump"
        self.source.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_zip(self, path: Path, names: dict[str, bytes]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            for name, payload in names.items():
                archive.writestr(name, payload)
        return path

    def visible_entries(self) -> list[str]:
        """What a scan of the source would see — the walker skips dotted
        directories, so staging must not appear here even mid-extraction."""
        return sorted(
            str(p.relative_to(self.source))
            for p in self.source.rglob("*")
            if not any(part.startswith(".") for part in p.relative_to(self.source).parts)
        )

    def test_successful_extraction_leaves_no_staging_behind(self) -> None:
        archive = self.make_zip(self.source / "book.zip", {"01.mp3": b"audio"})

        extracted = extract_archive(archive)

        self.assertEqual(extracted, self.source / "book")
        self.assertEqual((extracted / "01.mp3").read_bytes(), b"audio")
        self.assertFalse((self.source / STAGING_DIR_NAME).exists())

    def test_failure_part_way_leaves_nothing_in_the_source(self) -> None:
        archive = self.make_zip(
            self.source / "book.zip", {"01.mp3": b"audio", "02.mp3": b"audio"}
        )
        quarantine = self.root / "quarantine"

        # Fail AFTER writing a file. That is the case that matters: an
        # extractor that dies before touching the disk was never the problem.
        def half_extract(path: Path, dest: Path) -> None:
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "01.mp3").write_bytes(b"audio")
            raise RuntimeError("disk full")

        with mock.patch.object(main_module, "extract_zip", half_extract):
            with self.assertRaises(RuntimeError):
                extract_archive(archive, quarantine=quarantine)

        # The archive is the only remaining copy of that content. It stays.
        self.assertTrue(archive.exists())
        # And the fragment is NOT in the dump, under any name.
        self.assertEqual(self.visible_entries(), ["book.zip"])
        self.assertFalse((self.source / "book").exists())
        self.assertFalse((self.source / STAGING_DIR_NAME).exists())
        # It is held for inspection instead.
        held = list(quarantine.rglob("01.mp3"))
        self.assertEqual(len(held), 1, f"expected the partial file in quarantine, got {held}")

    def corrupt_zip(self, path: Path) -> Path:
        """A zip whose second member fails its CRC check.

        This is the realistic shape of the failure, and the reason the bug
        mattered. Python writes each member to disk and verifies its CRC
        afterwards, so a corrupt member raises with BOTH files already on
        disk — real, plausible-looking media files, one of them quietly
        damaged. A truncated archive is the gentler case: it usually fails
        while reading the header, before anything is written.
        """
        self.make_zip(
            path,
            {
                "Some Author - The Book (2001)/01.mp3": b"audio" * 2000,
                "Some Author - The Book (2001)/02.mp3": b"audio" * 2000,
            },
        )
        raw = bytearray(path.read_bytes())
        start = raw.rfind(b"audio" * 50)  # inside the second member's data
        for i in range(start, start + 200):
            raw[i] ^= 0xFF
        path.write_bytes(raw)
        return path

    def test_fragments_do_not_survive_into_the_next_run(self) -> None:
        """The end-to-end consequence, through main() rather than the helper.

        A failed extraction is reported and stops the run, so the damage is
        not in that run — it is that the fragments used to STAY in the dump.
        Nothing afterwards knows they came from a broken archive, so the next
        pass over the same dump files them as an ordinary book, one track of
        it silently corrupt.
        """
        archive_path = self.corrupt_zip(self.source / "book.zip")
        library = self.root / "library"

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main([str(self.source), "--dest", str(library), "--apply", "--yes"])

        self.assertEqual(code, 1, stderr.getvalue())
        self.assertTrue(archive_path.exists(), "a failed extract must not trash the archive")
        self.assertEqual(self.visible_entries(), ["book.zip"])

        # The operator clears the download they now know is broken, and runs
        # again. There must be nothing left for that run to find.
        archive_path.unlink()
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            main([str(self.source), "--dest", str(library), "--apply", "--yes"])

        self.assertFalse(
            library.exists() and any(library.rglob("*.mp3")),
            "fragments of a failed extraction were filed into the library",
        )

    def test_escaping_symlink_is_rejected_and_leaves_nothing(self) -> None:
        """An escaping link is caught, and the rejected tree does not survive.

        Stood up through a patched extractor rather than a crafted archive on
        purpose: Python's `zipfile` never creates symlinks — it writes the link
        target as ordinary file content — so a zip cannot exercise this path at
        all. The check exists for unrar/unar/7z, which are handed the archive
        whole and honour links by their own rules, and it runs after extraction
        because that is the only point where every extractor can be held to the
        same rule.
        """
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.txt").write_bytes(b"secret")

        archive = self.make_zip(self.source / "evil.zip", {"placeholder": b""})

        def extract_with_escaping_link(path: Path, dest: Path) -> None:
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "01.mp3").write_bytes(b"audio")
            (dest / "link").symlink_to(outside)

        with mock.patch.object(main_module, "extract_zip", extract_with_escaping_link):
            with self.assertRaises(UnsafeArchive):
                extract_archive(archive)

        self.assertEqual(self.visible_entries(), ["evil.zip"])
        self.assertFalse((self.source / STAGING_DIR_NAME).exists())
        self.assertTrue((outside / "secret.txt").exists())


class AtomicWriteTests(unittest.TestCase):
    """A file in the library is complete or absent, never half-written.

    `shutil.copy2` writes straight to the final path. An interrupted copy left
    a truncated file wearing the real name — indistinguishable downstream from
    a good one — and the retry then wrote the good copy beside it as
    `01 (2).mp3` rather than replacing it.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="shelfmark-atomic-")
        self.root = Path(self.tmp.name)
        self.src = self.root / "src" / "01.mp3"
        self.src.parent.mkdir(parents=True)
        self.payload = b"audio" * 5000
        self.src.write_bytes(self.payload)
        self.dest = self.root / "library" / "Author" / "2001 - Book" / "01.mp3"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @staticmethod
    def truncating_copy2(src: str, dst: str, **kwargs: object) -> None:
        """Write half the bytes, then die — a full disk, or a killed process."""
        data = Path(src).read_bytes()
        Path(dst).write_bytes(data[: len(data) // 2])
        raise OSError(errno.ENOSPC, "No space left on device")

    def test_interrupted_copy_leaves_no_file_under_the_real_name(self) -> None:
        with mock.patch.object(main_module.shutil, "copy2", self.truncating_copy2):
            with self.assertRaises(OSError):
                copy_file(self.src, self.dest)

        self.assertFalse(self.dest.exists(), "a truncated file was left under the final name")
        # The staging temporary is cleaned up too, not merely renamed away.
        leftovers = list(self.dest.parent.iterdir())
        self.assertEqual(leftovers, [], f"staging debris left behind: {leftovers}")

    def test_retry_after_an_interrupted_copy_yields_exactly_one_good_file(self) -> None:
        with mock.patch.object(main_module.shutil, "copy2", self.truncating_copy2):
            with contextlib.suppress(OSError):
                copy_file(self.src, self.dest)

        copy_file(self.src, self.dest)  # the retry, with a working copy2

        written = sorted(p.name for p in self.dest.parent.iterdir())
        self.assertEqual(written, ["01.mp3"], "the retry duplicated the track")
        self.assertEqual(self.dest.read_bytes(), self.payload)

    def test_partial_leftovers_are_treated_as_junk_not_offered_for_review(self) -> None:
        """If a crash does strand a temporary, it must not look like content."""
        self.dest.parent.mkdir(parents=True)
        stranded = self.dest.parent / f".01.mp3.abc123{main_module.PARTIAL_SUFFIX}"
        stranded.write_bytes(b"half")

        self.assertTrue(is_junk_file(stranded))

    def test_cross_filesystem_move_still_lands_atomically(self) -> None:
        """The EXDEV path — `shutil.move` would copy straight to the final name."""
        real_rename = main_module.os.rename

        def rename_across_devices(src: str, dst: str) -> None:
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        with mock.patch.object(main_module.os, "rename", rename_across_devices):
            with mock.patch.object(main_module.shutil, "copy2", self.truncating_copy2):
                with self.assertRaises(OSError):
                    move_file(self.src, self.dest)
            # Nothing under the real name, and the source is still there: a
            # failed move must not consume the only copy.
            self.assertFalse(self.dest.exists())
            self.assertTrue(self.src.exists())

            move_file(self.src, self.dest)  # retry with a working copy2

        self.assertEqual(main_module.os.rename, real_rename)
        self.assertEqual(self.dest.read_bytes(), self.payload)
        self.assertFalse(self.src.exists(), "a completed move must remove the source")


if __name__ == "__main__":
    unittest.main()

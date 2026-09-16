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
    BOOK_STAGING_DIR_NAME,
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

    def test_scene_release_junk_is_trashed_not_flagged(self) -> None:
        """file_id.diz and .nfo ride along with every scene release. Left
        unclassified they became "Unassigned sidecar (skipped)" warnings on
        every single real import, training the operator to stop reading
        them. Both must be trashed, silently, like the rest of the release
        clutter (.sfv, .nzb, ...) already is.
        """
        source = self.root / "scene"
        book = source / "Some Author - The Book (2001)"
        self.touch(book / "01.mp3")
        self.touch(book / "file_id.diz", b"ascii art")
        self.touch(book / "some-group.nfo", b"release notes")

        result = self.plan(source, media_mode="audio")

        self.assertFalse(result.warnings, result.warnings)
        trashed = {op.src.name for item in result.books for op in item.extras if op.kind == "trash"}
        self.assertEqual(trashed, {"file_id.diz", "some-group.nfo"})

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

    def test_existing_book_with_different_content_is_quarantined_not_merged(self) -> None:
        """The Frankenstein defect: a test book named "Mary Shelley -
        Frankenstein (1818)" organised into a library that already held
        "Mary Shelley/1818 - Frankenstein/" with tracks 01.mp3-09.mp3. Both
        parsed to the same dest_dir and the same track numbering, so the
        organiser wrote straight in; `unique_file` stopped an overwrite but
        the library was left holding two overlapping track sets with
        nothing reported. The incoming copy must be quarantined instead,
        and the existing book must not be touched at all.
        """
        dest = self.root / "library"
        existing = dest / "Mary Shelley" / "1818 - Frankenstein"
        for i in range(1, 10):
            self.touch(existing / f"{i:02d}.mp3", f"original track {i}".encode())

        source = self.root / "dump"
        self.touch(
            source / "Mary Shelley - Frankenstein (1818)" / "01.mp3",
            b"a completely different rip - track 1",
        )
        self.touch(
            source / "Mary Shelley - Frankenstein (1818)" / "02.mp3",
            b"a completely different rip - track 2",
        )

        plan = self.plan(source, dest, media_mode="audio")

        self.assertEqual(plan.books, [], "a colliding book must not be planned as a normal write")
        self.assertEqual(len(plan.collisions), 1)
        self.assertTrue(
            any("Mary Shelley" in w and "Frankenstein" in w for w in plan.warnings),
            plan.warnings,
        )

        quarantine = source / ".shelfmark-quarantine"
        apply_plan(plan, trash=source / "trash", dry_run=False, copy=False, quarantine=quarantine)

        # The existing book is completely untouched: still nine tracks, and
        # track 1 still holds the ORIGINAL bytes, not the incoming ones.
        self.assertEqual(
            sorted(p.name for p in existing.glob("*.mp3")),
            [f"{i:02d}.mp3" for i in range(1, 10)],
        )
        self.assertEqual((existing / "01.mp3").read_bytes(), b"original track 1")
        self.assertFalse(
            any((existing / name).exists() for name in ("01 (2).mp3", "02 (2).mp3")),
            "unique_file's renamed duplicates must never have been written at all",
        )

        # The incoming copy landed under quarantine, not in the library.
        quarantined = list(quarantine.rglob("*.mp3"))
        self.assertEqual(len(quarantined), 2)
        self.assertEqual(
            {p.read_bytes() for p in quarantined},
            {b"a completely different rip - track 1", b"a completely different rip - track 2"},
        )

    def test_quarantined_collision_leaves_no_empty_folder_in_the_dump(self) -> None:
        """A quarantined collision still has to be swept from the dump like
        any other consumed book. `remove_empty_dirs` only prunes a directory
        `dirs_the_plan_empties` names, and that helper originally listed only
        `plan.books` — a collision's source folder was left behind, empty,
        in the dump after every one of its files had already been moved to
        quarantine.
        """
        dest = self.root / "library"
        existing = dest / "Mary Shelley" / "1818 - Frankenstein"
        self.touch(existing / "01.mp3", b"original")

        source = self.root / "dump"
        book_dir = source / "Mary Shelley - Frankenstein (1818)"
        self.touch(book_dir / "01.mp3", b"a different rip")

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main([str(source), "--dest", str(dest), "--apply", "--yes"])

        self.assertEqual(code, 0)
        self.assertFalse(book_dir.exists(), "the emptied source folder was left behind")

    def test_genuinely_new_tracks_still_merge_into_an_existing_book(self) -> None:
        """The other half of the same line: a book already on disk can
        legitimately receive more tracks later (a supplementary download, a
        sync re-delivering new chapters). When the incoming file names do
        not collide with anything already at dest_dir, this is an addition,
        not a collision, and must land in the existing folder untouched.
        """
        dest = self.root / "library"
        existing = dest / "Some Author" / "2001 - The Book"
        self.touch(existing / "01.mp3", b"track one")
        self.touch(existing / "02.mp3", b"track two")

        source = self.root / "dump"
        # keep_names so the freshly-scanned file does not get renumbered
        # back down to "01.mp3" and collide with what's already there.
        self.touch(source / "Some Author - The Book (2001)" / "03 - Bonus Chapter.mp3", b"bonus")

        plan = build_plan(
            source=source,
            dest=dest,
            trash=source / "trash",
            folder_format="year-title",
            keep_names=True,
            include_non_cover_images=False,
            media_mode="audio",
        )

        self.assertEqual(plan.collisions, [])
        self.assertEqual(plan.warnings, [])

        apply_plan(plan, trash=source / "trash", dry_run=False, copy=False)

        self.assertEqual((existing / "01.mp3").read_bytes(), b"track one")
        self.assertEqual((existing / "02.mp3").read_bytes(), b"track two")
        self.assertTrue(any(existing.glob("*Bonus Chapter.mp3")), list(existing.iterdir()))

    def test_KNOWN_GAP_keep_names_lets_a_different_copy_merge_silently(self) -> None:
        """colliding_tracks only catches a collision when the INCOMING file
        names happen to match names already at dest_dir. Default renumbering
        (01.mp3, 02.mp3, ...) makes that reliable, but `--keep-names` keeps
        whatever the source called its files -- so a genuinely different
        copy of the same book, ripped/named differently upstream, produces
        disjoint filenames and is not detected at all. Both copies land in
        the same folder, interleaved, with no warning.

        This is a documented, accepted gap (see README's "What happens when
        the destination already holds a different book" and todo.md), not a
        regression to silently tolerate: it asserts CURRENT behaviour so a
        future fix for --keep-names has something concrete to flip red.
        The automatic/service pipeline never passes --keep-names, so this
        does not affect the unattended path.
        """
        dest = self.root / "library"
        existing = dest / "Some Author" / "2001 - The Book"
        self.touch(existing / "01 - Part A.mp3", b"original part A")
        self.touch(existing / "02 - Part B.mp3", b"original part B")

        source = self.root / "dump"
        book = source / "Some Author - The Book (2001)"
        self.touch(book / "Chapter One.mp3", b"a completely different rip - chapter one")
        self.touch(book / "Chapter Two.mp3", b"a completely different rip - chapter two")

        plan = build_plan(
            source=source,
            dest=dest,
            trash=source / "trash",
            folder_format="year-title",
            keep_names=True,
            include_non_cover_images=False,
            media_mode="audio",
        )

        # Not detected: this is the gap, not the fix.
        self.assertEqual(plan.collisions, [])
        self.assertEqual(plan.warnings, [])

        apply_plan(plan, trash=source / "trash", dry_run=False, copy=False)

        # Both copies now sit in the same folder, interleaved, silently.
        # --keep-names still prefixes with a LOCAL index ("01 - ", "02 - ",
        # renumbered from 1 within this incoming set alone), so the full
        # names happen not to collide even though both are "track one" of
        # their respective copies.
        self.assertEqual(
            sorted(p.name for p in existing.glob("*.mp3")),
            ["01 - Chapter One.mp3", "01 - Part A.mp3", "02 - Chapter Two.mp3", "02 - Part B.mp3"],
        )
        self.assertEqual((existing / "01 - Part A.mp3").read_bytes(), b"original part A")
        self.assertEqual(
            (existing / "01 - Chapter One.mp3").read_bytes(),
            b"a completely different rip - chapter one",
        )


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


class SceneReleaseMetadataTests(unittest.TestCase):
    """A scene release is a well-named folder holding an obfuscated archive:

        Brenda.Peynado.-.The.Rock.Eaters.2021.RETAIL.EPUB.eBook-CTO/
            tr8e3el.rar
            tr8e3el.nfo
            file_id.diz

    extract_archive() unpacks tr8e3el.rar into a sibling "tr8e3el/" folder
    (named after the ARCHIVE, see extract_dir_for), and the re-scan after
    extraction used to read metadata from THAT name: "Unknown Author /
    tr8e3el", every download filed as garbage. The release folder — the only
    place author, title and year actually exist — was one level up and never
    consulted. Every real download looks like this.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="shelfmark-scene-")
        self.root = Path(self.tmp.name)
        self.source = self.root / "downloads"
        self.source.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def make_zip(self, path: Path, names: dict[str, bytes]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as archive:
            for name, payload in names.items():
                archive.writestr(name, payload)
        return path

    def test_metadata_comes_from_release_folder_not_archive_stem(self) -> None:
        release = (
            self.source
            / "Brenda.Peynado.-.The.Rock.Eaters.2021.RETAIL.EPUB.eBook-CTO"
        )
        # The archive's inner filename is the obfuscated one — the whole
        # point of the bug. Its own name carries no author, title or year.
        # A real release ships this as a .rar; zip exercises the identical
        # code path (extract_dir_for names the folder from the archive stem
        # regardless of archive kind) without needing an external unrar.
        self.make_zip(release / "tr8e3el.zip", {"tr8e3el.epub": b"epub-bytes"})
        (release / "tr8e3el.nfo").write_bytes(b"release info")
        (release / "file_id.diz").write_bytes(b"diz")

        library = self.root / "library"
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main([str(self.source), "--dest", str(library), "--apply", "--yes"])

        self.assertEqual(code, 0, stderr.getvalue())
        filed = sorted(library.rglob("*.epub"))
        self.assertEqual(len(filed), 1, filed)
        self.assertEqual(
            filed[0],
            library / "Brenda Peynado" / "2021 - The Rock Eaters" / "The Rock Eaters.epub",
        )

    def test_loose_ebook_title_has_no_dangling_extension_dot(self) -> None:
        """A loose ebook file's own name runs through parse_name whole.

        parse_name only strips a recognized AUDIO/ARCHIVE suffix up front;
        ".epub" is neither, so it rides along as literal text. humanize()
        only collapses dots when there are two or more (a real initial's
        lone dot, "A. E.", must survive), so this single extension dot is
        never touched there either — it only vanishes once strip_quality
        removes the "epub" word sitting after it. Adding "epub" to
        strip_quality's format-token list (the scene-release fix above) was
        correct, but the cleanup that followed left the newly-orphaned dot
        behind: title="Title ." instead of "Title".
        """
        source = self.root / "loose"
        source.mkdir(parents=True)
        (source / "Author Name - Title (1999).epub").write_bytes(b"epub-bytes")

        plan = build_plan(
            source=source,
            dest=self.root / "loose-library",
            trash=source / "trash",
            folder_format="year-title",
            keep_names=False,
            include_non_cover_images=False,
            media_mode="ebook",
        )

        self.assertEqual(len(plan.books), 1, plan.books)
        meta = plan.books[0].meta
        self.assertEqual(meta.author, "Author Name")
        self.assertEqual(meta.title, "Title")
        self.assertEqual(meta.year, "1999")


class MultiBookCollectionMetadataTests(unittest.TestCase):
    """A multi-book collection where the per-book FOLDER carries title + year
    and the FILENAME carries the author — a real Frank Herbert Dune pack:

        Dune Saga - Frank Herbert Collection/
            Chapterhouse Dune (1985)/Chapterhouse Dune - Frank Herbert.epub
            Children of Dune (1976)/Children of Dune - Frank Herbert.epub
            Dune (1965)/Dune - Frank Herbert.epub
            Dune Messiah (1969)/Dune Messiah - Frank Herbert.epub
            God Emperor of Dune (1981)/God Emperor of Dune - Frank Herbert.epub
            Heretics of Dune (1984)/Heretics of Dune - Frank Herbert.epub

    Each folder alone has no author (parse_name reads "Unknown Author" plus
    the title), and each filename alone has both parts right ("Frank
    Herbert" / the same title). Combining them used to produce a THIRD,
    wrong answer that depended on whether the title happened to look like a
    two-word person's name:

      * "Chapterhouse Dune" and "Dune Messiah" (two capitalised words) pass
        looks_like_person, so enrich_meta's "Author - Title" guess fired on
        the filename and swapped it: author became the TITLE
        ("Chapterhouse Dune") and the real author ("Frank Herbert") was
        filed as the title. This is the worst outcome — confidently wrong,
        not merely incomplete.
      * "Children of Dune" and "Dune" do not look like a person (the first
        has "of", a TITLE_STOP word; the second is a single token), so
        nothing filled the author at all and the book shipped under
        "Unknown Author" with the right title.

    All six must land as author="Frank Herbert" with the folder's own title
    and year untouched, regardless of which of the two failure shapes their
    title would otherwise have hit.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="shelfmark-dune-")
        self.root = Path(self.tmp.name)
        self.source = self.root / "incoming"
        self.source.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_folder_title_plus_filename_author_never_swaps(self) -> None:
        collection = self.source / "Dune Saga - Frank Herbert Collection"
        books = {
            "Dune (1965)": ("Dune", "1965"),
            "Dune Messiah (1969)": ("Dune Messiah", "1969"),
            "Children of Dune (1976)": ("Children of Dune", "1976"),
            "God Emperor of Dune (1981)": ("God Emperor of Dune", "1981"),
            "Heretics of Dune (1984)": ("Heretics of Dune", "1984"),
            "Chapterhouse Dune (1985)": ("Chapterhouse Dune", "1985"),
        }
        for folder, (title, _year) in books.items():
            path = collection / folder / f"{title} - Frank Herbert.epub"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"epub-bytes")

        plan = build_plan(
            source=self.source,
            dest=self.root / "library",
            trash=self.source / "trash",
            folder_format="year-title",
            keep_names=False,
            include_non_cover_images=False,
            media_mode="ebook",
        )

        self.assertEqual(len(plan.books), 6, [b.meta for b in plan.books])
        got = {b.meta.title: (b.meta.author, b.meta.year) for b in plan.books}
        for title, year in books.values():
            self.assertIn(title, got)
            self.assertEqual(got[title], ("Frank Herbert", year), title)
        # The swap's fingerprint: the title must never end up as the
        # author, however person-like a two-word title reads.
        for b in plan.books:
            self.assertNotEqual(b.meta.author, b.meta.title)
        self.assertNotIn("Chapterhouse Dune", {b.meta.author for b in plan.books})
        self.assertNotIn("Dune Messiah", {b.meta.author for b in plan.books})


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


class WholeDirectoryStagingTests(unittest.TestCase):
    """A book folder appears in the library whole, or not at all.

    apply_plan used to move each track into dest_dir one at a time, so an
    import killed partway — out of disk, a bad track, an operator's Ctrl-C —
    left a book folder holding some tracks with the rest simply missing.
    Nothing downstream can tell that from a short book; Audiobookshelf
    imports it as a real one.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="shelfmark-stage-")
        self.root = Path(self.tmp.name)
        self.source = self.root / "dump"
        self.dest = self.root / "library"
        self.trash = self.source / "trash"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def touch(self, path: Path, content: bytes = b"x") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def make_plan(self):
        return build_plan(
            source=self.source,
            dest=self.dest,
            trash=self.trash,
            folder_format="year-title",
            keep_names=False,
            include_non_cover_images=False,
            media_mode="audio",
        )

    def test_failure_partway_leaves_no_partial_book_folder(self) -> None:
        """The property under test: a killed import leaves NOTHING under the
        book's name, and the source tracks it had not yet consumed are still
        there — a plain move would already have removed them.

        Mocks os.rename, not shutil.copy2: a same-filesystem move mode import
        stages with a rename now (see _stage_book), so that is where a real
        failure — a disk actually going away mid-import — would surface.
        """
        book = self.source / "Some Author - The Book (2001)"
        self.touch(book / "01.mp3", b"one")
        self.touch(book / "02.mp3", b"two")
        self.touch(book / "03.mp3", b"three")
        plan = self.make_plan()
        self.assertEqual(len(plan.books), 1)
        dest_dir = plan.books[0].dest_dir

        real_rename = main_module.os.rename
        calls = {"n": 0}

        def flaky_rename(src, dst):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_rename(src, dst)

        with mock.patch.object(main_module.os, "rename", flaky_rename):
            with self.assertRaises(OSError):
                apply_plan(plan, trash=self.trash, dry_run=False, copy=False)

        self.assertFalse(dest_dir.exists(), "a partial book folder was left in the library")
        self.assertFalse(list(self.dest.rglob("*.mp3")), "a stray track escaped into the library")
        self.assertFalse(
            list(self.dest.rglob(BOOK_STAGING_DIR_NAME)), "staging debris left behind"
        )
        # All three tracks are still at the source — the first track's
        # rename into staging is rolled back (reversed), and the second and
        # third were never touched at all.
        self.assertEqual(
            sorted(p.name for p in book.glob("*.mp3")), ["01.mp3", "02.mp3", "03.mp3"]
        )

    def test_staging_directory_is_beside_the_destination(self) -> None:
        """`rename` is atomic only within one filesystem, so staging has to
        be a sibling of dest_dir — not some other configured root."""
        book = self.source / "Some Author - The Book (2001)"
        self.touch(book / "01.mp3", b"one")
        plan = self.make_plan()
        dest_dir = plan.books[0].dest_dir
        real_rename = main_module.os.rename
        seen: dict[str, Path] = {}

        def capturing_rename(src, dst):
            # The FIRST rename is the per-track one, into staging; the
            # second is the whole-directory swap at the very end, which
            # lands at dest_dir itself and would otherwise overwrite this.
            if "dst" not in seen:
                seen["dst"] = Path(dst)
            return real_rename(src, dst)

        with mock.patch.object(main_module.os, "rename", capturing_rename):
            apply_plan(plan, trash=self.trash, dry_run=False, copy=False)

        self.assertIn("dst", seen)
        self.assertEqual(seen["dst"].parent.parent, dest_dir.parent / BOOK_STAGING_DIR_NAME)
        # And cleared away once the book has landed.
        self.assertFalse((dest_dir.parent / BOOK_STAGING_DIR_NAME).exists())

    def test_same_filesystem_move_renames_rather_than_copies(self) -> None:
        """The property that actually distinguishes a rename from a copy: the
        inode is preserved. A copy always allocates a new one, however
        byte-for-byte identical the content looks afterward — and
        reorganising an existing library in place (--dest equal to source)
        is metadata-only work today that must not turn into a full
        read-and-rewrite of it.
        """
        book = self.source / "Some Author - The Book (2001)"
        self.touch(book / "01.mp3", b"one")
        plan = self.make_plan()
        dest_dir = plan.books[0].dest_dir
        src_path = plan.books[0].tracks[0].src
        inode_before = src_path.stat().st_ino

        apply_plan(plan, trash=self.trash, dry_run=False, copy=False)

        dest_path = dest_dir / "01.mp3"
        self.assertTrue(dest_path.exists())
        self.assertEqual(
            dest_path.stat().st_ino,
            inode_before,
            "same-filesystem move copied the file's data instead of renaming it",
        )

    def test_failed_rollback_leaves_the_file_recoverable_not_deleted(self) -> None:
        """If reversing a rename ALSO fails, the original location no longer
        has that track — the forward rename already removed it — so deleting
        the staged copy too would destroy the only one left. The staging
        directory must survive instead, with the file still inside it.
        """
        book = self.source / "Some Author - The Book (2001)"
        self.touch(book / "01.mp3", b"one")
        self.touch(book / "02.mp3", b"two")
        plan = self.make_plan()

        real_rename = main_module.os.rename
        calls = {"n": 0}

        def flaky_rename(src, dst):
            calls["n"] += 1
            if calls["n"] == 1:
                return real_rename(src, dst)  # 01.mp3 stages fine
            if calls["n"] == 2:
                raise OSError(errno.ENOSPC, "No space left on device")  # 02.mp3 fails
            raise OSError(errno.EACCES, "Permission denied")  # rolling 01.mp3 back ALSO fails

        with mock.patch.object(main_module.os, "rename", flaky_rename):
            with self.assertRaises(OSError):
                apply_plan(plan, trash=self.trash, dry_run=False, copy=False)

        staging_roots = list(self.dest.rglob(BOOK_STAGING_DIR_NAME))
        self.assertEqual(
            len(staging_roots), 1, "the staging directory was removed despite a failed rollback"
        )
        stranded = list(staging_roots[0].rglob("01.mp3"))
        self.assertEqual(
            len(stranded), 1, "the only remaining copy of the un-rolled-back track was deleted"
        )
        self.assertEqual(stranded[0].read_bytes(), b"one")

    def test_move_retry_after_successful_import_does_not_duplicate(self) -> None:
        """The idempotent-retry guarantee, exercised in --move mode (the
        existing coverage for this is copy-only) across a multi-track book,
        after the first run created dest_dir via staging."""
        book = self.source / "Some Author - The Book (2001)"
        self.touch(book / "01.mp3", b"one")
        self.touch(book / "02.mp3", b"two")
        first = self.make_plan()
        apply_plan(first, trash=self.trash, dry_run=False, copy=False)

        # As if the release arrived again — the operator re-downloaded it,
        # or a sync re-delivered files already imported.
        self.touch(book / "01.mp3", b"one")
        self.touch(book / "02.mp3", b"two")
        second = self.make_plan()
        apply_plan(second, trash=self.trash, dry_run=False, copy=False)

        files = sorted(p.name for p in self.dest.rglob("*.mp3"))
        self.assertEqual(files, ["01.mp3", "02.mp3"])

    def test_copy_mode_leaves_source_untouched_for_a_staged_book(self) -> None:
        book = self.source / "Some Author - The Book (2001)"
        self.touch(book / "01.mp3", b"one")
        self.touch(book / "02.mp3", b"two")
        plan = self.make_plan()
        dest_dir = plan.books[0].dest_dir

        apply_plan(plan, trash=self.trash, dry_run=False, copy=True)

        self.assertEqual(
            sorted(p.name for p in dest_dir.glob("*.mp3")), ["01.mp3", "02.mp3"]
        )
        self.assertEqual(
            sorted(p.name for p in book.glob("*.mp3")), ["01.mp3", "02.mp3"]
        )

    def test_junk_sidecar_is_trashed_for_a_freshly_staged_book(self) -> None:
        """Trash-kind extras (junk, duplicates) do not belong in the book
        folder and must still be routed to trash for a book new enough to go
        through staging, not just for one written the old file-by-file way."""
        book = self.source / "Some Author - The Book (2001)"
        self.touch(book / "01.mp3", b"one")
        self.touch(book / "thumbs.db", b"junk")
        plan = self.make_plan()
        dest_dir = plan.books[0].dest_dir

        apply_plan(plan, trash=self.trash, dry_run=False, copy=False)

        self.assertTrue((self.trash / "thumbs.db").exists())
        self.assertFalse((dest_dir / "thumbs.db").exists())

    def test_stranded_staging_from_an_uncatchable_kill_is_reported_not_silent(self) -> None:
        """SIGKILL, an OOM kill, and a power cut cannot be caught, so
        _stage_book's own rollback (a `try`/`except`) never runs for them: a
        move-mode import killed that way leaves tracks renamed OUT of the
        source and stuck in .shelfmark-work-books, with nothing left to put
        them back.

        Before whole-directory staging, the same kill left some tracks in
        the library and the rest still in the source, where the next run's
        scan would find and finish the book — self-healing. Staging removes
        that: the walker skips a dotted directory, so a scan does not find
        these tracks in the source, the library never got a folder for them,
        and nothing looks for them here either. This test is the substitute
        for the self-healing this PR took away: the operator must at least
        be told the files exist and where, rather than a book quietly
        existing nowhere a human would look.
        """
        self.source.mkdir(parents=True, exist_ok=True)
        dest_dir = self.dest / "Some Author" / "2001 - The Book"
        staging_parent = dest_dir.parent / BOOK_STAGING_DIR_NAME
        staging = staging_parent / "2001 - The Book.abcdef"
        self.touch(staging / "01.mp3", b"one")
        self.touch(staging / "02.mp3", b"two")

        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main([str(self.source), "--dest", str(self.dest), "--apply", "--yes"])

        self.assertEqual(code, 0)
        err = stderr.getvalue()
        self.assertIn(
            "1 book staging directory", err, "the operator was not told anything was stranded"
        )
        self.assertIn(staging.name, err, "the stranded directory's own path was not reported")

        # Not deleted (the only copy of those tracks), and not silently
        # completed into the library either — a staging directory can be
        # partial, and finishing a partial one is exactly the half-a-book
        # this PR exists to prevent.
        self.assertTrue(staging.exists())
        self.assertEqual(
            sorted(p.name for p in staging.glob("*.mp3")), ["01.mp3", "02.mp3"]
        )
        self.assertFalse(dest_dir.exists())


if __name__ == "__main__":
    unittest.main()

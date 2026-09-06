#!/usr/bin/env python3
"""Bindery — organize messy audiobook and ebook dumps.

Audiobook layout (Audiobookshelf-friendly):

    Author Name/
      2021 - Book Title/
        01.mp3
        02.mp3
        cover.jpg

Ebook layout:

    Author Name/
      2021 - Book Title/
        Book Title.epub
        cover.jpg

Media modes (--media):
  auto   — detect per folder (default)
  audio  — only audiobooks
  ebook  — only ebooks

Examples:
    ./run.sh "/path/to/dump" --dry-run
    ./run.sh "/path/to/dump" --dest "/path/to/Audiobooks" --apply
    ./run.sh "/path/to/ebooks" --media ebook --dest "/path/to/Books" --apply
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

AUDIO_EXT = {
    ".mp3", ".m4b", ".m4a", ".flac", ".ogg", ".opus",
    ".aac", ".wma", ".wav", ".mp4", ".m4v",
}
EBOOK_EXT = {
    ".epub", ".mobi", ".azw", ".azw3", ".pdf", ".cbz", ".cbr",
    ".fb2", ".djvu", ".lit",
}
# Prefer these when multiple ebook formats exist for the same title
EBOOK_PREF = [".epub", ".mobi", ".azw3", ".azw", ".pdf", ".cbz", ".cbr", ".fb2"]
ARCHIVE_EXT = {".zip", ".rar", ".7z", ".tar", ".tgz", ".gz"}
COVER_STEMS = {"cover", "folder", "poster", "front", "albumart", "album"}
COVER_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
KEEP_TEXT = {"desc.txt", "reader.txt"}
TRASH_EXT = {
    ".nfo", ".sfv", ".md5", ".url", ".torrent", ".m3u", ".m3u8", ".pls",
    ".html", ".htm", ".log", ".cue", ".txt", ".ini", ".db", ".ds_store",
    ".part", ".crc", ".par2", ".exe", ".nzb",
}
JUNK_NAMES = {
    "thumbs.db", "desktop.ini", ".ds_store",
    "albumartsmall.jpg", "albumart_{small}.jpg",
}
SKIP_DIR_NAMES = {
    "trash", "_trash", ".trash", ".bindery-work", "__macosx",
    ".git", "@eadir", ".spotlight-v100", ".trashes",
}
DISC_RE = re.compile(
    r"^(?:cd|disc|disk|dvd|side)[\s._-]*([0-9]{1,3}|[a-d])(?:\b.*)?$",
    re.I,
)
# Book section / part folders (Christine, Roadwork, multi-part rips)
SECTION_RE = re.compile(
    r"""^(?:
        (?:\d+[\s._-]*)?(?:part|pt|section|book|volume|vol)[\s._-]*[\d IVXLC]+  # Part 1, Part One
        |
        \d+[\s._-]+part\b.*                                                     # 1 Part One Dennis
        |
        (?:january|february|march|april|may|june|july|august|september|october|november|december)\b.*
        |
        teenage\s+(?:car|love|death)\s+songs\b.*                                 # Christine sections
    )$""",
    re.I | re.X,
)
MONTH_ORDER = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}
PART_RE = re.compile(
    r"^(?P<base>.+)\.part(?P<num>0*\d+)\.(?P<ext>rar|zip)$",
    re.I,
)
RVOL_RE = re.compile(r"^(?P<base>.+)\.r(?P<num>\d{2})$", re.I)
WRAPPER_NAMES = {
    "mp3", "m4b", "m4a", "flac", "audio", "audiobook", "audiobooks",
    "book", "files", "media", "tracks", "chapters",
}
STOP_AUTHOR = {
    "cd", "disc", "disk", "downloads", "download", "incoming", "inbox",
    "audiobooks", "audiobook", "books", "torrents", "completed", "complete",
    "dump", "new", "misc", "temp", "tmp", "unsorted", "import", "library",
    "media", "audio", "files",
}
QUALITY_RE = re.compile(
    r"""(?:
        \b(?:unabridged|abridged|audiobooks?|audio\s*book|retail|explicit|
            complete|drm|web-?rip|repack|remux|read\s+by)\b
        |
        \b\d{2,3}\s*kbps\b
        |
        \b(?:64|96|128|192|224|256|320)k\b
        |
        \b(?:mp3|m4b|m4a|flac|opus)\b
        |
        \[\s*(?:unabridged|abridged|audiobook|retail|explicit)\s*\]
    )""",
    re.I | re.X,
)
SCENE_RE = re.compile(
    r"[-_.\s]+(?:IPT|eBook|ebook|NOGRP|V0|VBR|INTERNAL|PROPER|REPACK|READNFO)(?:[-_.\s].*)?$",
    re.I,
)
READ_BY_RE = re.compile(
    r"\b(?:read|narrated)\s+by\s+(.+)$",
    re.I,
)
YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
NARRATOR_RE = re.compile(r"\{([^{}]+)\}")
ASIN_RE = re.compile(r"\[(B0[0-9A-Z]{8})\]", re.I)
BRACKET_RE = re.compile(r"[\[(][^\[\]()]{0,80}[\])]")
PERSON_PARTICLES = {"van", "von", "de", "da", "di", "del", "della", "la", "le", "st", "st."}
TITLE_STOP = {
    "part", "volume", "vol", "book", "chapter", "ch", "the", "a", "an", "of",
    "and", "series", "trilogy", "saga", "omnibus", "collection", "anthology",
    "unabridged", "abridged", "game", "guide", "end", "new", "old",
    "hammer", "war", "road", "tale", "man", "world", "planet", "station",
    "shift", "crew", "stand", "zone", "walk", "half", "things", "lands",
    "summer", "galaxy", "masters", "troopers", "carbon", "space",
}

# Canonical title → (author, year, preferred display title)
# Keys are casefolded, punctuation-stripped for matching.

# Canonical author name aliases (casefolded key → preferred spelling)
AUTHOR_ALIASES: dict[str, str] = {
    "arthur c clark": "Arthur C Clarke",
    "arthur c. clark": "Arthur C Clarke",
    "arthur charles clarke": "Arthur C Clarke",
    "arthur clarke": "Arthur C Clarke",
    "h g wells": "H G Wells",
    "h.g. wells": "H G Wells",
    "h. g. wells": "H G Wells",
    "hg wells": "H G Wells",
    "niven & pournell": "Niven & Pournelle",
    "niven and pournell": "Niven & Pournelle",
    "niven & pournelle": "Niven & Pournelle",
    "niven and pournelle": "Niven & Pournelle",
    "larry niven": "Larry Niven",
    "robert a heinlein": "Robert A Heinlein",
    "robert heinlein": "Robert A Heinlein",
    "philip k dick": "Philip K Dick",
    "philip kindred dick": "Philip K Dick",
    "pkd": "Philip K Dick",
    "orson scott card": "Orson Scott Card",
    "lem stanislaw": "Stanislaw Lem",
    "stanislaw lem": "Stanislaw Lem",
    "madelein l'engle": "Madeleine L'Engle",
    "madeleine lengle": "Madeleine L'Engle",
    "madelein lengle": "Madeleine L'Engle",
    "e e 'doc' smith": "E E 'Doc' Smith",
    "e e doc smith": "E E 'Doc' Smith",
    "ee doc smith": "E E 'Doc' Smith",
    "doc smith": "E E 'Doc' Smith",
    "c s lewis": "C S Lewis",
    "cs lewis": "C S Lewis",
    "philip jose farmer": "Philip Jose Farmer",
    "philip josé farmer": "Philip Jose Farmer",
    "iain m banks": "Iain M Banks",
    "iain banks": "Iain M Banks",
    "ursula k le guin": "Ursula K Le Guin",
    "ursula le guin": "Ursula K Le Guin",
    "le guin": "Ursula K Le Guin",
}


def canonicalize_author(name: str) -> str:
    if not name or name == "Unknown Author":
        return name
    key = re.sub(r"\s+", " ", name.casefold().replace(".", " ").replace("  ", " ")).strip()
    key = re.sub(r"\s+", " ", key)
    if key in AUTHOR_ALIASES:
        return AUTHOR_ALIASES[key]
    # try without middle dots already normalized
    return name


KNOWN_TITLES: dict[str, tuple[str, str | None, str]] = {
    # Stephen King (missing years / wrong author folders)
    "11 22 63": ("Stephen King", "2011", "11 22 63"),
    "11/22/63": ("Stephen King", "2011", "11 22 63"),
    "blockade billy": ("Stephen King", "2010", "Blockade Billy"),
    "in the tall grass": ("Stephen King", "2012", "In the Tall Grass"),
    "morality": ("Stephen King", "2009", "Morality"),
    "mr mercedes": ("Stephen King", "2014", "Mr. Mercedes"),
    "mr. mercedes": ("Stephen King", "2014", "Mr. Mercedes"),
    "revival": ("Stephen King", "2014", "Revival"),
    "the bazaar of bad dreams": ("Stephen King", "2015", "The Bazaar of Bad Dreams"),
    "the gunslinger": ("Stephen King", "1982", "The Gunslinger"),
    "the wind through the keyhole": ("Stephen King", "2012", "The Wind Through The Keyhole"),
    "sleeping beauties": ("Stephen King", "2017", "Sleeping Beauties"),
    "gwendy's magic feather": ("Stephen King", "2020", "Gwendy's Magic Feather"),
    "gwendys magic feather": ("Stephen King", "2020", "Gwendy's Magic Feather"),
    "the green mile": ("Stephen King", "1996", "The Green Mile"),
    "two dead girls": ("Stephen King", "1996", "The Green Mile"),
    "coffey's hands": ("Stephen King", "1996", "The Green Mile"),
    "coffeys hands": ("Stephen King", "1996", "The Green Mile"),
    "the mouse on the mile": ("Stephen King", "1996", "The Green Mile"),
    "coffey on the mile": ("Stephen King", "1996", "The Green Mile"),
    "night journey": ("Stephen King", "1996", "The Green Mile"),
    "the bad death of eduard delacroix": ("Stephen King", "1996", "The Green Mile"),
    "hearts in atlantis": ("Stephen King", "1999", "Hearts in Atlantis"),
    "low men in yellow coats": ("Stephen King", "1999", "Hearts in Atlantis"),
    "yellow coats": ("Stephen King", "1999", "Hearts in Atlantis"),
    # Nonfiction / misc
    "the subtle art of not giving a f ck a counterintuitive approach to living a": (
        "Mark Manson", "2016", "The Subtle Art of Not Giving a F*ck"
    ),
    "you can read anyone": ("David J Lieberman", "2007", "You Can Read Anyone"),
    # Classics often year-only
    "journey to the center of the earth": ("Jules Verne", "1864", "Journey to the Center of the Earth"),
    "twenty thousand leagues under the sea": ("Jules Verne", "1870", "Twenty Thousand Leagues Under the Sea"),
    "20 000 leagues under the sea": ("Jules Verne", "1870", "Twenty Thousand Leagues Under the Sea"),
}


def title_key(name: str) -> str:
    """Normalize a title for KNOWN_TITLES lookup."""
    t = name.casefold()
    t = t.replace("'", "").replace('"', "").replace("`", "")
    t = re.sub(r"[._]+", " ", t)
    t = re.sub(r"[^a-z0-9 /&*-]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


SAMPLE_RE = re.compile(r"(?:^|[\s._-])(?:sample|preview|excerpt)(?:[\s._-]|$)", re.I)
INVALID_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
INDEX_RE = re.compile(r"^\d{1,4}$")


def eprint(*args: object) -> None:
    print(*args, file=sys.stderr)


def natural_key(value: str) -> list:
    return [int(p) if p.isdigit() else p.casefold() for p in re.split(r"(\d+)", value)]


def is_audio(path: Path) -> bool:
    return path.suffix.lower() in AUDIO_EXT and path.is_file()


def is_ebook(path: Path) -> bool:
    return path.suffix.lower() in EBOOK_EXT and path.is_file()


def is_media(path: Path) -> bool:
    return is_audio(path) or is_ebook(path)


def archive_kind(path: Path) -> str | None:
    name = path.name.lower()
    if name.endswith(".tar.gz") or name.endswith(".tgz"):
        return "tar"
    ext = path.suffix.lower()
    if ext in {".zip", ".rar", ".7z", ".tar"}:
        return ext[1:]
    return None


def is_disc_folder(name: str) -> bool:
    cleaned = name.strip()
    if DISC_RE.match(cleaned):
        return True
    return bool(re.match(r"^(?:cd|disc|disk|dvd)[\s._-]*\d+", cleaned, re.I))


def disc_number(name: str) -> int:
    m = re.match(
        r"^(?:cd|disc|disk|dvd|side)[\s._-]*([0-9]{1,3}|[a-d])",
        name.strip(),
        re.I,
    )
    if not m:
        return 0
    token = m.group(1).lower()
    if token.isalpha():
        return ord(token) - 96
    return int(token)


def is_section_folder(name: str) -> bool:
    cleaned = name.strip()
    if SECTION_RE.match(cleaned):
        return True
    # "1 Part One Dennis", "2 Part Two Arnie"
    if re.match(r"^\d+[\s._-]+part\b", cleaned, re.I):
        return True
    if re.match(r"^(?:part|pt|section)[\s._-]+\d+", cleaned, re.I):
        return True
    # standalone month names used as Roadwork-style sections
    if cleaned.casefold().split()[0] in MONTH_ORDER if cleaned else False:
        if len(cleaned.split()) <= 4:
            return True
    return False


def section_number(name: str) -> int:
    cleaned = name.strip()
    m = re.match(r"^(\d+)[\s._-]+part\b", cleaned, re.I)
    if m:
        return int(m.group(1))
    m = re.match(r"^(?:part|pt|section)[\s._-]+(\d+)", cleaned, re.I)
    if m:
        return int(m.group(1))
    m = re.match(r"^(january|february|march|april|may|june|july|august|september|october|november|december)\b", cleaned, re.I)
    if m:
        return MONTH_ORDER[m.group(1).casefold()]
    m = re.search(r"\bpart[\s._-]*(\d+)\b", cleaned, re.I)
    if m:
        return int(m.group(1))
    return 0


def is_non_author_folder(name: str) -> bool:
    n = name.strip()
    if INDEX_RE.match(n):
        return True
    if n.casefold() in STOP_AUTHOR:
        return True
    if re.fullmatch(r"(?:book|vol|volume|#)?\s*\d{1,4}", n, re.I):
        return True
    return False


def is_cover(path: Path) -> bool:
    if path.suffix.lower() not in COVER_EXT:
        return False
    stem = re.sub(r"[\s._-]+", "", path.stem.lower())
    return any(stem == c or stem.startswith(c) for c in COVER_STEMS)


def is_junk_file(path: Path) -> bool:
    name = path.name.lower()
    if name in JUNK_NAMES or name.startswith("._"):
        return True
    if name in KEEP_TEXT:
        return False
    if is_cover(path):
        return False
    if path.suffix.lower() in TRASH_EXT:
        return True
    if is_audio(path) and SAMPLE_RE.search(path.stem):
        return True
    return False


def humanize(name: str) -> str:
    stem = re.sub(r"\.part\d+$", "", name, flags=re.I)
    if stem.count(".") >= 2:
        stem = stem.replace(".", " ")
    stem = stem.replace("_", " ")
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem


def strip_quality(text: str) -> str:
    text = SCENE_RE.sub(" ", text)
    text = QUALITY_RE.sub(" ", text)
    text = ASIN_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" -_|")
    return text


def extract_year(text: str) -> tuple[str | None, str]:
    found = None
    matches = list(YEAR_RE.finditer(text))
    if matches:
        found = matches[-1].group(1)

        def drop(m: re.Match) -> str:
            return "" if m.group(1) == found else m.group(0)

        text = YEAR_RE.sub(drop, text, count=100)
        text = re.sub(r"\s+", " ", text).strip(" -_|")
    return found, text


def extract_narrator(text: str) -> tuple[str | None, str]:
    m = NARRATOR_RE.search(text)
    if not m:
        return None, text
    return m.group(1).strip(), (text[: m.start()] + text[m.end() :]).strip(" -_|")


def looks_like_person(text: str) -> bool:
    text = text.strip()
    if not text or YEAR_RE.search(text) or INDEX_RE.match(text):
        return False
    if "," in text:
        parts = [p.strip() for p in text.split(",") if p.strip()]
        return 1 <= len(parts) <= 3 and all(looks_like_person(p) for p in parts)
    words = text.split()
    # Single tokens are ambiguous (Flatland, Christine, Misery) — need 2+ words
    if not (2 <= len(words) <= 6):
        return False
    if words[0].casefold() in {"the", "a", "an"}:
        return False
    good = 0
    for w in words:
        w_clean = w.strip(" \"'`")
        wl = w_clean.casefold().strip(".")
        if wl in PERSON_PARTICLES or wl in {"&", "and"}:
            continue  # connector between surnames
        # Single-letter tokens are initials (A, H, G), not title stopwords like "a"/"an"
        if len(wl) > 1 and wl in TITLE_STOP:
            return False
        if re.match(r"^[A-Z]\.?$", w_clean) or re.match(r"^[A-Z]{1,3}$", w_clean):
            good += 1
            continue
        if not re.match(r"^[A-Z][A-Za-z.'\-]*$", w_clean):
            return False
        good += 1
    return good >= 1


def peel_trailing_author(title: str) -> tuple[str | None, str]:
    words = title.split()
    for n in (4, 3, 2):
        if len(words) > n and len(words) - n >= 1:
            cand = " ".join(words[-n:])
            rest = " ".join(words[:-n])
            if looks_like_person(cand) and rest:
                return cand, rest
    return None, title


def peel_index_prefix(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^\d{1,4}\s*[-.)]\s+", "", text)
    return text.strip()


def sanitize_component(name: str) -> str:
    name = INVALID_FS.sub(" ", name)
    name = name.replace("…", "...")
    name = re.sub(r"\s+", " ", name).strip(" .")
    name = name.rstrip(".")
    return name or "Unknown"


def unique_dir(path: Path) -> Path:
    if not path.exists():
        return path
    i = 2
    while True:
        candidate = path.parent / f"{path.name} ({i})"
        if not candidate.exists():
            return candidate
        i += 1


def pad_width(count: int) -> int:
    return max(2, len(str(max(count, 1))))


def which(names: list[str]) -> str | None:
    for n in names:
        found = shutil.which(n)
        if found:
            return found
    return None


@dataclass
class Meta:
    author: str
    title: str
    year: str | None = None
    narrator: str | None = None


def parse_name(raw: str) -> Meta:
    original = raw
    suffix = Path(raw).suffix.lower()
    if suffix in AUDIO_EXT or suffix in ARCHIVE_EXT:
        original = Path(raw).stem
        lower = raw.lower()
        if lower.endswith(".tar.gz"):
            original = Path(raw).name[: -len(".tar.gz")]
    text = humanize(original)
    narrator, text = extract_narrator(text)
    text = peel_index_prefix(text)
    text = SCENE_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" -_|")

    # "Title … Read by Author" / "Title Audiobook Read by Author"
    read_by_author = None
    m_rb = READ_BY_RE.search(text)
    if m_rb:
        read_by_author = m_rb.group(1).strip(" -_|")
        text = text[: m_rb.start()].strip(" -_|")
        text = strip_quality(text)

    author: str | None = None
    title = text
    year: str | None = None

    # Split on " - " but skip a pure numeric first segment (series index)
    parts = [p.strip() for p in re.split(r"\s+-\s+", text) if p.strip()]
    if parts and INDEX_RE.match(parts[0]):
        parts = parts[1:]
        text = " - ".join(parts) if parts else text

    if len(parts) >= 2:
        # Prefer last person-like segment as author when pattern is Title - Author
        left, right = parts[0], " - ".join(parts[1:])
        # If more than 2 segments: "Title - Subtitle - Author" or "Author - Title"
        left_year, left_rest = extract_year(left)
        right_year, right_rest = extract_year(right)
        year = right_year or left_year
        left_clean = strip_quality(BRACKET_RE.sub(" ", left_rest if left_year else left))
        right_clean = strip_quality(BRACKET_RE.sub(" ", right_rest if right_year else right))
        left_clean = re.sub(r"\s+", " ", left_clean).strip(" -_|")
        right_clean = re.sub(r"\s+", " ", right_clean).strip(" -_|")

        if left_year and not left_rest.strip():
            year = left_year
            if looks_like_person(right_clean):
                author, title = right_clean, left_year
            else:
                author, title = None, f"{left_year} - {right_clean}" if right_clean else left_year
        elif looks_like_person(left_clean) and looks_like_person(right_clean):
            # Audiobook dumps are usually "Title - Author"; bios may be "Author - Subject"
            # Prefer Title - Author (right = author). Bio filenames are fixed in enrich_meta.
            author, title = right_clean, left
        elif looks_like_person(left_clean) and not looks_like_person(right_clean):
            author, title = left_clean, right
        elif looks_like_person(right_clean):
            author, title = right_clean, left
        elif looks_like_person(left_clean):
            author, title = left_clean, right
        else:
            # Try trailing author on the full right+left combo
            peeled, rest = peel_trailing_author(f"{left_clean} {right_clean}".strip())
            if peeled:
                author, title = peeled, rest
            else:
                author, title = left_clean or None, right
    else:
        year, title = extract_year(text)
        title = strip_quality(title)
        title = BRACKET_RE.sub(" ", title)
        title = re.sub(r"\s+", " ", title).strip(" -_|")
        peeled, rest = peel_trailing_author(title)
        if peeled:
            author, title = peeled, rest

    if not year:
        y, rest = extract_year(title)
        year, title = y, rest or title
    else:
        y, rest = extract_year(title)
        if y == year:
            title = rest or title

    title = strip_quality(title)
    title = BRACKET_RE.sub(" ", title)
    title = re.sub(r"\s+", " ", title).strip(" -_|") or "Unknown Title"
    if title == "Unknown Title" and year:
        title = year
    if author:
        author = strip_quality(author)
        author = BRACKET_RE.sub(" ", author)
        author = re.sub(r"\s+", " ", author).strip(" -_|") or None
        if author and INDEX_RE.match(author):
            author = None
    # Prefer explicit "Read by X" when X looks like a person
    if read_by_author:
        rb = strip_quality(read_by_author)
        rb = re.sub(r"\s+", " ", rb).strip(" -_|")
        if looks_like_person(rb):
            author = rb
        elif not author:
            author = rb
    return Meta(author=author or "Unknown Author", title=title, year=year, narrator=narrator)


def meta_from_book_dir(book_dir: Path, source: Path) -> Meta:
    folder_meta = parse_name(book_dir.name)
    author = folder_meta.author
    title = folder_meta.title
    year = folder_meta.year
    narrator = folder_meta.narrator

    parent = book_dir.parent
    if (
        parent != source
        and not is_non_author_folder(parent.name)
        and parent.name.casefold() not in STOP_AUTHOR
    ):
        parent_clean = humanize(parent.name)
        if looks_like_person(parent_clean):
            author = parent_clean
            if folder_meta.author.casefold() == parent_clean.casefold():
                title = folder_meta.title
            else:
                raw = humanize(book_dir.name)
                raw = peel_index_prefix(raw)
                narr, raw = extract_narrator(raw)
                narrator = narrator or narr
                y, raw = extract_year(raw)
                year = year or y
                raw = strip_quality(raw)
                raw = BRACKET_RE.sub(" ", raw)
                title = re.sub(r"\s+", " ", raw).strip(" -_|") or folder_meta.title

    if author == "Unknown Author" or INDEX_RE.match(author or ""):
        peeled, rest = peel_trailing_author(title)
        if peeled:
            author, title = peeled, rest
        else:
            # "Title - Author" still inside title
            again = parse_name(title)
            if again.author != "Unknown Author":
                author, title = again.author, again.title
                year = year or again.year

    if author and INDEX_RE.match(author):
        author = "Unknown Author"

    title = re.sub(r"\s+", " ", title).strip(" -_|") or "Unknown Title"
    return Meta(author=author or "Unknown Author", title=title, year=year, narrator=narrator)


def tags_from_audio(path: Path) -> dict[str, str]:
    try:
        from mutagen import File as MutagenFile
    except ImportError:
        return {}
    try:
        audio = MutagenFile(path, easy=True)
    except Exception:
        return {}
    if not audio:
        return {}

    def first(*keys: str) -> str | None:
        for key in keys:
            try:
                val = audio.get(key)
            except Exception:
                val = None
            if not val:
                continue
            if isinstance(val, list):
                val = val[0] if val else None
            if val:
                text = str(val).strip()
                if text:
                    return text
        return None

    out: dict[str, str] = {}
    artist = first("albumartist", "artist", "composer")
    album = first("album")
    date = first("date", "year")
    if artist:
        out["artist"] = artist
    if album:
        out["album"] = album
    if date:
        out["date"] = date
    return out


def enrich_meta(meta: Meta, tracks: list[Path]) -> Meta:
    if not tracks:
        return meta
    author = meta.author
    title = meta.title
    year = meta.year

    # Track filenames like "Walter Isaacson - Steve Jobs 01-24.mp3" → Author - Title
    for t in tracks[:5]:
        stem = humanize(t.stem)
        stem = re.sub(r"\s+\d{1,3}\s*[-–]\s*\d{1,3}\s*$", "", stem).strip()
        parts = [p.strip() for p in re.split(r"\s+-\s+", stem) if p.strip()]
        if len(parts) >= 2 and looks_like_person(parts[0]):
            # Author - Title (track)
            rest = " - ".join(parts[1:])
            rest = re.sub(r"\s+\d{1,3}\s*$", "", rest).strip()
            if rest and not looks_like_person(rest):
                author = parts[0]
                if title in {author, "Unknown Title"} or looks_like_person(title):
                    title = rest
                break
            if rest and looks_like_person(rest) and looks_like_person(parts[0]):
                # Author - Subject biography
                author = parts[0]
                title = rest
                break

    tags = tags_from_audio(tracks[0])
    if tags:
        if (author == "Unknown Author" or INDEX_RE.match(author or "")) and tags.get("artist"):
            author = tags["artist"]
        if title == "Unknown Title" and tags.get("album"):
            title = tags["album"]
        if not year and tags.get("date"):
            match = YEAR_RE.search(tags["date"])
            if match:
                year = match.group(1)
    return Meta(author=author, title=title, year=year, narrator=meta.narrator)



def apply_known_titles(meta: Meta) -> Meta:
    """Fill year / correct author / canonicalize title from KNOWN_TITLES."""
    # Normalize author spelling first (Clark → Clarke, etc.)
    if meta.author and meta.author != "Unknown Author":
        meta = Meta(
            author=canonicalize_author(meta.author),
            title=meta.title,
            year=meta.year,
            narrator=meta.narrator,
        )
    keys_to_try = [
        title_key(meta.title),
        title_key(meta.author),  # when title and author were swapped into author field
    ]
    # Also try "author folder was actually a book title"
    for key in keys_to_try:
        if not key or key in {"unknown title", "unknown author"}:
            continue
        hit = KNOWN_TITLES.get(key)
        if not hit:
            # fuzzy: startswith match for long keys
            for k, v in KNOWN_TITLES.items():
                if len(k) >= 8 and (key.startswith(k) or k.startswith(key)):
                    hit = v
                    break
        if hit:
            k_author, k_year, k_title = hit
            author = meta.author
            title = meta.title
            year = meta.year
            # Prefer known author when current author is unknown or looks like a title fragment
            if author in {"Unknown Author"} or title_key(author) in KNOWN_TITLES or not looks_like_person(author):
                author = k_author
            elif looks_like_person(author) and author.casefold() != k_author.casefold():
                # Keep explicit person author only if it matches known
                if title_key(title) == key:
                    author = k_author
            title = k_title
            year = year or k_year
            return Meta(author=author, title=title, year=year, narrator=meta.narrator)
    # Year-only fill when title matches known under current author context
    tk = title_key(meta.title)
    if tk in KNOWN_TITLES:
        k_author, k_year, k_title = KNOWN_TITLES[tk]
        return Meta(
            author=k_author if meta.author == "Unknown Author" else meta.author,
            title=k_title,
            year=meta.year or k_year,
            narrator=meta.narrator,
        )
    return meta


def normalize_meta(meta: Meta, book_dir: Path, source: Path) -> Meta:
    """Fix common mis-parses: Disc N titles, section folders, Title/Author swaps, bios."""
    author, title, year, narrator = meta.author, meta.title, meta.year, meta.narrator
    parent = book_dir.parent if book_dir != source else source

    # Climb out of disc/section labels for title
    if (
        is_disc_folder(title)
        or is_section_folder(title)
        or is_disc_folder(book_dir.name)
        or is_section_folder(book_dir.name)
    ):
        climb = book_dir
        while climb != source and (
            is_disc_folder(climb.name) or is_section_folder(climb.name)
        ):
            climb = climb.parent
        if climb != source and climb != book_dir:
            parent_meta = parse_name(climb.name)
            if parent_meta.title and parent_meta.title not in {"Unknown Title"}:
                if not is_disc_folder(parent_meta.title) and not is_section_folder(parent_meta.title):
                    title = parent_meta.title
            if parent_meta.year:
                year = year or parent_meta.year
            if parent_meta.author != "Unknown Author":
                if author in {"Unknown Author"} or is_disc_folder(author):
                    author = parent_meta.author
            if looks_like_person(humanize(climb.name)):
                if author == "Unknown Author":
                    author = humanize(climb.name)
                if is_disc_folder(title) or is_section_folder(title) or title == "Unknown Title":
                    title = year or "Unknown Title"

    # Swap when author is clearly a title and title is clearly a person
    cleaned_title = title.replace('"', "").replace("'", "")
    y_t, title_rest = extract_year(cleaned_title)
    title_candidate = (title_rest or cleaned_title).strip(" -_|")
    title_candidate = re.sub(r"[()[\]]", " ", title_candidate)
    title_candidate = re.sub(r"\s+", " ", title_candidate).strip(" -_|")
    # "Edwin A Abbott - 1884" or "H G Wells - 1897" left in title field
    if " - " in title_candidate:
        left_t, right_t = [x.strip() for x in title_candidate.split(" - ", 1)]
        if looks_like_person(left_t) and not looks_like_person(right_t):
            title_candidate = left_t
        elif looks_like_person(right_t) and not looks_like_person(left_t):
            title_candidate = right_t
        elif looks_like_person(right_t):
            title_candidate = right_t
    title_is_person = looks_like_person(title_candidate)
    author_is_person = looks_like_person(author)
    author_starts_the = author.casefold().startswith(("the ", "a ", "an "))
    # Also treat single-token authors as titles (Flatland, etc.)
    author_is_titleish = (not author_is_person) and (
        author_starts_the
        or len(author.split()) <= 3
    )
    if title_is_person and author_is_titleish:
        author, title = title_candidate, author
        year = year or y_t

    # Author folder became title (merged discs under person name)
    if looks_like_person(title) and author in {title, "Unknown Author"}:
        author = title
        title = year or "Unknown Title"
    if looks_like_person(author) and title == author:
        title = year or "Unknown Title"

    if title == "Unknown Title" and year:
        title = year

    if author and INDEX_RE.match(author):
        author = "Unknown Author"
    if (is_disc_folder(title) or is_section_folder(title)) and year:
        title = year
    elif is_disc_folder(title) or is_section_folder(title):
        title = "Unknown Title"

    title = re.sub(r"\s+", " ", title).strip(" -_|") or "Unknown Title"

    # Apply known-title overrides (year fill-in, author correction, canonical title)
    meta_out = apply_known_titles(
        Meta(author=author or "Unknown Author", title=title, year=year, narrator=narrator)
    )
    return meta_out


def book_folder_name(meta: Meta, fmt: str) -> str:
    title = sanitize_component(meta.title)
    year = meta.year
    if year and title == year:
        name = year
    elif fmt == "year-title" and year:
        name = f"{year} - {title}"
    elif fmt == "title":
        name = title
    else:
        name = f"{title} ({year})" if year else title
    return sanitize_component(name)


def should_skip_dir(path: Path, source: Path, dest: Path, trash: Path) -> bool:
    try:
        if path == trash or trash in path.parents:
            return True
        if path == dest and dest != source:
            return True
        if dest != source and (dest in path.parents):
            return True
    except Exception:
        pass
    return path.name.casefold() in SKIP_DIR_NAMES or path.name.startswith(".")


def iter_files(root: Path, source: Path, dest: Path, trash: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        current = Path(dirpath)
        dirnames[:] = [
            d for d in dirnames if not should_skip_dir(current / d, source, dest, trash)
        ]
        for name in filenames:
            out.append(current / name)
    return out


def unwrap_book_dir(book_dir: Path, source: Path) -> Path:
    current = book_dir
    while current != source and (
        current.name.casefold() in WRAPPER_NAMES
        or is_disc_folder(current.name)
        or is_section_folder(current.name)
    ):
        current = current.parent
    return current


def book_root_for(audio: Path, source: Path) -> Path:
    parent = audio.parent
    if parent == source:
        return source
    current = parent
    while current != source and (
        is_disc_folder(current.name)
        or is_section_folder(current.name)
        or current.name.casefold() in WRAPPER_NAMES
    ):
        current = current.parent
    return current if current != source else source


def album_group_key(filename: str) -> str:
    stem = humanize(Path(filename).stem)
    stem = re.sub(
        r"(?:^|\s)(?:track|tr|ch|chapter|disc|cd|disk|pt|part)[\s._-]*\d+\s*",
        " ",
        stem,
        flags=re.I,
    )
    stem = re.sub(r"^[\s._-]*\d+[\s._-]*", "", stem)
    stem = re.sub(r"[\s._-]*\d+$", "", stem)
    stem = strip_quality(stem)
    _, stem = extract_year(stem)
    stem = re.sub(r"\s+", " ", stem).strip(" -_|")
    return stem.casefold() or Path(filename).stem.casefold()


def group_audio(audio_files: list[Path], source: Path) -> dict[Path, list[Path]]:
    groups: dict[Path, list[Path]] = defaultdict(list)
    loose: list[Path] = []
    for f in audio_files:
        root = book_root_for(f, source)
        if root == source:
            loose.append(f)
        else:
            groups[root].append(f)

    # Merge sibling Disc/CD/section folders under the same parent into one book root
    disc_buckets: dict[Path, list[Path]] = defaultdict(list)
    non_disc: dict[Path, list[Path]] = {}
    for root, tracks in groups.items():
        if root != source and (is_disc_folder(root.name) or is_section_folder(root.name)):
            disc_buckets[root.parent].extend(tracks)
        else:
            non_disc[root] = tracks
    merged: dict[Path, list[Path]] = dict(non_disc)
    for parent, tracks in disc_buckets.items():
        if parent == source:
            marker = source / ".loose.merged-discs"
            merged.setdefault(marker, []).extend(tracks)
        else:
            merged.setdefault(parent, []).extend(tracks)

    by_key: dict[str, list[Path]] = defaultdict(list)
    for f in loose:
        by_key[album_group_key(f.name)].append(f)
    for key, files in by_key.items():
        marker = source / f".loose.{key}"
        merged.setdefault(marker, []).extend(files)
    return merged



def group_ebooks(ebook_files: list[Path], source: Path) -> dict[Path, list[Path]]:
    """Group ebook files by book root (same heuristics as audio)."""
    groups: dict[Path, list[Path]] = defaultdict(list)
    loose: list[Path] = []
    for f in ebook_files:
        root = book_root_for(f, source)
        if root == source:
            loose.append(f)
        else:
            groups[root].append(f)

    # Merge section/disc-like wrappers
    buckets: dict[Path, list[Path]] = defaultdict(list)
    non_sec: dict[Path, list[Path]] = {}
    for root, files in groups.items():
        if root != source and (is_disc_folder(root.name) or is_section_folder(root.name)):
            buckets[root.parent].extend(files)
        else:
            non_sec[root] = files
    merged: dict[Path, list[Path]] = dict(non_sec)
    for parent, files in buckets.items():
        if parent == source:
            marker = source / ".loose.merged-ebooks"
            merged.setdefault(marker, []).extend(files)
        else:
            merged.setdefault(parent, []).extend(files)

    by_key: dict[str, list[Path]] = defaultdict(list)
    for f in loose:
        by_key[album_group_key(f.name)].append(f)
    for key, files in by_key.items():
        marker = source / f".loose.ebook.{key}"
        merged.setdefault(marker, []).extend(files)
    return merged


def ebook_dest_name(src: Path, meta: Meta) -> str:
    """Prefer Title.ext; keep original if already clean."""
    ext = src.suffix.lower()
    title = sanitize_component(meta.title) if meta.title not in {"Unknown Title"} else sanitize_component(src.stem)
    return f"{title}{ext}"


def detect_folder_media(files: list[Path]) -> str:
    """Return 'audio' or 'ebook' based on which media dominates."""
    na = sum(1 for f in files if is_audio(f))
    ne = sum(1 for f in files if is_ebook(f))
    if ne > na:
        return "ebook"
    return "audio"


def track_sort_key(path: Path, source: Path) -> tuple:
    disc = 0
    section = 0
    for parent in path.parents:
        if parent == source:
            break
        if is_disc_folder(parent.name) and disc == 0:
            disc = disc_number(parent.name)
        if is_section_folder(parent.name) and section == 0:
            section = section_number(parent.name)
    # Leading chapter/part index in the filename (e.g. "1_ Part One - November")
    leading = 0
    m = re.match(r"^(\d+)", path.stem.strip())
    if m:
        leading = int(m.group(1))
    # disc → filename leading index → section → name (Roadwork months use leading index)
    return (disc, leading if leading else section, natural_key(path.name), str(path).casefold())


def track_filename(index: int, src: Path, width: int, keep_names: bool) -> str:
    num = str(index).zfill(width)
    ext = src.suffix.lower() or ".mp3"
    if not keep_names:
        return f"{num}{ext}"
    original = humanize(src.stem)
    original = re.sub(
        r"^(?:track|tr|chapter|ch)[\s._-]*\d+[\s._-]*", "", original, flags=re.I
    )
    original = re.sub(r"^0*\d+[\s._-]*", "", original)
    original = original.strip(" -_|") or src.stem
    original = sanitize_component(original)
    return f"{num} - {original}{ext}"


class UnsafeArchive(Exception):
    pass


def _safe_target(dest_dir: Path, member: str) -> Path:
    dest_dir = dest_dir.resolve()
    member = member.replace("\\", "/")
    if member.startswith("/") or re.match(r"^[a-zA-Z]:", member):
        raise UnsafeArchive(member)
    target = (dest_dir / member).resolve()
    if dest_dir != target and dest_dir not in target.parents:
        raise UnsafeArchive(member)
    return target


def extract_dir_for(archive: Path) -> Path:
    name = archive.name
    lower = name.lower()
    m = PART_RE.match(name)
    if m:
        base = m.group("base")
    elif lower.endswith(".tar.gz"):
        base = name[: -len(".tar.gz")]
    elif lower.endswith(".tgz"):
        base = name[: -len(".tgz")]
    else:
        base = archive.stem
    dest = archive.parent / base
    return unique_dir(dest) if dest.exists() else dest


def extract_zip(path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            _safe_target(dest, info.filename)
        zf.extractall(dest)


def extract_tar(path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path) as tf:
        for member in tf.getmembers():
            _safe_target(dest, member.name)
        tf.extractall(dest)


def run_cmd(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, capture_output=True, text=True)


def extract_rar(path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    try:
        import rarfile

        with rarfile.RarFile(path) as rf:
            for info in rf.infolist():
                _safe_target(dest, info.filename)
            rf.extractall(dest)
        return
    except ImportError:
        pass
    except Exception:
        pass
    unrar = which(["unrar"])
    if unrar:
        r = run_cmd([unrar, "x", "-o+", "-y", str(path), str(dest) + os.sep])
        if r.returncode == 0:
            return
        raise RuntimeError(r.stderr or r.stdout or "unrar failed")
    unar = which(["unar"])
    if unar:
        r = run_cmd([unar, "-f", "-o", str(dest), str(path)])
        if r.returncode == 0:
            return
        raise RuntimeError(r.stderr or r.stdout or "unar failed")
    seven = which(["7z", "7za", "7zr"])
    if seven:
        r = run_cmd([seven, "x", f"-o{dest}", "-y", str(path)])
        if r.returncode == 0:
            return
        raise RuntimeError(r.stderr or r.stdout or "7z failed")
    raise RuntimeError(
        "No RAR extractor found. Install unrar, unar, or 7-Zip and keep it on PATH."
    )


def extract_7z(path: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    seven = which(["7z", "7za", "7zr"])
    if not seven:
        raise RuntimeError("No 7z binary found. Install 7-Zip and keep it on PATH.")
    r = run_cmd([seven, "x", f"-o{dest}", "-y", str(path)])
    if r.returncode != 0:
        raise RuntimeError(r.stderr or r.stdout or "7z failed")


def extract_archive(path: Path) -> Path:
    kind = archive_kind(path)
    # For multipart, kind is still rar/zip from the first volume
    if kind is None and PART_RE.match(path.name):
        kind = path.suffix.lower()[1:]
    dest = extract_dir_for(path)
    if kind == "zip":
        extract_zip(path, dest)
    elif kind == "tar":
        extract_tar(path, dest)
    elif kind == "rar":
        extract_rar(path, dest)
    elif kind == "7z":
        extract_7z(path, dest)
    else:
        raise RuntimeError(f"Unsupported archive: {path.name}")
    return dest


def multipart_key(path: Path) -> tuple[str, int] | None:
    m = PART_RE.match(path.name)
    if m:
        return (f"{m.group('base').casefold()}|{m.group('ext').casefold()}", int(m.group("num")))
    m = RVOL_RE.match(path.name)
    if m:
        return (f"{m.group('base').casefold()}|rar", int(m.group("num")))
    return None


def find_archives(files: list[Path]) -> list[Path]:
    singles: list[Path] = []
    multipart: dict[str, list[tuple[int, Path]]] = {}
    for f in files:
        if not f.is_file() or not archive_kind(f):
            # .partN.rar still has archive_kind rar
            if not f.is_file():
                continue
            if not archive_kind(f) and not PART_RE.match(f.name):
                continue
        mp = multipart_key(f)
        if mp:
            key, num = mp
            multipart.setdefault(key, []).append((num, f))
        elif archive_kind(f):
            singles.append(f)
    chosen = list(singles)
    for _key, volumes in multipart.items():
        volumes.sort(key=lambda t: t[0])
        chosen.append(volumes[0][1])
    return chosen


@dataclass
class FileOp:
    src: Path
    dest: Path
    kind: str
    note: str = ""


@dataclass
class BookPlan:
    source_dir: Path
    meta: Meta
    dest_dir: Path
    tracks: list[FileOp] = field(default_factory=list)
    extras: list[FileOp] = field(default_factory=list)


@dataclass
class Plan:
    books: list[BookPlan] = field(default_factory=list)
    extracts: list[FileOp] = field(default_factory=list)
    trash: list[FileOp] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def pick_cover(files: list[Path]) -> Path | None:
    covers = [f for f in files if f.is_file() and is_cover(f)]
    if not covers:
        return None
    rank = {n: i for i, n in enumerate(["cover", "folder", "poster", "front", "album"])}

    def key(p: Path) -> tuple:
        stem = p.stem.lower()
        r = 50
        for name, i in rank.items():
            if stem.startswith(name):
                r = i
                break
        return (r, len(stem), p.name.lower())

    covers.sort(key=key)
    return covers[0]


def collect_sidecars(book_dir: Path, tracks: list[Path], source: Path) -> list[Path]:
    if str(book_dir).startswith(str(source / ".loose.")):
        return []
    if not book_dir.exists():
        return []
    sidecars: list[Path] = []
    track_set = {t.resolve() for t in tracks if t.exists()}
    for dirpath, dirnames, filenames in os.walk(book_dir):
        current = Path(dirpath)
        dirnames[:] = [
            d
            for d in dirnames
            if d.casefold() not in SKIP_DIR_NAMES and not d.startswith(".")
        ]
        for name in filenames:
            p = current / name
            try:
                if p.resolve() in track_set:
                    continue
            except OSError:
                continue
            sidecars.append(p)
    return sidecars



def merge_duplicate_books(books: list[BookPlan], source: Path, keep_names: bool) -> list[BookPlan]:
    """Combine books that resolved to the same Author/Title/Year (e.g. Disc 1..N of 1984)."""
    buckets: dict[tuple[str, str, str], BookPlan] = {}
    order: list[tuple[str, str, str]] = []
    for book in books:
        key = (
            book.meta.author.casefold().strip(),
            book.meta.title.casefold().strip(),
            (book.meta.year or "").strip(),
        )
        if key not in buckets:
            buckets[key] = book
            order.append(key)
            continue
        existing = buckets[key]
        existing.tracks.extend(book.tracks)
        existing.extras.extend(book.extras)
        # Prefer a real directory over a .loose marker as source_dir
        if str(existing.source_dir).startswith(str(source / ".loose.")) and not str(
            book.source_dir
        ).startswith(str(source / ".loose.")):
            existing.source_dir = book.source_dir

    merged: list[BookPlan] = []
    for key in order:
        book = buckets[key]
        # Re-sort and renumber continuously (preserve ebook vs audio)
        is_ebook_book = any(op.kind == "ebook" for op in book.tracks)
        srcs = [op.src for op in book.tracks]
        if is_ebook_book:
            srcs = sorted(set(srcs), key=lambda path: (
                EBOOK_PREF.index(path.suffix.lower()) if path.suffix.lower() in EBOOK_PREF else 99,
                natural_key(path.name),
            ))
            new_tracks = [
                FileOp(src, book.dest_dir / ebook_dest_name(src, book.meta), "ebook")
                for src in srcs
            ]
        else:
            srcs = sorted(set(srcs), key=lambda path: track_sort_key(path, source))
            width = pad_width(len(srcs))
            new_tracks = [
                FileOp(src, book.dest_dir / track_filename(i, src, width, keep_names), "track")
                for i, src in enumerate(srcs, start=1)
            ]
        book.tracks = new_tracks
        # Dedupe covers/extras by dest name, prefer cover
        seen_extra: set[str] = set()
        new_extras: list[FileOp] = []
        for op in book.extras:
            k = op.dest.name.casefold()
            if k in seen_extra:
                continue
            seen_extra.add(k)
            new_extras.append(op)
        book.extras = new_extras
        merged.append(book)
    return merged


def build_plan(
    source: Path,
    dest: Path,
    trash: Path,
    folder_format: str,
    keep_names: bool,
    include_non_cover_images: bool,
    media_mode: str = "auto",
) -> Plan:
    plan = Plan()
    files = [p for p in iter_files(source, source, dest, trash) if p.is_file()]
    archives = find_archives(files)
    for arc in archives:
        target = extract_dir_for(arc)
        plan.extracts.append(FileOp(arc, target, "extract", "archive"))
        # Other volumes of the same set → trash after successful extract
        mp = multipart_key(arc)
        if mp:
            base_key = mp[0]
            for f in files:
                other = multipart_key(f)
                if other and other[0] == base_key and f != arc:
                    plan.trash.append(FileOp(f, trash / f.name, "trash", "multipart volume"))

    audio = [
        p
        for p in files
        if p.suffix.lower() in AUDIO_EXT and not SAMPLE_RE.search(p.stem)
    ]
    ebooks = [p for p in files if p.suffix.lower() in EBOOK_EXT]
    sample_audio = [
        p for p in files if p.suffix.lower() in AUDIO_EXT and SAMPLE_RE.search(p.stem)
    ]
    for s in sample_audio:
        plan.trash.append(FileOp(s, trash / s.name, "trash", "sample"))

    mode = (media_mode or "auto").casefold()
    do_audio = mode in {"auto", "audio", "both"}
    do_ebook = mode in {"auto", "ebook", "both"}
    if mode == "audio":
        ebooks = []
    if mode == "ebook":
        audio = []

    used_dest_dirs: dict[str, int] = {}

    def allocate_dest(meta: Meta) -> Path:
        author_dir = dest / sanitize_component(meta.author)
        folder = book_folder_name(meta, folder_format)
        dest_dir = author_dir / folder
        key = str(dest_dir).casefold()
        if key in used_dest_dirs:
            used_dest_dirs[key] += 1
            dest_dir = author_dir / f"{folder} ({used_dest_dirs[key]})"
        else:
            used_dest_dirs[key] = 1
        return dest_dir

    def add_sidecars(bp: BookPlan, book_dir: Path, media_files: list[Path]) -> None:
        sidecars = collect_sidecars(book_dir, media_files, source)
        cover = pick_cover(sidecars)
        for sc in sidecars:
            if cover is not None and sc == cover:
                ext = sc.suffix.lower()
                if ext == ".jpeg":
                    ext = ".jpg"
                bp.extras.append(FileOp(sc, bp.dest_dir / f"cover{ext}", "cover", "cover"))
                continue
            if sc.name.lower() in KEEP_TEXT:
                bp.extras.append(FileOp(sc, bp.dest_dir / sc.name.lower(), "keep", "sidecar"))
                continue
            if include_non_cover_images and sc.suffix.lower() in COVER_EXT:
                bp.extras.append(
                    FileOp(sc, bp.dest_dir / sanitize_component(sc.name), "keep", "image")
                )
                continue
            if is_junk_file(sc) or sc.suffix.lower() in COVER_EXT or not is_media(sc):
                bp.extras.append(FileOp(sc, trash / sc.name, "trash", "junk"))

    if do_audio:
        groups = group_audio(audio, source)
        for book_dir, tracks in sorted(groups.items(), key=lambda kv: str(kv[0]).casefold()):
            tracks = sorted(tracks, key=lambda p: track_sort_key(p, source))
            if not tracks:
                continue
            # In auto mode, skip folders that are primarily ebooks
            if mode == "auto":
                sibling_media = list(tracks)
                if book_dir.exists() and book_dir.is_dir():
                    for child in book_dir.rglob("*"):
                        if child.is_file() and is_ebook(child):
                            sibling_media.append(child)
                if detect_folder_media(sibling_media) == "ebook" and any(
                    is_ebook(f) for f in sibling_media
                ):
                    # Let the ebook pass own these files; audio here is unusual
                    pass  # still process audio if present
            if str(book_dir).startswith(str(source / ".loose.")):
                meta = parse_name(tracks[0].name)
            else:
                meta = meta_from_book_dir(book_dir, source)
            meta = enrich_meta(meta, tracks)
            meta = normalize_meta(meta, book_dir, source)
            dest_dir = allocate_dest(meta)
            width = pad_width(len(tracks))
            bp = BookPlan(source_dir=book_dir, meta=meta, dest_dir=dest_dir)
            for i, track in enumerate(tracks, start=1):
                dest_name = track_filename(i, track, width, keep_names)
                bp.tracks.append(FileOp(track, dest_dir / dest_name, "track"))
            add_sidecars(bp, book_dir, tracks)
            plan.books.append(bp)

    if do_ebook:
        egroups = group_ebooks(ebooks, source)
        for book_dir, efiles in sorted(egroups.items(), key=lambda kv: str(kv[0]).casefold()):
            # Prefer better formats first
            def ebook_sort(path: Path) -> tuple:
                ext = path.suffix.lower()
                try:
                    pref = EBOOK_PREF.index(ext)
                except ValueError:
                    pref = 99
                return (pref, natural_key(path.name))

            efiles = sorted(set(efiles), key=ebook_sort)
            if not efiles:
                continue
            if str(book_dir).startswith(str(source / ".loose.")):
                meta = parse_name(efiles[0].name)
            else:
                meta = meta_from_book_dir(book_dir, source)
            # enrich_meta is audio-tag oriented; still try
            meta = enrich_meta(meta, efiles)
            meta = normalize_meta(meta, book_dir, source)
            dest_dir = allocate_dest(meta)
            bp = BookPlan(source_dir=book_dir, meta=meta, dest_dir=dest_dir)
            for ef in efiles:
                dest_name = ebook_dest_name(ef, meta)
                bp.tracks.append(FileOp(ef, dest_dir / dest_name, "ebook"))
            add_sidecars(bp, book_dir, efiles)
            plan.books.append(bp)

    claimed: set[Path] = set()
    for op in plan.extracts:
        claimed.add(op.src)
    for book in plan.books:
        for op in book.tracks + book.extras:
            claimed.add(op.src)
    for op in plan.trash:
        claimed.add(op.src)

    for f in files:
        if f in claimed:
            continue
        if archive_kind(f) or multipart_key(f):
            continue
        if is_junk_file(f) or f.suffix.lower() in COVER_EXT or f.suffix.lower() in TRASH_EXT:
            plan.trash.append(FileOp(f, trash / f.name, "trash", "unassigned junk"))
        elif f.suffix.lower() in AUDIO_EXT or f.suffix.lower() in EBOOK_EXT:
            plan.warnings.append(f"Unassigned media (skipped): {f}")
        else:
            plan.trash.append(FileOp(f, trash / f.name, "trash", "unassigned"))

    plan.books = merge_duplicate_books(plan.books, source, keep_names)
    # Recompute dest_dir uniqueness after merge
    used: dict[str, int] = {}
    for book in plan.books:
        author_dir = dest / sanitize_component(book.meta.author)
        folder = book_folder_name(book.meta, folder_format)
        dest_dir = author_dir / folder
        key = str(dest_dir).casefold()
        if key in used:
            used[key] += 1
            dest_dir = author_dir / f"{folder} ({used[key]})"
        else:
            used[key] = 1
        if dest_dir != book.dest_dir:
            book.dest_dir = dest_dir
            for op in book.tracks:
                op.dest = dest_dir / op.dest.name
            for op in book.extras:
                if op.kind != "trash":
                    op.dest = dest_dir / op.dest.name
    return plan


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def unique_file(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 2
    while True:
        candidate = path.parent / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def move_file(src: Path, dest: Path) -> None:
    if not src.exists():
        return
    dest = Path(dest)
    if src.resolve() == dest.resolve():
        return
    ensure_parent(dest)
    dest = unique_file(dest)
    shutil.move(str(src), str(dest))


def copy_file(src: Path, dest: Path) -> None:
    if not src.exists():
        return
    ensure_parent(dest)
    dest = unique_file(dest)
    shutil.copy2(str(src), str(dest))


def unique_trash_path(trash: Path, name: str) -> Path:
    dest = trash / name
    if not dest.exists():
        return dest
    stem, suffix = Path(name).stem, Path(name).suffix
    n = 2
    while True:
        candidate = trash / f"{stem} ({n}){suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def remove_empty_dirs(root: Path, keep: set[Path]) -> None:
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        current = Path(dirpath)
        if current in keep:
            continue
        if current.name.casefold() in SKIP_DIR_NAMES:
            continue
        try:
            if not any(current.iterdir()):
                current.rmdir()
        except OSError:
            pass


def apply_extracts(plan: Plan, trash: Path, dry_run: bool) -> list[str]:
    errors: list[str] = []
    for op in plan.extracts:
        if dry_run:
            continue
        try:
            extracted = extract_archive(op.src)
            print(f"  extracted {op.src.name} -> {extracted.name}/")
            if op.src.exists():
                move_file(op.src, unique_trash_path(trash, op.src.name))
        except Exception as exc:
            errors.append(f"{op.src.name}: {exc}")
    return errors


def apply_plan(plan: Plan, trash: Path, dry_run: bool, copy: bool) -> None:
    transfer = copy_file if copy else move_file
    if not dry_run:
        trash.mkdir(parents=True, exist_ok=True)
    for book in plan.books:
        for op in book.tracks + book.extras:
            if op.kind == "trash":
                dest = unique_trash_path(trash, op.src.name)
                if dry_run:
                    continue
                transfer(op.src, dest)
            else:
                if dry_run:
                    continue
                transfer(op.src, op.dest)
    for op in plan.trash:
        if dry_run:
            continue
        dest = unique_trash_path(trash, op.src.name)
        transfer(op.src, dest)
    if not dry_run and not copy:
        for op in plan.extracts:
            if op.src.exists():
                move_file(op.src, unique_trash_path(trash, op.src.name))


def rel(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def print_plan(plan: Plan, source: Path, dest: Path) -> None:
    print()
    print(f"Source      {source}")
    print(f"Destination {dest}")
    print(f"Books       {len(plan.books)}")
    tracks = sum(len(b.tracks) for b in plan.books)
    print(f"Tracks      {tracks}")
    print(f"Archives    {len(plan.extracts)}")
    trash_n = len(plan.trash) + sum(
        1 for b in plan.books for e in b.extras if e.kind == "trash"
    )
    print(f"Trash       {trash_n}")
    print()

    if plan.extracts:
        print("Extract")
        for op in plan.extracts:
            print(f"  {op.src.name}  ->  {op.dest.name}/")
        print()

    print("Books")
    for book in plan.books:
        if book.meta.year and book.meta.title != book.meta.year:
            year = f" ({book.meta.year})"
        else:
            year = ""
        print(f"  {book.meta.author} / {book.meta.title}{year}")
        print(f"    -> {rel(book.dest_dir, dest)}")
        kind = "ebooks" if any(op.kind == "ebook" for op in book.tracks) else "tracks"
        for op in book.tracks[:5]:
            print(f"       {op.src.name}  =>  {op.dest.name}")
        if len(book.tracks) > 5:
            print(f"       … +{len(book.tracks) - 5} more {kind}")
        for op in book.extras:
            if op.kind == "trash":
                continue
            print(f"       {op.src.name}  =>  {op.dest.name} ({op.kind})")
        print()

    if plan.warnings:
        print("Warnings")
        for w in plan.warnings[:20]:
            print(f"  {w}")
        if len(plan.warnings) > 20:
            print(f"  … +{len(plan.warnings) - 20} more")
        print()


def confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    try:
        answer = input(prompt).strip().casefold()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def run(args: argparse.Namespace) -> int:
    source = Path(args.source).expanduser().resolve()
    if not source.is_dir():
        eprint(f"Source is not a directory: {source}")
        return 2

    dest = Path(args.dest).expanduser().resolve() if args.dest else source
    trash = (
        Path(args.trash).expanduser().resolve()
        if args.trash
        else source / args.trash_name
    )

    dry_run = not args.apply
    if args.dry_run:
        dry_run = True

    print("Scanning…")
    plan = build_plan(
        source=source,
        dest=dest,
        trash=trash,
        folder_format=args.format,
        keep_names=args.keep_names,
        include_non_cover_images=args.keep_images,
        media_mode=getattr(args, "media", "auto"),
    )
    print_plan(plan, source, dest)

    if not plan.books and not plan.extracts:
        print("Nothing to do.")
        return 0

    if dry_run and plan.extracts:
        print(
            "Dry run does not extract archives. Re-run with --apply so zip/rar "
            "contents can be grouped into books."
        )
        print()

    if dry_run:
        print("Dry run. No files were changed. Pass --apply to make it real.")
        return 0

    if not args.yes and sys.stdin.isatty():
        if not confirm("Apply this plan? [y/N] "):
            print("Cancelled.")
            return 1

    if plan.extracts:
        print("Extracting archives…")
        errors = apply_extracts(plan, trash=trash, dry_run=False)
        for err in errors:
            eprint(f"  extract failed: {err}")
        print("Re-scanning after extract…")
        plan = build_plan(
            source=source,
            dest=dest,
            trash=trash,
            folder_format=args.format,
            keep_names=args.keep_names,
            include_non_cover_images=args.keep_images,
            media_mode=getattr(args, "media", "auto"),
        )
        print_plan(plan, source, dest)

    print("Moving files…")
    apply_plan(plan, trash=trash, dry_run=False, copy=args.copy)

    keep = {source, dest, trash}
    remove_empty_dirs(source, keep)

    print()
    print(f"Done. {len(plan.books)} books on the shelf.")
    print(f"Junk is in {trash}")
    if dest == source:
        print("Remove the trash folder before pointing Audiobookshelf at this directory.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="main",
        description="Organize messy audiobook folders for Audiobookshelf.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("source", help="Folder of messy audiobooks (the dump).")
    p.add_argument(
        "--dest",
        help="Clean library folder. Omit to reorganize the source folder in place.",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Actually move files. Without this flag, only prints a plan.",
    )
    p.add_argument("--dry-run", action="store_true", help="Force plan-only run.")
    p.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation.")
    p.add_argument(
        "--copy",
        action="store_true",
        help="Copy files to --dest instead of moving them.",
    )
    p.add_argument(
        "--keep-names",
        action="store_true",
        help='Name tracks "01 - Chapter title.mp3" instead of "01.mp3".',
    )
    p.add_argument(
        "--keep-images",
        action="store_true",
        help="Keep every image in the book folder, not just cover art.",
    )
    p.add_argument(
        "--format",
        choices=("title-year", "year-title", "title"),
        default="year-title",
        help='Book folder pattern. Default: "Year - Title".',
    )
    p.add_argument(
        "--media",
        choices=("auto", "audio", "ebook", "both"),
        default="auto",
        help="Which media to organize: auto (default), audio, ebook, or both.",
    )
    p.add_argument("--trash-name", default="trash", help='Trash folder name (default: "trash").')
    p.add_argument("--trash", help="Full path for junk. Defaults to <source>/<trash-name>.")
    return p


def self_test() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="bindery-test-"))
    dump = tmp / "dump"
    dest = tmp / "library"
    dump.mkdir()
    dest.mkdir()

    def touch(path: Path, text: str = "x") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode())

    # numeric index + Title - Author
    touch(dump / "1 - Ender's Game - Orson Scott Card (1985)" / "01.mp3")
    touch(dump / "87 - Altered Carbon - Richard Morgan (2002)" / "01.mp3")

    # discs under one book
    for d in range(1, 4):
        touch(dump / "1984" / f"Disc {d}" / f"Track {d}.mp3")

    # multipart rar names only listed once
    for i in range(1, 5):
        touch(dump / f"Book.part{i:02d}.rar")

    # ebook sample
    touch(dump / "Frankenstein - Mary Shelley (1818)" / "Frankenstein.epub")

    plan = build_plan(
        source=dump,
        dest=dest,
        trash=dump / "trash",
        folder_format="title-year",
        keep_names=False,
        include_non_cover_images=False,
        media_mode="both",
    )
    authors = {b.meta.author for b in plan.books}
    print("authors:", sorted(authors))
    for b in plan.books:
        print(f"- {b.meta.author} | {b.meta.title} | tracks={len(b.tracks)}")
    assert "Orson Scott Card" in authors
    assert "Richard Morgan" in authors
    assert not any(INDEX_RE.match(a) for a in authors)
    # discs merged
    nineteen = [b for b in plan.books if "1984" in b.meta.title or b.meta.year == "1984"]
    assert nineteen, plan.books
    assert len(nineteen[0].tracks) == 3
    # one multipart extract
    assert len(plan.extracts) == 1, plan.extracts
    ebook_books = [b for b in plan.books if any(op.kind == "ebook" for op in b.tracks)]
    assert ebook_books, "expected at least one ebook book"
    print("self-test OK")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--self-test"]:
        return self_test()
    parser = build_parser()
    args = parser.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        eprint("\nInterrupted.")
        raise SystemExit(130)

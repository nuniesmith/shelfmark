"""Repair Audiobookshelf metadata.json files left behind by a listicle pack.

The library's FOLDERS are already correct — "John Wyndham/1951 - The Day of the
Triffids/". What is wrong is the metadata.json inside each one, which ABS reads
in preference to the folder structure:

    "title":   "43 - The Day of the Triffids - John Wyndham - 1951"
    "authors": []

An empty authors list is why Audiobookshelf invents a single author for the
whole pack, and the raw title is why every item is displayed with its list
number and a duplicated author and year.

Both are recoverable without touching a single audio file. The author comes
from the parent directory, which is already right. The title is parsed out of
the existing metadata title rather than taken from the folder name, because the
folder name has been through filesystem sanitising and has lost punctuation —
"2001: A Space Odyssey" survives in the metadata but is "2001 A Space Odyssey"
on disk.

The parser used is the books tool's own parse_name, the one just fixed to read
pre-1900 years and to stop collapsing initials. If it cannot agree with the
folder, the file is left alone and reported.

Only files whose authors list is empty are considered. Anything already filled
in — the Stephen King books, which are fine — is never rewritten, so this is
safe to run over the whole library and safe to run twice.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

def load_parser():
    """parse_name lives beside this file, in the organiser itself."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from main import parse_name  # noqa: E402

    return parse_name


def norm(s: str) -> str:
    """Compare names ignoring punctuation and spacing ("H. G." vs "H G")."""
    return "".join(c for c in (s or "").casefold() if c.isalnum())


def _tokens(s: str) -> set[str]:
    return {t for t in (norm(w) for w in (s or "").split()) if t}


def same_person(parsed: str, folder: str) -> bool:
    """Do these two spellings name the same author?

    The folder is the trusted side; this only has to catch the case where the
    metadata string is a messier rendering of it. Three real examples from this
    library, all of which are the same person:

        "Madelein L'Engle"  vs  "Madeleine L'Engle"   (typo in the source)
        "Robert Heinlein"   vs  "Robert A Heinlein"   (dropped middle initial)
        "Lem Stanislaw"     vs  "Stanislaw Lem"       (name order reversed)

    A shared surname or a substantially shared token set covers all three, while
    still refusing two genuinely different authors, who share neither.
    """
    if norm(parsed) == norm(folder):
        return True
    a, b = _tokens(parsed), _tokens(folder)
    if not a or not b:
        return False
    pa, pb = parsed.split(), folder.split()
    if pa and pb and norm(pa[-1]) == norm(pb[-1]):
        return True
    return len(a & b) >= max(1, min(len(a), len(b)) / 2)


def plan_one(meta_path: Path, parse_name) -> dict | None:
    """Return the change for one file, or None if it needs none."""
    try:
        data = json.loads(meta_path.read_text())
    except Exception as exc:
        return {"path": meta_path, "skip": f"unreadable: {exc}"}

    if data.get("authors"):
        return None  # already correct — never touched

    book_dir = meta_path.parent
    folder_author = book_dir.parent.name
    raw_title = data.get("title") or book_dir.name

    meta = parse_name(raw_title)
    title = meta.title
    year = data.get("publishedYear") or meta.year

    # The folder author is the trusted value; the parse only has to agree.
    if meta.author and meta.author != "Unknown Author":
        if not same_person(meta.author, folder_author):
            return {
                "path": meta_path,
                "skip": f"parsed author {meta.author!r} != folder {folder_author!r}",
            }

    if not title or title == "Unknown Title":
        return {"path": meta_path, "skip": f"could not parse a title from {raw_title!r}"}

    return {
        "path": meta_path,
        "folder_author": folder_author,
        "old_title": raw_title,
        "new_title": title,
        "year": year,
        "data": data,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("library", help="library root (the folder holding the author dirs)")
    ap.add_argument("--apply", action="store_true", help="write changes (default: preview)")
    ap.add_argument("--backup", action="store_true", help="keep metadata.json.bak beside each file")
    args = ap.parse_args()

    parse_name = load_parser()
    root = Path(args.library).expanduser()
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2

    metas = sorted(root.glob("*/*/metadata.json"))
    changes, skips = [], []
    untouched = 0
    for m in metas:
        r = plan_one(m, parse_name)
        if r is None:
            untouched += 1
        elif "skip" in r:
            skips.append(r)
        else:
            changes.append(r)

    print(f"metadata.json files found : {len(metas)}")
    print(f"  already have an author  : {untouched}  (never touched)")
    print(f"  to repair               : {len(changes)}")
    print(f"  cannot repair safely    : {len(skips)}")
    print()
    for c in changes[:12]:
        print(f"  {c['folder_author']}")
        print(f"      title  {c['old_title']!r}")
        print(f"          ->  {c['new_title']!r}")
    if len(changes) > 12:
        print(f"  ... and {len(changes)-12} more")
    for s in skips:
        print(f"  SKIP {s['path']}: {s['skip']}")

    if not args.apply:
        print("\npreview only — pass --apply to write")
        return 0

    written = 0
    for c in changes:
        data = c["data"]
        data["authors"] = [c["folder_author"]]
        data["title"] = c["new_title"]
        if c["year"]:
            data["publishedYear"] = str(c["year"])
        if args.backup:
            shutil.copy2(c["path"], c["path"].with_suffix(".json.bak"))
        tmp = c["path"].with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
        tmp.replace(c["path"])
        written += 1
    print(f"\nwrote {written} file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

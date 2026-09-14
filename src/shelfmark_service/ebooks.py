"""Index the ebooks root Shelfmark keeps on disk so Discord can search it.

Audiobookshelf has exactly one library (`Audiobooks`) and the user does not
want to run a separate ebook reader app just to browse files, so this module
walks `SHELFMARK_EBOOKS_ROOT` directly rather than delegating to the ABS API
the rest of the service otherwise uses for audiobooks.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


def _main_module():
    """Reach main.py's is_ebook()/EBOOK_PREF without a second extension list.

    Production runs with PYTHONPATH=/app/src (see Dockerfile), where `main`
    is importable directly. The test suite instead runs `unittest` from the
    repo root, where `src` is what ends up on sys.path and only `src.main`
    resolves there. Trying both here means EBOOK_EXT/EBOOK_PREF have exactly
    one definition instead of two lists that can silently drift apart.
    """
    try:
        import main as _main
    except ImportError:
        from src import main as _main
    return _main


@dataclass(frozen=True)
class Ebook:
    id: str
    title: str
    author: str | None
    relpath: str
    size: int
    ext: str


class EbookNotFound(Exception):
    """No file under the ebooks root matches the given id."""


def _ebook_id(root: Path, path: Path) -> str:
    """Hash the path RELATIVE to the root; never expose or accept the path itself.

    A download request only ever carries this id. `resolve_ebook` re-derives
    the same hash for every real file the walk turns up and looks for a
    match, so there is no step where client-supplied text gets joined onto a
    directory — the `../../etc/passwd` shape has nowhere to attach itself.
    """
    rel = path.relative_to(root).as_posix()
    return hashlib.sha256(rel.encode("utf-8")).hexdigest()[:24]


def _iter_ebook_files(root: Path) -> Iterator[Path]:
    is_ebook = _main_module().is_ebook
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if is_ebook(path):
            yield path


def _pref_index(path: Path) -> int:
    ebook_pref: list[str] = _main_module().EBOOK_PREF
    ext = path.suffix.lower()
    return ebook_pref.index(ext) if ext in ebook_pref else len(ebook_pref)


def _group_key(path: Path) -> tuple[Path, str]:
    # Same folder AND same stem is "the same book in another format" — what
    # main.py's organizer produces when a title has more than one EBOOK_PREF
    # hit (e.g. Title.epub next to Title.pdf). Grouping on the folder alone
    # would also merge unrelated titles that happen to share one — an
    # Author/ folder with no per-book subfolder yet — and silently drop one
    # of them out of every search result.
    return (path.parent, path.stem.casefold())


def list_ebooks(root: Path, query: str, limit: int = 25) -> list[Ebook]:
    """Search the ebooks root, returning one entry per book, best format first."""
    groups: dict[tuple[Path, str], list[Path]] = {}
    for path in _iter_ebook_files(root):
        groups.setdefault(_group_key(path), []).append(path)

    needle = query.strip().casefold()
    entries: list[Ebook] = []
    for files in groups.values():
        files.sort(key=lambda p: (_pref_index(p), p.name.casefold()))
        best = files[0]
        rel = best.relative_to(root)
        parts = rel.parts
        if len(parts) >= 3:
            # Author/Year - Title/Title.ext — main.py's organized layout.
            author, title = parts[0], parts[-2]
        elif len(parts) == 2:
            # Author/Title.ext — no dedicated book folder yet.
            author, title = parts[0], best.stem
        else:
            # A loose file directly under the root.
            author, title = None, best.stem
        haystack = f"{author or ''} {title} {rel.as_posix()}".casefold()
        if needle and needle not in haystack:
            continue
        try:
            size = best.stat().st_size
        except OSError:
            continue
        entries.append(
            Ebook(
                id=_ebook_id(root, best),
                title=title,
                author=author,
                relpath=rel.as_posix(),
                size=size,
                ext=best.suffix.lower(),
            )
        )
    entries.sort(key=lambda e: ((e.author or "").casefold(), e.title.casefold()))
    return entries[:limit]


def resolve_ebook(root: Path, ebook_id: str) -> Path:
    """Turn an opaque search-result id back into a real file, safely.

    The id is matched against a hash computed fresh for every file the walk
    finds — it is never used to build a path — so a value like
    "../../etc/passwd" or an absolute path simply matches nothing and raises
    EbookNotFound. The containment check below is defense in depth for the
    one case hashing alone doesn't cover: a symlink INSIDE the root pointing
    outside it, whose `path` is honest but whose `path.resolve()` is not.
    """
    root_resolved = root.resolve()
    for path in _iter_ebook_files(root):
        if _ebook_id(root, path) != ebook_id:
            continue
        resolved = path.resolve()
        if resolved != root_resolved and root_resolved not in resolved.parents:
            raise EbookNotFound(ebook_id)
        return resolved
    raise EbookNotFound(ebook_id)

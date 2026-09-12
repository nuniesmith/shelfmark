# Bindery

Organize messy **audiobook** and **ebook** dumps into a clean Author / Year - Title layout for [Audiobookshelf](https://www.audiobookshelf.org/) (audio) or a simple ebook library.

## Layout

**Audiobooks**

```text
Author Name/
  2021 - Book Title/
    01.mp3
    02.mp3
    cover.jpg
```

**Ebooks**

```text
Author Name/
  1818 - Frankenstein/
    Frankenstein.epub
    cover.jpg
```

## Requirements

- Python 3.9+
- Optional: `unrar` / `unar` / `7z` for RAR/7z archives
- Optional Python packages (see `requirements.txt`): `rarfile`, `mutagen`

## Quick start

```bash
chmod +x run.sh

# Preview only (safe)
./run.sh "/path/to/messy/dump" --dry-run

# Write a clean library. NOTE: this MOVES the files out of the dump.
# Add --copy if you want the dump to survive.
./run.sh "/path/to/messy/dump" \
  --dest "/path/to/Audiobooks-clean" \
  --apply

# The same thing, taking nothing out of the dump
./run.sh "/path/to/messy/dump" \
  --dest "/path/to/Audiobooks-clean" \
  --copy --apply

# Ebooks only
./run.sh "/path/to/ebook/dump" \
  --media ebook \
  --dest "/path/to/Books-clean" \
  --apply

# Both audio and ebooks in one pass
./run.sh "/path/to/mixed" --media both --dest "/path/to/Library" --apply
```

In-place reorganize (no separate dest):

```bash
./run.sh "/path/to/library" --format year-title --apply
```

## CLI options

| Flag | Description |
|------|-------------|
| `source` | Messy dump folder (required) |
| `--dest DIR` | Clean output library. Omit to edit source in place. Files are **moved** unless `--copy` |
| `--apply` | Actually move/copy files (default is dry-run plan) |
| `--dry-run` | Force plan-only |
| `--copy` | Copy into dest instead of moving. Nothing is removed from the source: no trashing, archives stay put, empty folders are left alone. Archives are still **extracted in place**, so a dump containing zips gains the unpacked folders |
| `--media auto\|audio\|ebook\|both` | What to organize (default: `auto`) |
| `--format year-title\|title-year\|title` | Folder naming (default: `year-title` → `1999 - Title`) |
| `--keep-names` | Audio tracks as `01 - Chapter.mp3` instead of `01.mp3` |
| `--keep-images` | Keep all images, not only cover |
| `--yes` / `-y` | Skip confirmation. Required when there is no terminal (a pipe, cron, a script) — without it, `--apply` refuses rather than guessing |
| `--trash-name NAME` | Junk folder name under source (default: `trash`) |
| `--self-test` | Run built-in smoke tests |

```bash
./run.sh --self-test
./run.sh --help
```

## What it does

1. Scans the dump for audio (`.mp3`, `.m4b`, …) and ebooks (`.epub`, `.mobi`, `.pdf`, …)
2. Extracts zip/rar/7z (multipart RAR treated as one set)
3. Merges Disc/CD and “Part One” style section folders into one book
4. Parses author / title / year from folders, filenames, and tags when possible
5. Unwraps numbered listicle packs — `Top 100 Sci-Fi Books/43 - Title - Author - Year/`
   becomes `Author/Year - Title/`, rather than filing all 100 books under an
   author called "Top 100 Sci-Fi Books"
6. Applies known title/author fixes (e.g. missing King years, Clark → Clarke)
7. Builds `Author / Year - Title /` and renumbers audio tracks
8. Moves leftovers into `trash/`

## Supported formats

**Audio:** mp3, m4b, m4a, flac, ogg, opus, aac, wma, wav, mp4, m4v  

**Ebook:** epub, mobi, azw, azw3, pdf, cbz, cbr, fb2, djvu, lit  

**Archives:** zip, rar, 7z, tar, tar.gz  

## Repairing a library Audiobookshelf has already scanned

Audiobookshelf writes a `metadata.json` into each book folder and reads it in
preference to the folder structure. If it scanned the library while a pack was
still mis-filed, fixing the folders afterwards changes nothing in the UI — the
stale author and the raw title are in those files.

`src/fix_metadata.py` repairs them in place, without touching a single audio
file. The author comes from the folder, which the organiser has already made
correct; the title is parsed out of the existing metadata title rather than the
folder name, so punctuation the filesystem cannot hold survives
(`2001: A Space Odyssey`, not `2001 A Space Odyssey`).

```bash
# preview
python3 src/fix_metadata.py "/path/to/Audiobooks"

# write, keeping a .bak beside each file
python3 src/fix_metadata.py "/path/to/Audiobooks" --apply --backup
```

Only files whose `authors` list is empty are considered, so books that are
already right are never rewritten and the command is safe to run twice.

**If Audiobookshelf has already re-scanned the library, the authors will not be
empty — they will hold the invented name.** ABS writes its own database back
into these files on scan, so a pack it has seen carries e.g.
`"authors": ["Top 100 Sci-Fi Books"]` rather than `[]`. Name that value
explicitly to have it rebuilt from the folder:

```bash
python3 src/fix_metadata.py "/path/to/Audiobooks" \
  --replace-author "Top 100 Sci-Fi Books" --apply --backup
```

`--replace-author` is repeatable, and any author *not* named is left alone — so
real pen names such as Richard Bachman are never collapsed into the folder's
author.

Afterwards, force Audiobookshelf to re-read the files. A restart is not enough:
it re-initialises the watcher but does not re-read metadata for items it already
knows. Use **Library → ⋮ → Force Re-Scan**, or the API:

```bash
curl -X POST -H "Authorization: Bearer $ABS_API_KEY" \
  "https://your-abs-host/api/libraries/$LIBRARY_ID/scan?force=1"
```

## Customizing known titles and authors

Edit `src/main.py`:

- `KNOWN_TITLES` — map messy titles → `(Author, Year, Canonical Title)`
- `AUTHOR_ALIASES` — map alternate spellings → preferred author name

## Tips

- Always `--dry-run` first on a large library.
- Re-running over an already-clean library is safe and is how you repair one
  that was filed wrong. A second pass reads its own `Year - Title` folders back
  without losing the year, so you can point it at the library itself:
  `./run.sh "/path/to/Audiobooks" --format year-title --dry-run`
- Use `--dest --copy` if you want to keep the original dump. `--dest` on its own
  moves the files out of it.
- `--copy` removes nothing — not the junk, not the archives, not empty folders.
  The one thing it adds is the extracted contents of any archive, unpacked
  beside it, because the organiser has to be able to see inside. If even that is
  too much, copy the dump somewhere else first and run against the copy.
- A dry run cannot see inside archives, so on a dump of zips it will report
  `Books 0`. With `--apply` the tool extracts, re-plans, and asks again before
  moving anything.
- Install `unrar` for RAR sets: `sudo apt install unrar`
- Point Audiobookshelf at the **clean** folder, not the dump or `trash/`.
- After in-place runs, review and delete `trash/` before library scan.

## Project layout

```text
run.sh              # venv + deps + entrypoint
requirements.txt    # optional: rarfile, mutagen
src/main.py         # organizer
src/fix_metadata.py # repair Audiobookshelf's metadata.json in place
README.md
.gitignore
```

## License

Use and modify freely for personal library management.

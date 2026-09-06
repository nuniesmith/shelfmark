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

# Write a clean library (source left intact when --dest is set)
./run.sh "/path/to/messy/dump" \
  --dest "/path/to/Audiobooks-clean" \
  --apply

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
| `--dest DIR` | Clean output library. Omit to edit source in place |
| `--apply` | Actually move/copy files (default is dry-run plan) |
| `--dry-run` | Force plan-only |
| `--copy` | Copy into dest instead of moving |
| `--media auto\|audio\|ebook\|both` | What to organize (default: `auto`) |
| `--format year-title\|title-year\|title` | Folder naming (default: `year-title` → `1999 - Title`) |
| `--keep-names` | Audio tracks as `01 - Chapter.mp3` instead of `01.mp3` |
| `--keep-images` | Keep all images, not only cover |
| `--yes` / `-y` | Skip confirmation |
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
5. Applies known title/author fixes (e.g. missing King years, Clark → Clarke)
6. Builds `Author / Year - Title /` and renumbers audio tracks
7. Moves leftovers into `trash/`

## Supported formats

**Audio:** mp3, m4b, m4a, flac, ogg, opus, aac, wma, wav, mp4, m4v  

**Ebook:** epub, mobi, azw, azw3, pdf, cbz, cbr, fb2, djvu, lit  

**Archives:** zip, rar, 7z, tar, tar.gz  

## Customizing known titles and authors

Edit `src/main.py`:

- `KNOWN_TITLES` — map messy titles → `(Author, Year, Canonical Title)`
- `AUTHOR_ALIASES` — map alternate spellings → preferred author name

## Tips

- Always `--dry-run` first on a large library.
- Use `--dest` so the original dump stays untouched.
- Install `unrar` for RAR sets: `sudo apt install unrar`
- Point Audiobookshelf at the **clean** folder, not the dump or `trash/`.
- After in-place runs, review and delete `trash/` before library scan.

## Project layout

```text
run.sh              # venv + deps + entrypoint
requirements.txt    # optional: rarfile, mutagen
src/main.py         # organizer
README.md
.gitignore
```

## License

Use and modify freely for personal library management.

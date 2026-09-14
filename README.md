# Shelfmark

A shelfmark is the code a library puts on a book to say where it belongs on the
shelf. This tool assigns one to every book in a messy dump: it reads author,
title and year out of whatever the folder happens to be called, and files the
result as `Author / Year - Title /` for
[Audiobookshelf](https://www.audiobookshelf.org/) or a plain ebook library.

It also repairs libraries Audiobookshelf has *already* scanned and got wrong,
which is a different problem and needs a different tool — see
[Repairing a library](#repairing-a-library-audiobookshelf-has-already-scanned)
below.

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

- Python 3.13+
- Optional: `unrar` / `unar` / `7z` for RAR/7z archives (the Docker image
  includes `unrar-free` and 7-Zip)
- Python packages installed by `run.sh` (see `requirements.txt`): `rarfile`,
  `mutagen`

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

### Docker

The image currently packages the safe CLI organizer. It is a useful staging
container while the API, worker, and Discord services are being built.

```bash
docker build -t shelfmark:dev .

# Preview a dump (read-only)
docker run --rm \
  -v "/path/to/messy/dump:/incoming:ro" \
  shelfmark:dev shelfmark /incoming --dry-run

# Apply into separate audiobook and ebook roots
docker run --rm \
  --user "$(id -u):$(id -g)" \
  -v "/path/to/messy/dump:/incoming" \
  -v "/mnt/1tb/audiobooks:/audiobooks" \
  shelfmark:dev shelfmark /incoming --dest /audiobooks --apply --yes
```

The container includes `unrar-free` and 7-Zip for archive formats. Bind mounts
must be writable by the container user for an apply run. The eventual Freddy
compose deployment will mount the canonical library, incoming, work,
quarantine, and data directories separately and run the API/worker services
with the matching host UID/GID.

The image also installs `shelfmark-api` and `shelfmark-worker` entry points for
the service layer. They are intentionally not added to the live Freddy Compose
file yet; use the example service definition as the staging starting point:

```bash
cp .env.example .env
cp docker-compose.shelfmark.example.yml docker-compose.shelfmark.yml
docker compose -f docker-compose.shelfmark.yml up -d --build
```

The initial internal API exposes health checks, job submission/status, current
Audiobookshelf library search, Prowlarr release search, and asynchronous
release grabs. Set `SHELFMARK_API_TOKEN` before using anything beyond
`/healthz`:

```bash
curl http://127.0.0.1:8110/healthz
curl -H "Authorization: Bearer $SHELFMARK_API_TOKEN" \
  "http://127.0.0.1:8110/api/v1/releases/search?q=Ursula%20Le%20Guin"
```

The API and worker share only the SQLite database and mounted staging/library
paths. Provider calls still require the corresponding `AUDIOBOOKSHELF_*` and
`PROWLARR_*` settings in `.env`.

### Discord bot

Commands: `/library-search`, `/release-search` (with grab buttons),
`/downloads`, `/job`, `/metadata-match`, `/scan`, `/organize-preview`. Each one
is deferred before any network call and answered ephemerally, so the
interaction token is never used as a long-running task channel.

**Invite it with zero permissions.** Scopes `bot` and
`applications.commands`, permission integer `0`. Every reply is an ephemeral
interaction response, which needs no channel permission at all. It also
requests **no privileged intents** — `Intents.none()` plus `guilds` — so there
is nothing to justify in the developer portal and no verification gate later.

| Variable | What it does |
|---|---|
| `DISCORD_BOT_TOKEN` | Required. Without it the bot logs why and idles. |
| `SHELFMARK_API_TOKEN` | Required — the bot calls the API with it. |
| `SHELFMARK_DISCORD_GUILD_ID` | Syncs commands to one guild, which is instant. Without it they sync globally and can take up to an hour to appear. |
| `SHELFMARK_DISCORD_ALLOWED_ROLE_IDS` | Comma-separated role IDs permitted to use the bot. **Empty means nobody.** |

To collect the IDs, turn on **User Settings → Advanced → Developer Mode**, then
right-click the server for its ID and a role (in **Server Settings → Roles**)
for its ID.

The role itself needs **no Discord permissions**. It is used only as a
membership tag — the check is a set intersection on role IDs and never reads a
permission bit. Granting it anything real would hand those people server powers
the bot will never consult.

Restrict the bot to one channel through **Server Settings → Integrations →
Shelfmark → Manage** rather than in code. Discord enforces that before the
interaction is ever sent.

**The allow-list fails closed.** With `SHELFMARK_DISCORD_ALLOWED_ROLE_IDS`
unset, every command is refused with a message saying so. It used to permit
everyone, which meant a bot deployed before its roles were configured let any
member of the server run any command — `/organize-preview` included, which
takes an arbitrary absolute path and reports what is at it.

**Missing configuration does not crash-loop.** The container runs under
`restart: unless-stopped`, so exiting on a missing token would respawn forever
and scroll the one useful message out of the log. Instead the bot prints what
is missing and idles until the next deployment supplies it. A token Discord
*rejects* is treated the same way, rather than retried against the login
endpoint until the application is rate-limited.

## Transfers from Sullivan

`POST /api/v1/transfers/pull` queues a `transfer_completed` job: rsync pulls
the tree through the restricted `shelfmark-sync` account, waits for it to
settle, and then verifies it.

**Verification is a second, independent pass** — `rsync --checksum --dry-run`
against the source. rsync already guards each transfer with its own rolling
checksum, so this is not about a corrupted wire; it is about everything after,
such as a truncated write or a file that changed on either side between the
pull and the import. The organiser is destructive, so it must not be handed a
tree that no longer matches. A mismatch fails the job.

Note that `rsync --dry-run` **exits 0 whether or not anything differs** — the
differences are in its output. Reading the exit status would report every
transfer as verified, corrupt ones included.

Host keys: the worker keeps a persistent `known_hosts` (default
`/data/known_hosts`, override with `SULLIVAN_SSH_KNOWN_HOSTS`) and uses
`StrictHostKeyChecking=accept-new`, so Sullivan's key is trusted on first
contact and pinned thereafter. Set `SULLIVAN_SSH_STRICT_HOST_KEY=true` once
that file holds a key you have checked and even first contact must match.

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
| `--quarantine DIR` | Where to hold the partial output of a failed extraction (default: `<source>/.shelfmark-quarantine`). The archive itself is never moved there |
| `--trash-name NAME` | Junk folder name under source (default: `trash`) |
| `--trash-unknown` | Move unrecognized non-media files to trash (default is to leave them for review) |
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
8. Preserves known Audiobookshelf/ebook metadata sidecars and leaves unknown files for review
9. Moves recognized junk into `trash/` (use `--trash-unknown` to opt into moving other files)

## Supported formats

**Audio:** mp3, m4b, m4a, flac, ogg, opus, aac, wma, wav, mp4, m4v  

**Ebook:** epub, mobi, azw, azw3, pdf, cbz, cbr, fb2, djvu, lit  

**Archives:** zip, rar, 7z, tar, tar.gz  

### What happens when a write is interrupted

Files are written to a temporary name inside the destination folder, flushed,
and renamed into place. A track in the library is therefore complete or absent
— never a truncated file wearing the right name, which nothing downstream can
distinguish from a real one.

This is also what stops the duplicate-track failure. A half-written `01.mp3` is
not identical to its source, so a retry used to decline to overwrite it and
write the good copy beside it as `01 (2).mp3` — leaving the library holding
both, with the broken one sorting first.

Moves within a single filesystem use `rename` directly: atomic, and no data is
copied at all. Only a move that crosses a filesystem boundary has to stage.

A **new** book — one whose destination folder does not exist yet — is also
staged whole before it appears. Every track and sidecar is assembled into a
private directory beside the destination (`.shelfmark-work-books`, a separate
marker from the one archive extraction uses, so the two never contend over the
same working directory) and the finished folder is handed over with one
`rename`. Either the whole book appears, or nothing does — there is no state
where the library holds a folder missing most of its tracks.

In move mode, a track on the same filesystem as the library is staged with a
`rename` too, not a copy — this is why reorganising a library in place
(`--dest` equal to the source) stays the metadata-only operation it always
was, rather than turning into a full read-and-rewrite of every file. Only a
source on a different filesystem is actually copied, the same case `move_file`
already has to handle, and `--copy` always copies, because there the source
must survive regardless.

An import killed partway (out of disk, a bad track, an operator's Ctrl-C)
loses nothing: every rename already made into staging is reversed — a rename
back costs exactly what making it did — and the staging directory is removed.
If reversing one of those renames also fails, deleting the directory would
destroy the only remaining copy of that track, so it is left in place instead
and its path is printed for manual recovery.

Filing more files into a book that is **already** on disk — a repeat run, or
new tracks arriving for one already imported — still writes straight in, file
by file, exactly as before: that folder is already visible to a scan either
way, so staging buys nothing there, and `rename` cannot merge into a
destination that already has files in it regardless.

### What happens when an archive is broken

Archives are unpacked into a private staging directory and moved into place
only once the whole tree has extracted and been checked. Either the finished
folder appears, or nothing does — there is no state in which part of an archive
is sitting in the dump looking like a book.

That matters because the failure is otherwise silent in both directions.
Python writes each member of a zip to disk and verifies its checksum
afterwards, so a corrupt member fails with the files already written: real,
plausible-looking media, one track quietly damaged. The run stops and reports
the error, but the fragments used to stay behind, and nothing later knows where
they came from — the next pass over that dump files them as an ordinary book.

When an extraction fails:

- the **archive is left exactly where it is**. It is the only remaining copy of
  that content, and the run may simply need repeating with more disk free.
- the partial output goes to `.shelfmark-quarantine/` (override with
  `--quarantine`), timestamped, since it is sometimes the only evidence of what
  was wrong with the archive.
- the run exits non-zero and moves nothing into the library.

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
  beside it, because the organiser has to be able to see inside. A failed
  extraction adds nothing at all. If even a successful one is too much, copy the
  dump somewhere else first and run against the copy.
- A dry run cannot see inside archives, so on a dump of zips it will report
  `Books 0`. With `--apply` the tool extracts, re-plans, and asks again before
  moving anything.
- Install `unrar` for RAR sets: `sudo apt install unrar`
- Point Audiobookshelf at the **clean** folder, not the dump or `trash/`.
- After in-place runs, review `trash/` and the warnings before library scan.

## Project layout

```text
run.sh              # venv + deps + entrypoint
requirements.txt    # optional: rarfile, mutagen
pyproject.toml      # package metadata and console entry points
Dockerfile          # Python 3.13 CLI image (API image will supersede this)
.dockerignore       # excludes credentials, state, and local build files
src/main.py         # organizer — assigns the shelfmark
src/fix_metadata.py # repair Audiobookshelf's metadata.json in place
src/shelfmark_service/ # API, SQLite queue, and worker foundation
tests/              # regression tests for organizer safety and idempotence
LICENSE             # MIT
README.md
.gitignore
```

Previously `src/utils/books` in
[nuniesmith/scripts](https://github.com/nuniesmith/scripts); the commit history
moved with it.

## License

[MIT](LICENSE). © nuniesmith 2026.

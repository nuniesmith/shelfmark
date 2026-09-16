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
Audiobookshelf library search, Prowlarr release search, asynchronous release
grabs, and an ebooks index (`/api/v1/ebooks/search`,
`/api/v1/ebooks/{id}/download`) that walks `SHELFMARK_EBOOKS_ROOT` directly.
Set `SHELFMARK_API_TOKEN` before using anything beyond `/healthz`:

```bash
curl http://127.0.0.1:8110/healthz
curl -H "Authorization: Bearer $SHELFMARK_API_TOKEN" \
  "http://127.0.0.1:8110/api/v1/releases/search?q=Ursula%20Le%20Guin"
```

The API and worker share only the SQLite database and mounted staging/library
paths. Provider calls still require the corresponding `AUDIOBOOKSHELF_*` and
`PROWLARR_*` settings in `.env`.

### Discord bot

`/release-search` and `/ebook-request` both search **books only** by default.
Prowlarr indexes everything, so an unfiltered search for "dune" returns
`Dune Part Two 2024 BluRay 1080p` (category 2050) and a Car SOS episode about
a dune buggy (5010) before it returns a single book. Pass `book_only=false` to
the API route to search every category deliberately; no command does.

The filter is `PROWLARR_BOOK_CATEGORIES` (default `7000`) rather than a
hardcoded `7020`/EBook, because an indexer that does not advertise 7020 would
silently return nothing at all.

Commands: `/library-search`, `/release-search` (with grab buttons),
`/ebook-search` (with send-to-phone buttons), `/ebook-request` (with grab
buttons), `/downloads`, `/job`, `/metadata-match`, `/scan`,
`/organize-preview`. Each one is deferred before any network call and
answered ephemerally, so the interaction token is never used as a
long-running task channel.

**Ebooks.** Audiobookshelf has no ebook library, and running a separate
reader app just to browse files isn't wanted, so Shelfmark indexes
`SHELFMARK_EBOOKS_ROOT` itself:

- `/ebook-search <query>` walks the ebooks root, matching on author, title,
  and filename, and shows up to 5 results with a **Send** button per result.
  Pressing one fetches the file and attaches it to an ephemeral reply — open
  it from Discord on a phone and it lands in whichever app is registered for
  that format. When a book has more than one file (an epub next to a pdf,
  say), the better format wins automatically, in `EBOOK_PREF` order.
- `/ebook-request <query>` searches Prowlarr restricted to the configured
  book categories (`PROWLARR_BOOK_CATEGORIES`, default `7000`) and offers the
  same grab buttons as `/release-search`.
- Discord refuses attachments over 10 MB on an unboosted server. The size is
  checked and reported in plain language (naming the book and its size)
  *before* any upload is attempted, rather than surfacing as a failed
  Discord API call. Raise the ceiling with
  `SHELFMARK_DISCORD_MAX_ATTACHMENT_MB` if the server is boosted.
- A search result's id is an opaque token, never a filesystem path. The
  download endpoint re-derives it from the files it finds under the ebooks
  root and only serves a match that resolves back inside that root — a
  request built from someone else's search result, or a raw path, matches
  nothing.

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
| `SHELFMARK_DISCORD_MAX_ATTACHMENT_MB` | `/ebook-search`'s file-size ceiling before Discord would refuse the upload. Default `10`; raise it if the server is boosted. |
| `PROWLARR_BOOK_CATEGORIES` | Categories `/ebook-request` restricts to. Default `7000`, since a typical indexer advertises the general Books bucket rather than 7020 (EBook) specifically. |

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

## Automatic download pipeline

Grabbing a release used to be the end of the automated part: a human had to
notice qBittorrent finished, call `/transfers/pull`, run the organizer, and
trigger a library scan by hand. The worker now does all four stages on its
own once a grab lands.

**The grab itself goes straight to qBittorrent, not through Prowlarr.**
`grab_release` used to call `ProwlarrClient.grab()`, which POSTs to
Prowlarr's own `/api/v1/search` and hands the release to whatever download
client PROWLARR itself has configured. On a deployment with one configured
client, every grab lands in that client's own category — not necessarily
`QBITTORRENT_CATEGORY` — so the reconciler above never sees it and the
whole pipeline sits inert with releases stuck in qBittorrent forever.
`grab_release` now calls `QBittorrentClient.add_urls()` directly, in the
exact same `QBITTORRENT_CATEGORY` the reconciler reads, so the two settings
can never drift apart. It takes the release's `downloadUrl` (or `magnetUrl`
if that's what the release carries instead — a magnet needs no proxy and is
passed through byte for byte). A private-tracker `downloadUrl` looks like
`http://<prowlarr-host>:9696/1/download?apikey=...&link=...`: Prowlarr
fetches the actual `.torrent` from the tracker using its own credentials and
serves it back, which is what lets qBittorrent fetch a private-tracker
release (IPTorrents, here) with no tracker auth of its own. But the host in
that URL is Prowlarr's own view of itself, and that is not guaranteed to be
reachable from qBittorrent's network namespace — on this deployment,
Prowlarr's `sullivan:9696` hostname refused the connection from inside the
qBittorrent container, while `prowlarr:9696` (the name both resolve on
their shared Docker network) answered fine. So only the scheme and host are
rewritten, to `QBITTORRENT_PROWLARR_BASE_URL` (default `http://prowlarr:9696`,
matching `PROWLARR_URL`'s own container-name convention); the path and the
entire query string are left untouched, since `apikey` and `link` both live
there and are what actually authorizes the download. Passing the original
host through unchanged would not raise an error — qBittorrent would simply
accept the add and never fetch anything.

**A reconciler, not a per-torrent watcher.** The worker enqueues its own
`reconcile_downloads` job on a timer (default every
`SHELFMARK_RECONCILE_INTERVAL_SECONDS=60`, driven from the same `while`
loop in `worker.py` that already re-queues stale jobs — no threads, no
second process). Each pass asks qBittorrent "what's finished in the
`QBITTORRENT_CATEGORY` (default `shelfmark-books`) that I haven't imported
yet?" and acts on the answer, rather than tracking one grab end-to-end in a
long-lived task. A long-lived watcher would be stranded by a worker restart
mid-watch — and this process restarts on every deploy — whereas a
reconciler that re-derives its answer from scratch each pass self-heals
from any failure, including one of its own.

**"Complete" is a qBittorrent state, not just 100% progress.** A torrent
sitting at `progress == 1` can still be re-checking its files
(`checkingUP`/`checkingResumeData`, e.g. after qBittorrent's own restart) or
being moved to its final save path (`moving`). Both report full progress
while the content is not yet safe to pull. Only qBittorrent's seeding states
— `uploading`, `stalledUP`, `queuedUP`, `pausedUP`/`stoppedUP`, `forcedUP`
(both the pre- and post-5.x names) — count as genuinely done.

**Idempotency key: the torrent hash.** A `reconciled_torrents` table (schema
migration 3, following the same `schema_migrations`-gated pattern as
migration 2's `error_code` column) records every hash the reconciler has
ever claimed. Claiming a hash and enqueuing its first job commit in one SQLite
transaction, so a crash between the two can never happen — either both took
effect or neither did, and the next reconcile pass claims a half-written hash
fresh instead of dropping it or importing it again. Since qBittorrent reports
a finished, still-seeding torrent as complete forever, this is what stops
every future pass from re-importing the same book, restarts included.

**Three separate jobs, chained on success.** The reconciler enqueues
`transfer_completed`; once it reports `verified: true`, its success enqueues
`organize_apply`; that job's success enqueues `library_scan` (only for the
audio side, and only if Audiobookshelf is configured — see below). Each stage
is an ordinary row in `jobs`, independently retryable and visible through
`/job` with its structured error code — a failure at any stage stops the
chain there rather than leaving a partially-organized book. An unverified
transfer never reaches the organizer: it is destructive, and
`transfer_completed` already fails the job outright on a checksum mismatch
rather than returning normally.

**`organize_apply` always gets an explicit `source` and `dest`; it never
defaults either.** `source` is `SHELFMARK_INCOMING_ROOT` — the whole
staging root, not the just-landed book's own subfolder — because `dest`
falling back to `source` (its default when omitted) means "organize"
silently does nothing but rename in place, and pointing `source` at the
book folder itself removes the very folder name `build_plan` reads
author/title/year from, scattering every track into its own book with no
author. Scanning the whole root instead of just this torrent's folder is
safe because the worker is single-process and runs one job at a time
(`Worker.run_once`): nothing else can be mid-write into it while an organize
job runs, and `transfer_completed` never lands a folder there until its own
stability wait and checksum verify have both passed — so everything under
it is always either a complete, verified book, or not there yet. `dest` is
one of `SHELFMARK_AUDIOBOOKS_ROOT` / `SHELFMARK_EBOOKS_ROOT` — one
`organize_apply` job per configured root, each scoped with `media: audio` or
`media: ebook`, since a single download can hold both and `build_plan` takes
only one `dest` per call. A pass whose media type is not present in that
torrent simply plans zero books; only a pass that actually organizes
something chains onward (into `library_scan` for audio, or straight to a
completion notice for ebooks — Audiobookshelf has no ebook library, so
there is nothing to scan on that side). If neither root is configured, the
chain stops with a Discord warning rather than falling back to anything.

**Notifications, without the worker depending on discord.py.** A Discord
webhook (`SHELFMARK_DISCORD_WEBHOOK_URL`) is a plain HTTP POST, so the
worker posts to it directly through the same `HttpClient` every other
integration in this file uses — no gateway connection, no `discord.py`
import in the worker process. A completed book and a failed stage each get
a message; a Discord outage only ever logs a warning; it never fails or
retries the job that triggered it, since the jobs table (not Discord) is
the source of truth.

**The off switch.** `SHELFMARK_DOWNLOAD_AUTOMATION_ENABLED=false` (default
`true`) stops the periodic reconcile from ever being enqueued. Existing
manual paths — `/transfers/pull`, `/organize-preview`, `/scan` — are
unaffected either way.

**Worker liveness monitoring.** Running unattended only helps if a dead or
wedged worker is actually visible somewhere. `jobs.heartbeat_at` only exists
on a RUNNING job row, so a worker sitting idle with an empty queue (the
normal state, most of the time) writes nothing there — indistinguishable
from a crashed one. The worker now records its own liveness in a small
`worker_liveness` table (schema migration 4) once per main-loop iteration —
including when idle — keyed by `worker_id`, so a second worker never
clobbers the first's row. Two ways this surfaces:

- `GET /readyz` includes a `worker` object (`status`: `unknown` / `ok` /
  `busy` / `stale`, plus `worker_id`, `last_seen_at`, `age_seconds`) and
  returns **503** only for `stale`. A 200 with "stale" in the body would be
  invisible to an uptime monitor that only reads the status code, so a
  stale worker fails the same way `/readyz` already fails for a missing
  media root — and it stays on `/readyz`, not `/healthz`: the API process
  itself is fine even when the worker is dead, and `shelfmark-api`'s own
  Docker healthcheck targets `/healthz` specifically, so a stale worker
  never makes Docker think the *API* container needs restarting.
  **`unknown`** (no row at all) is reported whenever no worker has ever
  ticked yet — a database from before migration 4, or a fresh deploy in the
  first fraction of a second before the worker container completes its
  first loop — and never fails `/readyz`.
- The `shelfmark-worker` container has its own Docker `healthcheck` now
  too: `shelfmark-worker-healthcheck`, a console entry point (alongside
  `shelfmark-api`/`shelfmark-worker`/`shelfmark-bot`) installed by
  `pyproject.toml` and wired into `docker-compose.yml`'s `test:` as one
  word. The image is python-slim with no `curl` (only `openssh-client`,
  `rsync`, `7zip`, and `unrar-free` are installed, for the Sullivan
  transfer), and the worker serves no HTTP of its own, so it reads
  `worker_liveness` straight out of SQLite instead.

**One classifier, not two.** Both surfaces above call the exact same
`Database.worker_liveness_status(stale_after_seconds, running_job_bound_seconds)`
— `/readyz` from `api.py`, `shelfmark-worker-healthcheck` from
`worker.py`'s `check_liveness_cli`. Early on the healthcheck was a separate
inline `python -c` one-liner that only checked liveness age, and that
mismatch actually shipped for a moment: `docker ps` reported
`shelfmark-worker` as `unhealthy` during a legitimate long transfer even
after `/readyz` had already been fixed to report `busy` for the exact same
situation. Two health signals disagreeing is worse than either alone —
`docker ps` is the reflex check when something seems wrong, and a
container that routinely shows unhealthy while working normally teaches
the operator the column means nothing. There is now exactly one
implementation of the rule, so the two cannot drift apart again.

**The busy-worker case, and the bound that still catches it.**
`Worker.run_once` runs one job to completion synchronously — no threads —
so a big `transfer_completed` pull, its 30s settle-wait, and its
`--checksum` verify pass can together outlast `worker_liveness_stale_seconds`
(default `180`, three times the reconcile interval) with the worker
perfectly healthy the whole time; nothing refreshes `worker_liveness` until
that job returns. Reporting that as `stale` would page for a routine
import, and an alert that fires when nothing is wrong trains whoever gets
paged to ignore it — worse than no check at all. So a stale
`worker_liveness` row is not immediately `stale`: the classifier also
checks the `jobs` table for a `running` job. One that started within
`SHELFMARK_TRANSFER_TIMEOUT_SECONDS` (the longest any single job is meant
to take, already configured) reports `busy` and stays healthy — that job's
own `started_at` is itself evidence someone is home. A `running` job
older than that bound is no longer credible evidence of anything: either
it genuinely overran its own ceiling, or the worker died mid-job and left
the row stuck in `running` forever — exactly the "wedged worker hides
behind a permanently running job" failure this bound exists to still
catch, so that case reports `stale` regardless.

Writing the liveness row on every loop iteration is throttled to at most
once per `SHELFMARK_WORKER_POLL_SECONDS` (default 2s): when idle, the loop
already sleeps that long between iterations so nothing changes; the
throttle only matters when many quick jobs run back to back with no sleep
in between, where it caps this at one small SQLite upsert per poll interval
instead of one per job.

**Job retention.** Left unpruned, `jobs` and `audit_events` grow forever:
measured on the live database, `reconcile_downloads` alone reached 1,290 of
1,308 job rows (98.6%) after about a day and a half, and a fresh check a
few hours later found the last 40 jobs in a row were reconciler ticks —
completely burying the pipeline history a human actually wants to read.
Two separate fixes, for two separate costs:

- `GET /api/v1/jobs` now defaults to **excluding** `reconcile_downloads`
  jobs — `?include_reconciler=true` opts back in (e.g. to confirm the
  reconciler is alive at all). This is what fixes readability; it does not
  by itself bound disk use.
- The worker's main loop (same single-threaded loop as
  `_maybe_enqueue_reconcile`/`_maybe_record_liveness` — no threads, no
  second process) now also runs `_maybe_sweep_retention`, throttled to once
  per `SHELFMARK_RETENTION_SWEEP_INTERVAL_SECONDS` (default 1h), which
  deletes TERMINAL jobs (`succeeded`/`failed`/`cancelled` only — a `queued`
  or `running` job is never touched, at any age) past a tier-specific
  window:

  | Tier | Window | Why |
  |------|--------|-----|
  | `reconcile_downloads`, succeeded, claimed nothing | 1 hour | The steady-state tick — one every `SHELFMARK_RECONCILE_INTERVAL_SECONDS` forever, whether or not there's anything new. This is the 98.6%. It carries no information once its own hour has passed; an hour is enough to eyeball "is the reconciler actually running" |
  | `reconcile_downloads`, claimed something / failed / cancelled | 30 days | Not noise, but its useful detail (which torrent, what error) already lives in the `transfer_completed`/`organize_apply`/`library_scan` chain it triggered and that chain's own audit trail — so it gets that chain's own window, not a separate longer one |
  | Every other job kind (`grab_release`, `transfer_completed`, `organize_preview`/`apply`, `metadata_*`, `library_scan`) | 90 days | A real, human- or pipeline-triggered action, not a tick. Long enough to answer "what happened to the book I requested last month" |
  | ...and it FAILED | 180 days | Failures are rarer and worth noticing a pattern in (the same release failing `organize_apply` three times this month is a signal) |

  Each window is its own `SHELFMARK_RETENTION_*_SECONDS` env var if the
  defaults above need adjusting on a given deployment.

Whether a `reconcile_downloads` pass "claimed something" lives inside its
`result_json` (`claimed_jobs`), which this reads with plain `json.loads` —
the same way every other job result is read in this codebase — rather than
leaning on SQLite's own JSON functions being compiled into whatever build
happens to be deployed. `audit_events` rows are deleted in the same
transaction as the job row they describe (every row in that table is
`target_type='job'`/`target_id=<job id>` — there has never been another
`target_type`), so an audit trail never outlives the job it is about.
Deletes are batched (500 rows per transaction, a scan cap of 5,000 rows for
the JSON-inspecting tier) rather than one huge transaction per sweep: the
worker claims/heartbeats jobs on this same SQLite database from this same
single-threaded loop, so a delete holding the write lock over an entire
backlog (a worker down for a week, or this feature's first run against an
already-1,290-row database) would delay every job in flight behind it.

**`reconciled_torrents` (the reconciler's idempotency ledger, migration 3)
is never pruned by any of this, on purpose.** qBittorrent reports a
finished, still-seeding torrent as complete forever — deleting a row here
would make the reconciler treat an already-imported torrent as new,
re-transferring and re-organizing a book already in the library. That
table is meant to grow forever; see the comment on its own `CREATE TABLE`
in `db.py`.

**Known gaps, left out of scope for this feature:**

- `shelfmark-bot` still has no Docker `healthcheck` at all — only
  `shelfmark-api` and now `shelfmark-worker` do.

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
| `--quarantine DIR` | Where to hold the partial output of a failed extraction, and any incoming book whose destination already holds a *different* book under the same name (default: `<source>/.shelfmark-quarantine`). Neither the archive nor the existing library book is ever moved there |
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
6. Reads a scene release's metadata from the release folder, not the archive
   it unpacked — `Brenda.Peynado.-.The.Rock.Eaters.2021.RETAIL.EPUB.eBook-CTO/`
   holding an obfuscated `tr8e3el.rar` extracts to a `tr8e3el/` folder with no
   author, title, or year in its name at all, so that name is skipped in
   favor of the release folder one level up
7. Applies known title/author fixes (e.g. missing King years, Clark → Clarke)
8. Builds `Author / Year - Title /` and renumbers audio tracks
9. Preserves known Audiobookshelf/ebook metadata sidecars and leaves unknown files for review
10. Moves recognized junk into `trash/` — including scene-release clutter like `.nfo` and
    `file_id.diz` (use `--trash-unknown` to opt into moving other files too)
11. Refuses to merge an incoming book into a destination that already holds a *different*
    book under the same name — the whole incoming copy goes to quarantine instead (see below)

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

An import killed partway by anything **catchable** — out of disk, a bad
track, an operator's Ctrl-C — loses nothing: every rename already made into
staging is reversed (a rename back costs exactly what making it did) and the
staging directory is removed. If reversing one of those renames also fails,
deleting the directory would destroy the only remaining copy of that track,
so it is left in place instead and its path is printed for manual recovery.

A kill that cannot be caught — `SIGKILL`, an OOM kill, a power cut — is a
different case, and worse: nothing runs afterward to reverse anything, so a
move-mode import stopped that way leaves tracks renamed OUT of the source and
sitting in `.shelfmark-work-books`, with no copy left in the source and none
in the library either. Before whole-directory staging, that same kill left
some tracks in the library and the rest in the source, and the next run's
scan found and finished the book on its own. Staging trades that self-healing
away, because a dotted directory is invisible to a scan by design — so every
run checks the destination tree for one of these left over from an earlier
kill and reports it loudly on stderr (count and path) before scanning
anything. Nothing recovers it automatically — a staging directory can be
partial, and completing a partial one is exactly the half-a-book this feature
exists to prevent — so recovering the files back into the library or the
source is a manual step for now.

Filing more files into a book that is **already** on disk — a repeat run, or
new tracks arriving for one already imported — still writes straight in, file
by file, exactly as before: that folder is already visible to a scan either
way, so staging buys nothing there, and `rename` cannot merge into a
destination that already has files in it regardless.

### What happens when the destination already holds a different book

A test book named `Mary Shelley - Frankenstein (1818)` was once organised
into a library that already had `Mary Shelley/1818 - Frankenstein/` with
tracks `01.mp3`-`09.mp3`. Both parsed to the same destination folder and the
same track numbering, so the organiser wrote straight into it. Nothing was
lost — a half-written destination is impossible either way — but nothing
distinguished "the same book, again" from "a different rip of the same
book", so the new tracks landed beside the originals as `01 (2).mp3`,
`02 (2).mp3`, with no warning that two overlapping track sets now shared one
folder.

Every incoming track and ebook is now compared against whatever already
sits at its destination name. Identical content (the same file, delivered
again, or an in-place reorganise scanning its own output) is always a
no-op, exactly as before. Anything **different** at that name means the
whole incoming book — not just the colliding file — is left alone by the
library and moved to quarantine instead: printed as a warning in the plan,
naming the destination and what is already there, and held under
`--quarantine` for the operator to compare and merge by hand. The existing
book is never opened, read, or written.

This only catches a collision when the **incoming** file names actually
match names already at the destination — it checks file identity at each
shared name, not whether the two deliveries are "the same edition" as a
whole. A differing cover image or `metadata.json` alone does not trigger
this either: those commonly differ between two honest deliveries of the same
book, so only the tracks and ebook files themselves count as evidence of a
different copy.

Without `--keep-names` (the default, and what the automatic/service
pipeline always uses), tracks are unconditionally renumbered from `01`
within the incoming set alone, so a track count very different from what's
already at the destination does **not** avoid detection — the incoming
`01.mp3` still lands on the existing `01.mp3` and the mismatch is still
caught. Measured directly: delivering a genuinely missing track by itself
(e.g. just a `03.mp3` for a book that already has two tracks) gets
renumbered to `01.mp3` too, collides with the existing first track, and
quarantines the whole one-file delivery — nothing is lost, and the warning
names the path, so this fails safe, but "I have the track that was missing"
is an ordinary thing to want to do and it now needs pulling out of
quarantine by hand. The reliable workaround is to re-deliver the **whole**
book (all its existing tracks, bit-identical, plus the new one) in one
folder: every already-present track then matches on both name and content,
only the new one is actually new, and it merges into the existing folder
with no collision.

**The real gap is `--keep-names`.** It preserves whatever the source called
its files instead of renumbering, so a genuinely different copy of the same
book — ripped and named differently upstream — can produce file names that
never match anything already at the destination. Detection never fires,
and both copies are filed into the same folder, interleaved, with no
warning at all. This does not affect the automatic/unattended pipeline,
which never passes `--keep-names`; it is a real risk only for a manual,
`--keep-names` run.

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

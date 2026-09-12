# Deployment inventory

The P0 configuration record: every host path, container path, URL, port and
credential source Shelfmark touches. Sanitized — it names credentials and where
they come from, never their values.

Gathered 2026-09-12 by resolving the running containers on each host rather than
reading the checked-in Compose, because the two disagree in places that matter.
Re-derive it the same way rather than trusting this file after a change:

```bash
docker ps --format '{{.Names}}|{{.Ports}}|{{.Image}}'
docker volume ls -q | while read v; do
  docker volume inspect "$v" --format '{{.Name}} {{if .Options}}{{index .Options "device"}}{{end}}'
done
```

## Hosts

| Host | LAN | Tailscale | Role |
|---|---|---|---|
| freddy | 10.0.0.96 | 100.106.65.55 | Audiobookshelf, Shelfmark, canonical book storage |
| sullivan | 10.0.0.49 | 100.87.125.19 | Prowlarr, qBittorrent, the *arr stack, media storage |
| princess | — | 100.118.181.48 | Linode reverse proxy, TLS, public `*.7gram.xyz` |
| oryx | 10.0.0.153 | 100.113.72.63 | Unrelated to Shelfmark; the FKS trading stack |

**SSH.** `jordan@` works on freddy and sullivan by key. Sullivan's sshd listens
on the LAN and the tailnet; **freddy's answers on both but fail2ban will ban a
host that probes usernames** — it banned oryx during this work, and
`ssh -J jordan@100.87.125.19 jordan@100.106.65.55` through sullivan is the way
around it while a ban is live. Unban with
`sudo fail2ban-client set sshd unbanip <ip>`.

**Tailscale MagicDNS does not resolve inside containers.** `http://sullivan:9696`
times out from a container on freddy while `http://100.87.125.19:9696` answers in
2ms. Any container-to-container-across-hosts URL must be an IP or a
Princess-published name.

## Storage

| Path | Host | Size | Free | Holds |
|---|---|---|---|---|
| `/mnt/1tb` | freddy | 916G | 579G | Everything freddy serves |
| `/` | sullivan | 1.8T | 1.2T | OS **and the qBittorrent download tree** |
| `/mnt/media` | sullivan | 19T | 1.4T | The *arr media library |

### Freddy — canonical book storage

| Host path | Owner | Mode | Mounted as |
|---|---|---|---|
| `/mnt/1tb/audiobooks` | `jordan:actions` | 2775 | `/audiobooks` in both ABS and Shelfmark |
| `/mnt/1tb/ebooks` | `actions:actions` | 2775 | `/ebooks` in Shelfmark |
| `/mnt/1tb/shelfmark/data` | `actions:actions` | 755 | `/data` — SQLite, manifests |
| `/mnt/1tb/shelfmark/incoming` | `jordan:actions` | 2775 | `/incoming` — pulled downloads |
| `/mnt/1tb/shelfmark/work` | `jordan:actions` | 2775 | `/work` — extraction staging |
| `/mnt/1tb/shelfmark/quarantine` | `jordan:actions` | 2775 | `/quarantine` — failed jobs |
| `/mnt/1tb/audiobookshelf/config` | — | — | ABS `/config`, holds `absdatabase.sqlite` |
| `/mnt/1tb/audiobookshelf/metadata` | — | — | ABS `/metadata` |

The library is 79G, 7,231 files, 189 books, 61 authors.

**The containers run as `actions` (1001:1001), not `PUID/PGID`.** Deployment
creates `/mnt/1tb/shelfmark/data` as `actions:actions` mode 755, so only uid 1001
can write it; this host's `PUID/PGID` is 1000:1000 and reaches neither that nor
`/mnt/1tb/ebooks`. Getting this wrong crash-loops both containers on
`sqlite3.OperationalError: unable to open database file`.

### Sullivan — download side

| Host path | Mounted as | Note |
|---|---|---|
| `/media/qbittorrent/complete` | `/complete` | **On `/`, not `/mnt/media`** — 383G used, 1.2T free |
| `/media/qbittorrent/incomplete` | `/incomplete` | in-progress torrents |
| `/mnt/media/books/audiobooks` | `/audiobooks` | empty |
| `/mnt/media/ebooks` | `/ebooks` | empty |

Existing qBittorrent categories: `radarr`, `tv-sonarr`, `seed` (each with an
`_unpackerred` sibling). No Shelfmark category yet.

## Ports and URLs

| Service | Host | Port | Public name | Shelfmark reaches it via |
|---|---|---|---|---|
| Audiobookshelf | freddy | 13378 | `abs.7gram.xyz` | `http://audiobookshelf:80` |
| Shelfmark API | freddy | 8110 | `shelfmark.7gram.xyz` | — |
| Prowlarr | sullivan | 9696 | `prowlarr.7gram.xyz` | `https://prowlarr.7gram.xyz` |
| qBittorrent | sullivan | 8080 | `qbt.7gram.xyz` | `https://qbt.7gram.xyz` |
| Authentik | freddy | 9000 | `auth.7gram.xyz` | not used |
| Uptime Kuma | freddy | 3001 | `status.7gram.xyz` | will poll `/healthz` |

Neither the Prowlarr nor the qBittorrent vhost sits behind Authentik forward
auth, so API-key auth passes through the proxy unchanged.

Audiobookshelf is deliberately the container name and not its public one. It runs
in the same Compose project; `abs.7gram.xyz` would leave the machine, cross to
Toronto and return over Tailscale to reach a neighbouring container. Measured
from a container on freddy: **0.0014s by container name, 0.138s via the public
name.** The public names are right for Sullivan because that host genuinely is
elsewhere and MagicDNS is unavailable.

## Credentials

Held as GitHub Actions secrets on `nuniesmith/freddy`, written into `/home/actions/freddy/.env`
(mode 600, owned by `actions`) at deploy time. None are required to boot: every
integration is optional, and a wrong value returns a 502 from the affected route
while the rest of the service keeps serving.

| Secret | Source |
|---|---|
| `SHELFMARK_API_TOKEN` | generated — `openssl rand -hex 32` |
| `AUDIOBOOKSHELF_API_TOKEN` | ABS → Settings → Users → API Keys |
| `AUDIOBOOKSHELF_LIBRARY_ID` | `a332fc04-1385-4502-b342-cc45fb502133` |
| `PROWLARR_API_KEY` | Prowlarr → Settings → General |
| `QBITTORRENT_USERNAME` / `_PASSWORD` | qBittorrent WebUI login |
| `DISCORD_BOT_TOKEN` | Discord Developer Portal → Bot |
| `SHELFMARK_DISCORD_GUILD_ID` | right-click the server → Copy ID |
| `SHELFMARK_DISCORD_ALLOWED_ROLE_IDS` | role IDs, comma-separated |

Sullivan uses the same pattern for its *arr keys, which is where this one is
copied from.

## Findings

### 1. Sullivan's media volume was full; the operator has since freed space

Measured at 100% with **0 bytes available** during this inventory, which would
have failed any *arr service moving a completed download into the library. The
operator cleared space the same day and it now reads 93% with 1.4T free.

Recorded because it is worth a monitor rather than a rediscovery: a 19T volume
reaching zero is not a slow drift. Uptime Kuma is already running on freddy and
can watch it.

**It never blocked Shelfmark.** Its route is `/media/qbittorrent/complete` →
freddy, and that tree is on sullivan's root filesystem, not `/mnt/media`.

Note that `jordan` cannot write to `/mnt/media` even with space free — that is a
permissions boundary, not a capacity one, and the two look identical from a
failed `touch`.

### 2. A year-old 60G tarball is still on that volume

```
/mnt/media/books/audiobooks.tar.gz   63,881,810,617 bytes   modified 2025-10-02
```

59.5 GiB, eleven months old, predating all of the organizing work. Freddy now
holds the live library (79G, 7,231 files) and the two 73G working copies made
during the migration were deleted after verification, so this is the only
remaining archive copy — and it is a year stale.

With the volume back to 1.4T free this is no longer urgent, but it is still 59.5
GiB of stale data and still the largest single reclaim available.

**Operator decision, and not a simple one**: it is a poor backup — a year old,
predating all of the organizing work — but it is the only remaining archive copy
of the library.

### 3. Unpackerr will race a Shelfmark download category

`UN_FOLDER_0_PATH=/complete` — the catch-all watcher covers the **whole** tree,
not just the `/complete/lidarr`, `/complete/radarr`, `/complete/tv-sonarr` paths
its *arr blocks name. A `/complete/shelfmark-books` category would be extracted
by unpackerr and by Shelfmark at once.

Cleanest fix is to put the category **outside the watched tree** — e.g.
`/media/qbittorrent/shelfmark` with its own bind into qBittorrent — rather than
narrowing unpackerr's config. No race by construction, and unpackerr keeps
working exactly as it does today.

### 4. Calibre-Web is defined but has never run

Thirteen references in Sullivan's Compose, no running container and no stopped
one. `/mnt/media/ebooks` and `/mnt/media/books/ebooks` are both empty.

Nothing owns ebooks today, so there is no migration to perform and no competing
writer to coordinate with. **Decision: Audiobookshelf on freddy owns ebooks**,
with `/mnt/1tb/ebooks` as the root. It is already mounted into Shelfmark, it is
on the host with free space, and it avoids standing up a second library
application whose Compose entry has a known-wrong `/data` mount.

### 5. The qBittorrent path discrepancy has already been fixed

`todo.md` records qBittorrent defaulting to `/media/qbittorrent/complete` while
the *arr services default to `/mnt/media/qbittorrent/complete`. Resolved against
the running containers, **every one of them uses `/media/qbittorrent/complete`** —
qBittorrent, Lidarr, Filebot and Unpackerr alike. The real `.env` already
normalizes it. No action.

### 6. The Plex claim token needs no rotation

Recorded in `todo.md` as a committed credential. A Plex claim token is valid for
five minutes after generation, so a stale one in Git is inert. Closed.

## P0 status

- [x] Resolve Freddy and Sullivan compose against the running state
- [x] Record private addresses, VPN routes and SSH reachability
- [x] Confirm canonical audiobook and ebook roots on Freddy
- [x] Decide whether ABS or Calibre-Web owns ebooks — **ABS, see finding 4**
- [x] ~~Rotate the committed Plex claim~~ — not a credential, see finding 6
- [ ] Back up ABS config, ABS metadata, audiobook storage, Compose and env files
- [ ] Create the restricted Sullivan sync account and key
- [ ] Record firewall rules — `ufw status` needs sudo, not captured

Unresolved from the acceptance criteria: backups are not yet taken or
restore-tested, and the restricted `shelfmark-sync` account on Sullivan does not
exist, so Freddy cannot yet pull from the download host.

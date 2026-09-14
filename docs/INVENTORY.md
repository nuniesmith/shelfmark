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

### 2. A year-old 60G tarball — RESOLVED 2026-09-12

```
/mnt/media/books/audiobooks.tar.gz   63,881,810,617 bytes   modified 2025-10-02
```

Eleven months old, predating all of the organizing work, and the only remaining
archive copy of the library. **Deleted after the current backup below was taken
and verified**, not before — the order mattered, because until that backup
existed this stale tarball was the only thing standing between a freddy disk
failure and total loss.

Reclaimed 59.5 GiB; `/mnt/media` went from 1.4T to 1.5T free.

`/mnt/media/books` is `actions`-owned without group write, so `jordan` cannot
unlink files there. The deletion went through a root container mounting only
that one directory. Worth knowing that the docker group is root-equivalent on
these hosts, and that this is the escape hatch when a path is `actions`-owned.

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

### 7. The sync account was dead on arrival — nologin blocks the forced command

Found 2026-09-14, the first time the account was exercised from freddy rather
than reasoned about.

`provision-sullivan-sync.sh` created `shelfmark-sync` with
`--shell /usr/sbin/nologin`, on the assumption that "no shell" was the
hardening. It is not: **sshd runs a forced command as `$SHELL -c "<command>"`**,
so `nologin` intercepts it and answers *"This account is currently not
available"*. `rrsync` never ran. The account could do nothing whatsoever.

What makes this worth a finding rather than a one-line fix is how it presents.
Of the four properties that define the account, three are prohibitions:

```
  FAIL  read of the download category succeeds     <- the only informative one
  PASS  interactive command is refused
  PASS  write to Sullivan is refused
  PASS  read outside the category is refused
```

A totally broken account satisfies every prohibition. Checked by hand, in the
order a person naturally checks them, it reads as *hardened* — and the failure
surfaces only later, as a transfer that never happens. The read check is the
single one that distinguishes "locked down" from "inert", which is why
verification now lives in `scripts/verify-sullivan-sync.sh` with captured exit
codes rather than in a list of commands to run and eyeball.

The fix is `usermod --shell /bin/sh shelfmark-sync`. It gives up nothing: the
forced command in `authorized_keys` replaces whatever the client asks for, so
interactive use stays unreachable. The shell was never what prevented it.

### 8. ufw does not filter Docker-published ports

The operator captured `ufw status` on both hosts on 2026-09-13. The operative
conclusion, re-verified 2026-09-14 from sullivan against freddy:

```
  10.0.0.96:8110       /healthz -> 200    (LAN)
  100.106.65.55:8110   /healthz -> 200    (tailnet)
```

Docker installs its own iptables rules in `DOCKER-USER` and the `nat` table,
which are consulted **before** ufw's chains. A published port is therefore
reachable from any host that can route to the address, whatever `ufw status`
lists. This is standard Docker behaviour, not a misconfiguration of these
hosts, and it applies to every published port on both machines — not only
Shelfmark's.

Shelfmark's own exposure is now bounded by authentication rather than by the
firewall: `SHELFMARK_API_TOKEN` is set, and `/api/v1/*` returns 401 without it
(`/healthz` stays open by design). Addresses are private in both cases — RFC1918
on the LAN and 100.64.0.0/10 CGNAT on the tailnet — so nothing here is
internet-reachable.

**Not changed.** Restricting these ports means editing live firewall rules on
two hosts that serve other services, which is its own task with its own
rollback plan. Recorded so the decision is explicit rather than overlooked.

## Backups

Pulled by sullivan from freddy — a different host from the originals, which is
the point of them. Under `/home/jordan/backups/shelfmark` on sullivan's root
filesystem (1.4T free), because `jordan` cannot write `/mnt/media/books`.

| What | How | Size |
|---|---|---|
| `audiobooks/` | `rsync -a --delete` from `/mnt/1tb/audiobooks` | 79G |
| `abs-config/` | rsync, with the live DB excluded and replaced by a snapshot | 12M |
| `abs-metadata/` | `rsync -a --delete` from ABS `/metadata` | 6.6M |
| `configs/` | freddy's resolved compose, mode 600 | 32K |

**Verified on capture, 2026-09-12.** 7,324 files live and 7,324 in the backup; a
second `rsync -an --delete --itemize-changes` reported no differences at all, so
the trees are identical rather than merely the same size. A sample restore of
`John Wyndham/` into a temporary directory read back with correct titles and
authors. 84,047,407,025 bytes transferred, rsync exit 0.

**The Audiobookshelf database is snapshotted, not copied.** A plain `cp` of a
SQLite file a running server is writing can catch it mid-transaction and restore
to a corrupt database. `sqlite3.Connection.backup()` takes a consistent snapshot
while ABS keeps serving. Verified on capture: `integrity_check ok`, 189
libraryItems, 68 authors.

Re-run the whole thing from sullivan:

```bash
F=jordan@100.106.65.55
D=/home/jordan/backups/shelfmark
ssh $F 'python3 - <<PY
import sqlite3
s=sqlite3.connect("file:/mnt/1tb/audiobookshelf/config/absdatabase.sqlite?mode=ro",uri=True)
d=sqlite3.connect("/tmp/absdatabase-snapshot.sqlite"); s.backup(d); d.close(); s.close()
PY'
rsync -a --delete --exclude 'absdatabase.sqlite*' $F:/mnt/1tb/audiobookshelf/config/ $D/abs-config/
rsync -a $F:/tmp/absdatabase-snapshot.sqlite $D/abs-config/absdatabase.sqlite
rsync -a --delete $F:/mnt/1tb/audiobookshelf/metadata/ $D/abs-metadata/
rsync -a --delete $F:/mnt/1tb/audiobooks/ $D/audiobooks/
```

### Restoring

The library is a plain file tree, so a restore is an rsync the other way. To
check a restore without touching anything live, pull into a temporary directory
and compare counts:

```bash
mkdir -p /tmp/restore-test
rsync -a /home/jordan/backups/shelfmark/audiobooks/ /tmp/restore-test/
find /tmp/restore-test -type f | wc -l     # expect the live count
python3 -c "import sqlite3;print(sqlite3.connect('/home/jordan/backups/shelfmark/abs-config/absdatabase.sqlite').execute('PRAGMA integrity_check').fetchone())"
```

Restoring ABS itself means stopping the container, replacing `/config` and
`/metadata`, and starting it again — the database must not be swapped underneath
a running server.

## The restricted Sullivan sync account

`scripts/provision-sullivan-sync.sh` creates it. Run on sullivan with sudo,
passing the public key generated on freddy:

```bash
sudo bash provision-sullivan-sync.sh "ssh-ed25519 AAAA... shelfmark@freddy"
```

It creates `shelfmark-sync` with an `authorized_keys` entry that forces
`rrsync -ro` against a single directory, so the account can do exactly one
thing: read that directory over rsync. No interactive shell, no pty, no
forwarding, no write.

**The login shell must be a real one — `/bin/sh`, not `/usr/sbin/nologin`.**
An SSH forced command is not executed directly; sshd runs it as
`$SHELL -c "<command>"`. With `nologin` the account answers every connection
with *"This account is currently not available"* and `rrsync` never starts, so
the one operation the account exists for is blocked while the three that must
fail still fail. Nothing is given up by `/bin/sh`: the forced command replaces
whatever the client asks for, so interactive use is unreachable regardless.
The first version of the provisioning script shipped `nologin` and was dead on
arrival — verified 2026-09-14 and fixed.

### Verifying it

Run from freddy, inside the worker, where the private key lives:

```bash
docker exec shelfmark-worker verify-sullivan-sync
```

Four properties, and **the asymmetry matters**: one must succeed and three must
fail. A wholly broken account — wrong shell, missing `rrsync`, revoked key —
still fails all three of the checks that are supposed to fail, so a hand-run
session reads as "everything refused, looks locked down" when nothing works at
all. Only the read check separates those two states, which is why this is a
script with captured exit codes and not a list of commands to eyeball.

The category is `/media/qbittorrent/shelfmark`, deliberately **outside**
`/media/qbittorrent/complete`. Unpackerr's catch-all watcher is
`UN_FOLDER_0_PATH=/complete`, so a category under there would be extracted by
Unpackerr and Shelfmark at once — see finding 3. Keeping it out of that tree
removes the race by construction rather than by configuration that can drift.

Generate the key on freddy so the private half never travels:

```bash
ssh-keygen -t ed25519 -N "" -f /tmp/shelfmark-sync -C "shelfmark@freddy"
```

Private half → GitHub secret `SHELFMARK_SULLIVAN_SSH_KEY` on `nuniesmith/freddy`;
CI installs it as mode 600 owned by uid 1001, which is required because ssh
refuses a group-readable private key. Public half → the script above. Then
delete `/tmp/shelfmark-sync*`.

## P0 status

- [x] Resolve Freddy and Sullivan compose against the running state
- [x] Record private addresses, VPN routes and SSH reachability
- [x] Confirm canonical audiobook and ebook roots on Freddy
- [x] Decide whether ABS or Calibre-Web owns ebooks — **ABS, see finding 4**
- [x] ~~Rotate the committed Plex claim~~ — not a credential, see finding 6
- [x] Back up ABS config, ABS metadata, audiobook storage and Compose
- [x] Create the restricted Sullivan sync account and key — account exists on
      sullivan and the key is wired through CI. **The account was dead on
      arrival** (finding 7); the shell fix needs one `usermod` with root, after
      which `verify-sullivan-sync` must report all four checks holding.
- [x] Delete the stale 60G tarball — done, 59.5 GiB reclaimed
- [x] Record firewall rules — captured 2026-09-13, see finding 8

One item remains open, and it is the sync account: provisioned, key in place,
but blocked on a one-line shell change with root on sullivan. Everything else
in P0 is closed.

Environment files are deliberately **not** backed up. They hold live secrets, and
every value in them is already recoverable from GitHub Actions secrets, which is
the authority. Copying them around would multiply the number of places a
credential sits.

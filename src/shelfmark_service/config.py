"""Environment-backed settings shared by the API and worker."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path


def _path_from_env(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else None


def _bool_from_env(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().casefold()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _float_from_env(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return max(0.1, float(value))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


def _int_tuple_from_env(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    result: list[int] = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError as exc:
            raise ValueError(f"{name} must be a comma-separated list of integers") from exc
    return tuple(result) if result else default


@dataclass(frozen=True)
class Settings:
    """Runtime paths and service settings.

    Empty media roots are allowed during development.  The readiness endpoint
    checks only roots that have been configured, which lets the API start before
    the Freddy bind mounts are supplied by Compose.
    """

    database_path: Path = Path("/data/shelfmark.db")
    manifest_root: Path = Path("/data/manifests")
    audio_root: Path | None = None
    ebook_root: Path | None = None
    incoming_root: Path | None = None
    work_root: Path | None = None
    quarantine_root: Path | None = None
    api_token: str | None = None
    worker_id: str = "shelfmark-worker"
    poll_interval: float = 2.0
    http_timeout: float = 15.0
    http_retries: int = 3
    circuit_breaker_failure_threshold: int = 5
    circuit_breaker_cooldown_seconds: float = 30.0
    audiobookshelf_url: str | None = None
    audiobookshelf_token: str | None = None
    audiobookshelf_library_id: str | None = None
    prowlarr_url: str | None = None
    prowlarr_api_key: str | None = None
    # The user's one configured indexer advertises the general Books buckets
    # (7000/7010/7030/7050) but neither 7020 (EBook specifically) nor 7060
    # (Audiobook) — filtering to 7020 alone would return zero results, so
    # `/ebook-request` defaults to the bucket that indexer actually has.
    prowlarr_book_categories: tuple[int, ...] = (7000,)
    qbittorrent_url: str | None = None
    qbittorrent_username: str | None = None
    qbittorrent_password: str | None = None
    qbittorrent_api_key: str | None = None
    # Matches /api/v1/downloads' own default, so a reconciler pass and a
    # manual `/downloads` look at the same category unless both are told
    # otherwise.
    qbittorrent_category: str = "shelfmark-books"
    # Prowlarr's `downloadUrl` proxies the real .torrent from the tracker
    # using PROWLARR's own credentials -- that's what lets a private-tracker
    # release (IPTorrents) work with no tracker auth of its own. But the
    # host in that URL is Prowlarr's OWN view of itself, which is not
    # necessarily reachable from qBittorrent's network namespace: verified
    # live from inside the qBittorrent container on Sullivan, `sullivan:9696`
    # (Prowlarr's own hostname) refused the connection while `prowlarr:9696`
    # -- the name qBittorrent and Prowlarr both resolve on their shared
    # `sullivan_download` Docker network -- answered fine. grab_release
    # rewrites just the scheme+host of `downloadUrl` to this base (see
    # worker.py) before handing it to qBittorrent directly; the path and
    # full query string (apikey and link both live there) are left alone,
    # since that is what actually authorizes the download. Defaults to the
    # same container-name convention PROWLARR_URL already uses.
    qbittorrent_prowlarr_base_url: str = "http://prowlarr:9696"
    sullivan_host: str | None = None
    sullivan_user: str | None = None
    sullivan_identity_file: Path | None = None
    sullivan_ssh_port: int = 22
    sullivan_known_hosts: Path | None = Path("/data/known_hosts")
    sullivan_strict_host_key: bool = False
    sullivan_completed_root: str = "/complete/shelfmark-books"
    transfer_settle_seconds: float = 30.0
    transfer_poll_seconds: float = 5.0
    transfer_timeout_seconds: float = 3600.0
    worker_stale_seconds: float = 900.0
    # How long a worker's `worker_liveness` row (see db.py migration 4) may go
    # unrefreshed before /readyz and the worker's own Docker healthcheck call
    # it stale. This is deliberately a SEPARATE knob from `worker_stale_seconds`
    # above: that one gates reaping an abandoned RUNNING JOB back onto the
    # queue (a 900s default chosen to tolerate a slow-but-alive job), while
    # this one gates a liveness signal ticked once per poll loop -- most of
    # which are idle. Defaulted to 3x the reconcile interval (60s default) so
    # normal idle ticking (every `SHELFMARK_WORKER_POLL_SECONDS`, default 2s)
    # or an ordinary quick job never trips it, while a genuinely wedged
    # worker is still caught within a few minutes rather than waiting for the
    # much more conservative 900s job reaper. A threshold at or below the
    # poll/tick rate would flap on every idle cycle, reporting "stale"
    # between two perfectly normal writes.
    worker_liveness_stale_seconds: float = 180.0
    # Off switch for the whole grab->transfer->organize->scan chain: an
    # operator who needs to stop automatic imports (a bad release flooding
    # retries, a library reorganization in progress) flips one env var and
    # restarts, rather than needing a redeploy of different code.
    download_automation_enabled: bool = True
    reconcile_interval_seconds: float = 60.0
    discord_webhook_url: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_path=_path_from_env("SHELFMARK_DB_PATH") or Path("/data/shelfmark.db"),
            manifest_root=_path_from_env("SHELFMARK_MANIFEST_ROOT") or Path("/data/manifests"),
            audio_root=_path_from_env("SHELFMARK_AUDIOBOOKS_ROOT"),
            ebook_root=_path_from_env("SHELFMARK_EBOOKS_ROOT"),
            incoming_root=_path_from_env("SHELFMARK_INCOMING_ROOT"),
            work_root=_path_from_env("SHELFMARK_WORK_ROOT"),
            quarantine_root=_path_from_env("SHELFMARK_QUARANTINE_ROOT"),
            api_token=os.environ.get("SHELFMARK_API_TOKEN") or None,
            worker_id=os.environ.get("SHELFMARK_WORKER_ID") or socket.gethostname(),
            poll_interval=_float_from_env("SHELFMARK_WORKER_POLL_SECONDS", 2.0),
            http_timeout=_float_from_env("SHELFMARK_HTTP_TIMEOUT_SECONDS", 15.0),
            http_retries=max(0, int(os.environ.get("SHELFMARK_HTTP_RETRIES", "3"))),
            circuit_breaker_failure_threshold=max(
                1, int(os.environ.get("SHELFMARK_CIRCUIT_BREAKER_FAILURE_THRESHOLD", "5"))
            ),
            circuit_breaker_cooldown_seconds=_float_from_env(
                "SHELFMARK_CIRCUIT_BREAKER_COOLDOWN_SECONDS", 30.0
            ),
            audiobookshelf_url=os.environ.get("AUDIOBOOKSHELF_URL") or None,
            audiobookshelf_token=os.environ.get("AUDIOBOOKSHELF_API_TOKEN") or None,
            audiobookshelf_library_id=os.environ.get("AUDIOBOOKSHELF_LIBRARY_ID") or None,
            prowlarr_url=os.environ.get("PROWLARR_URL") or None,
            prowlarr_api_key=os.environ.get("PROWLARR_API_KEY") or None,
            prowlarr_book_categories=_int_tuple_from_env("PROWLARR_BOOK_CATEGORIES", (7000,)),
            qbittorrent_url=os.environ.get("QBITTORRENT_URL") or None,
            qbittorrent_username=os.environ.get("QBITTORRENT_USERNAME") or None,
            qbittorrent_password=os.environ.get("QBITTORRENT_PASSWORD") or None,
            qbittorrent_api_key=os.environ.get("QBITTORRENT_API_KEY") or None,
            qbittorrent_category=os.environ.get("QBITTORRENT_CATEGORY", "shelfmark-books"),
            qbittorrent_prowlarr_base_url=os.environ.get(
                "QBITTORRENT_PROWLARR_BASE_URL", "http://prowlarr:9696"
            ),
            sullivan_host=os.environ.get("SULLIVAN_SSH_HOST") or None,
            sullivan_user=os.environ.get("SULLIVAN_SSH_USER") or None,
            sullivan_identity_file=_path_from_env("SULLIVAN_SSH_IDENTITY_FILE"),
            sullivan_ssh_port=int(os.environ.get("SULLIVAN_SSH_PORT", "22")),
            # Persistent, so the key learned on first contact is pinned for
            # every later transfer rather than re-trusted each time.
            sullivan_known_hosts=(
                _path_from_env("SULLIVAN_SSH_KNOWN_HOSTS") or Path("/data/known_hosts")
            ),
            sullivan_strict_host_key=_bool_from_env("SULLIVAN_SSH_STRICT_HOST_KEY", False),
            sullivan_completed_root=os.environ.get(
                "SULLIVAN_COMPLETED_ROOT", "/complete/shelfmark-books"
            ),
            transfer_settle_seconds=_float_from_env("SHELFMARK_TRANSFER_SETTLE_SECONDS", 30.0),
            transfer_poll_seconds=_float_from_env("SHELFMARK_TRANSFER_POLL_SECONDS", 5.0),
            transfer_timeout_seconds=_float_from_env("SHELFMARK_TRANSFER_TIMEOUT_SECONDS", 3600.0),
            worker_stale_seconds=_float_from_env("SHELFMARK_WORKER_STALE_SECONDS", 900.0),
            worker_liveness_stale_seconds=_float_from_env(
                "SHELFMARK_WORKER_LIVENESS_STALE_SECONDS", 180.0
            ),
            download_automation_enabled=_bool_from_env("SHELFMARK_DOWNLOAD_AUTOMATION_ENABLED", True),
            reconcile_interval_seconds=_float_from_env("SHELFMARK_RECONCILE_INTERVAL_SECONDS", 60.0),
            discord_webhook_url=os.environ.get("SHELFMARK_DISCORD_WEBHOOK_URL") or None,
        )

    def configured_roots(self) -> dict[str, Path]:
        return {
            name: path
            for name, path in {
                "audio_root": self.audio_root,
                "ebook_root": self.ebook_root,
                "incoming_root": self.incoming_root,
                "work_root": self.work_root,
                "quarantine_root": self.quarantine_root,
            }.items()
            if path is not None
        }

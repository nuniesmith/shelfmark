"""Database-backed worker for organizer jobs."""

from __future__ import annotations

import logging
import os
import posixpath
import signal
import time
from pathlib import Path
from typing import Any

from .clients import AudiobookshelfClient, HttpClient, ProwlarrClient, QBittorrentClient, ServiceError
from .config import Settings
from .db import Database, Job
from .errors import ErrorCode, ShelfmarkError
from .manifest import JsonlManifest, sha256_file
from .transfer import RsyncTransfer, TransferError, wait_until_stable

logger = logging.getLogger("shelfmark.worker")


class JobCancelled(RuntimeError):
    pass


def _path(payload: dict[str, Any], name: str, default: Path | None = None) -> Path:
    value = payload.get(name)
    if value is None:
        if default is None:
            raise ShelfmarkError(ErrorCode.INVALID_PAYLOAD, f"job payload requires {name}")
        return default
    return Path(str(value)).expanduser().resolve()


def _upstream_failure(exc: ServiceError) -> ShelfmarkError:
    # Mirrors api.py's `_upstream_error`: never surface `exc.message` here.
    # It is the upstream response BODY, which api.py's own comment on
    # `_upstream_error` already documents as unsafe ("can contain release
    # URLs, credentials, or other data that should stay in service logs") —
    # that rule applies just as much to a job's stored error as to an HTTP
    # response, so only the service name and status code travel further.
    return ShelfmarkError(
        ErrorCode.UPSTREAM_UNAVAILABLE,
        f"{exc.service} request failed",
        details={"service": exc.service, "status": exc.status},
    )


# qBittorrent's own "genuinely done" states, from GET /api/v2/torrents/info.
# `progress == 1` alone is not enough to decide a torrent is safe to pull:
#
# - checkingUP / checkingDL / checkingResumeData: qBittorrent is re-hashing
#   files already on disk (after its own restart, or a manual recheck). The
#   torrent can sit at progress 1.0 the entire time a recheck runs.
# - moving: a completed download is being relocated to its final save path.
#   progress is already 1.0, but the content is not at `content_path` yet --
#   pulling here races the move and can catch a half-moved tree.
# - allocating / downloading / metaDL / *DL / error / missingFiles / unknown:
#   plainly not done.
#
# Both the pre-5.x ("paused") and 5.x+ ("stopped") state names are included,
# since this codebase does not pin a qBittorrent version.
_QBITTORRENT_COMPLETE_STATES = frozenset(
    {"uploading", "stalledUP", "queuedUP", "pausedUP", "stoppedUP", "forcedUP"}
)


def _is_torrent_complete(torrent: dict[str, Any]) -> bool:
    """Whether a qBittorrent torrent entry is finished, checked, and at rest.

    See `_QBITTORRENT_COMPLETE_STATES` above for why `progress` alone cannot
    answer this.
    """
    progress = torrent.get("progress")
    return isinstance(progress, (int, float)) and progress >= 1 and torrent.get("state") in _QBITTORRENT_COMPLETE_STATES


def _split_webhook_url(url: str) -> tuple[str, str]:
    """Split a webhook URL into (base_url, last_path_segment).

    `HttpClient.request(path)` joins as `f"{base_url}/{path}"`. Calling it
    with an empty path would append a trailing slash to the whole webhook
    URL (`.../webhooks/<id>/<token>/`), which is a different route than the
    one Discord documents and is not worth risking against a real webhook.
    Splitting off the last segment as `path` reconstructs the exact original
    URL with no trailing slash instead.
    """
    base, separator, last = url.rstrip("/").rpartition("/")
    return base, last if separator else ""


def _plan_summary(plan: Any) -> dict[str, Any]:
    return {
        "books": len(plan.books),
        "tracks": sum(len(book.tracks) for book in plan.books),
        "archives": len(plan.extracts),
        "trash": len(plan.trash),
        "warnings": list(plan.warnings),
        "items": [
            {
                "author": book.meta.author,
                "title": book.meta.title,
                "year": book.meta.year,
                "narrator": book.meta.narrator,
                "destination": str(book.dest_dir),
                "files": len(book.tracks),
            }
            for book in plan.books
        ],
    }


class Worker:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings
        self.worker_id = settings.worker_id

    def run_once(self) -> bool:
        job = self.database.claim_next(self.worker_id)
        if job is None:
            return False
        manifest = JsonlManifest(
            self.settings.manifest_root / f"{job.id}.jsonl", actor=self.worker_id
        )
        manifest.event("job_started", job_id=job.id, kind=job.kind, worker_id=self.worker_id)
        logger.info("job claimed id=%s kind=%s", job.id, job.kind)
        try:
            self.database.heartbeat(job.id, self.worker_id)
            result = self.execute(job)
            self.database.heartbeat(job.id, self.worker_id)
            if self.database.cancellation_requested(job.id):
                self.database.cancel_running(job.id, worker_id=self.worker_id)
                manifest.event("job_cancelled", job_id=job.id)
                logger.info("job cancelled id=%s", job.id)
            else:
                self.database.complete(job.id, self.worker_id, result)
                manifest.event("job_succeeded", job_id=job.id, result=result)
                logger.info("job completed id=%s", job.id)
                self._chain_after_success(job, result)
        except JobCancelled as exc:
            self.database.cancel_running(job.id, worker_id=self.worker_id)
            manifest.event("job_cancelled", job_id=job.id, reason=str(exc))
            logger.info("job cancelled id=%s reason=%s", job.id, exc)
        except ShelfmarkError as exc:
            self.database.fail(job.id, self.worker_id, exc.message, code=exc.code.value)
            manifest.event(
                "job_failed", job_id=job.id, error=exc.message, code=exc.code.value, details=exc.details
            )
            logger.exception("job failed id=%s code=%s", job.id, exc.code.value)
            self._notify_chain_failure(job, exc.message, exc.code.value)
        except Exception as exc:  # noqa: BLE001 - failure belongs in the job record
            # Anything landing here escaped every explicit ShelfmarkError raise
            # in execute() — an OSError from a disk write, an ImportError, a
            # bug. It still needs a code, because a job record with a message
            # but no code is exactly the "nothing downstream can branch on
            # this" problem this module exists to fix. INTERNAL is that
            # deliberately generic bucket, never a guess at a more specific one.
            self.database.fail(job.id, self.worker_id, str(exc), code=ErrorCode.INTERNAL.value)
            manifest.event("job_failed", job_id=job.id, error=str(exc), code=ErrorCode.INTERNAL.value)
            logger.exception("job failed id=%s", job.id)
            self._notify_chain_failure(job, str(exc), ErrorCode.INTERNAL.value)
        return True

    def execute(self, job: Job) -> dict[str, Any]:
        if job.kind in {"metadata_update", "metadata_match", "library_scan"}:
            if not self.settings.audiobookshelf_url or not self.settings.audiobookshelf_token:
                raise ShelfmarkError(
                    ErrorCode.PROVIDER_NOT_CONFIGURED, "Audiobookshelf integration is not configured"
                )
            client = AudiobookshelfClient(
                self.settings.audiobookshelf_url,
                self.settings.audiobookshelf_token,
                timeout=self.settings.http_timeout,
                retries=self.settings.http_retries,
                breaker_failure_threshold=self.settings.circuit_breaker_failure_threshold,
                breaker_cooldown_seconds=self.settings.circuit_breaker_cooldown_seconds,
            )
            # Everything below talks to Audiobookshelf, so one handler covers
            # all three sub-kinds: a ServiceError here means the integration is
            # configured but the request itself failed (bad ID, ABS down,
            # timeout) — a different situation from the payload/config checks
            # above, which is why it gets UPSTREAM_UNAVAILABLE instead of
            # INVALID_PAYLOAD or PROVIDER_NOT_CONFIGURED.
            try:
                if job.kind == "metadata_update":
                    item_id = str(job.payload.get("item_id", ""))
                    media = job.payload.get("media")
                    if not item_id or not isinstance(media, dict):
                        raise ShelfmarkError(
                            ErrorCode.INVALID_PAYLOAD, "metadata_update requires item_id and media"
                        )
                    return {"item_id": item_id, "upstream": client.update_media(item_id, media), "updated": True}
                if job.kind == "metadata_match":
                    item_id = str(job.payload.get("item_id", ""))
                    if not item_id:
                        raise ShelfmarkError(ErrorCode.INVALID_PAYLOAD, "metadata_match requires item_id")
                    fields = {
                        key: job.payload.get(key)
                        for key in ("title", "author", "provider", "isbn", "asin")
                        if job.payload.get(key)
                    }
                    return {
                        "item_id": item_id,
                        "upstream": client.match(
                            item_id,
                            **fields,
                            override_defaults=bool(job.payload.get("override_defaults", False)),
                        ),
                        "matched": True,
                    }
                library_id = str(job.payload.get("library_id", ""))
                if not library_id:
                    raise ShelfmarkError(ErrorCode.INVALID_PAYLOAD, "library_scan requires library_id")
                return {
                    "library_id": library_id,
                    "upstream": client.scan(library_id, force=bool(job.payload.get("force", False))),
                    "scan_started": True,
                }
            except ServiceError as exc:
                raise _upstream_failure(exc) from exc
        if job.kind == "transfer_completed":
            if not self.settings.sullivan_host or not self.settings.sullivan_user:
                raise ShelfmarkError(ErrorCode.PROVIDER_NOT_CONFIGURED, "Sullivan transfer is not configured")
            raw_remote = str(job.payload.get("remote_path", "")).strip()
            if not raw_remote:
                raise ShelfmarkError(ErrorCode.INVALID_PAYLOAD, "job payload requires remote_path")
            base_remote = posixpath.normpath(self.settings.sullivan_completed_root)
            remote = posixpath.normpath(
                raw_remote if raw_remote.startswith("/") else posixpath.join(base_remote, raw_remote)
            )
            if remote != base_remote and not remote.startswith(base_remote.rstrip("/") + "/"):
                raise ShelfmarkError(
                    ErrorCode.INVALID_PAYLOAD, "remote_path must stay within Sullivan's Shelfmark category"
                )
            local_root = self.settings.incoming_root or Path("/incoming")
            local = _path(job.payload, "local_path", local_root)
            if local != local_root and local_root not in local.parents:
                raise ShelfmarkError(
                    ErrorCode.INVALID_PAYLOAD, "local_path must stay within the configured incoming root"
                )
            transfer = RsyncTransfer(
                host=self.settings.sullivan_host,
                user=self.settings.sullivan_user,
                identity_file=self.settings.sullivan_identity_file,
                port=self.settings.sullivan_ssh_port,
                timeout_seconds=self.settings.transfer_timeout_seconds,
                retries=self.settings.http_retries,
                known_hosts=self.settings.sullivan_known_hosts,
                strict_host_key=self.settings.sullivan_strict_host_key,
            )
            # Each call below fails for a different reason, so each gets its
            # own translation rather than one try/except around the whole
            # transfer: a pull failure is the SSH/rsync path being unreachable
            # (UPSTREAM_UNAVAILABLE); wait_until_stable failing means nothing
            # usable ever showed up locally (SOURCE_MISSING); a failed verify
            # RUN is the checksum command itself breaking (UPSTREAM_UNAVAILABLE
            # again), which is distinct from the command succeeding and
            # reporting that the content differs (VERIFICATION_FAILED, below).
            try:
                transfer.pull(remote, local)
            except TransferError as exc:
                raise ShelfmarkError(ErrorCode.UPSTREAM_UNAVAILABLE, f"transfer from Sullivan failed: {exc}") from exc
            # Where the book actually landed. rsync now preserves the remote
            # directory name, so the tree is under local/<name> rather than
            # loose in the incoming root — and it is that path an organize job
            # has to be pointed at, not the shared root holding every download.
            name = posixpath.basename(remote.rstrip("/"))
            landed = local / name if name else local
            try:
                snapshot = wait_until_stable(
                    landed,
                    settle_seconds=self.settings.transfer_settle_seconds,
                    poll_seconds=self.settings.transfer_poll_seconds,
                    timeout_seconds=self.settings.transfer_timeout_seconds,
                )
            except TransferError as exc:
                raise ShelfmarkError(ErrorCode.SOURCE_MISSING, str(exc)) from exc
            # Verify AFTER the tree has settled, not straight after the pull:
            # comparing a tree still being written reports differences that are
            # simply the write in progress.
            try:
                differences = transfer.verify(remote, local)
            except TransferError as exc:
                raise ShelfmarkError(
                    ErrorCode.UPSTREAM_UNAVAILABLE, f"verification against Sullivan failed: {exc}"
                ) from exc
            if differences:
                raise ShelfmarkError(
                    ErrorCode.VERIFICATION_FAILED,
                    "transfer does not match Sullivan after copying "
                    f"({len(differences)} file(s) differ): {', '.join(differences[:5])}",
                    details={"differing_files": differences[:5], "differing_count": len(differences)},
                )
            JsonlManifest(
                self.settings.manifest_root / f"{job.id}.jsonl", actor=self.worker_id
            ).event(
                "transfer_verified",
                remote_path=remote,
                local_path=str(landed),
                files=len(snapshot),
            )
            return {
                "remote_path": remote,
                "local_path": str(landed),
                "files": len(snapshot),
                "verified": True,
            }
        if job.kind == "grab_release":
            if not self.settings.prowlarr_url or not self.settings.prowlarr_api_key:
                raise ShelfmarkError(ErrorCode.PROVIDER_NOT_CONFIGURED, "Prowlarr integration is not configured")
            release = job.payload.get("release")
            if not isinstance(release, dict) or not release:
                raise ShelfmarkError(ErrorCode.INVALID_PAYLOAD, "job payload requires a release object")
            client = ProwlarrClient(
                self.settings.prowlarr_url,
                self.settings.prowlarr_api_key,
                timeout=self.settings.http_timeout,
                retries=self.settings.http_retries,
                breaker_failure_threshold=self.settings.circuit_breaker_failure_threshold,
                breaker_cooldown_seconds=self.settings.circuit_breaker_cooldown_seconds,
            )
            try:
                return {"release": release, "upstream": client.grab(release), "submitted": True}
            except ServiceError as exc:
                raise _upstream_failure(exc) from exc
        if job.kind == "reconcile_downloads":
            return self._reconcile_downloads(job)
        if job.kind not in {"organize_preview", "organize_apply"}:
            # Reachable only if a job was enqueued with a kind the API's
            # `JobRequest` schema never allows (e.g. inserted directly through
            # `Database.enqueue`, which does not validate `kind`) — a bad
            # request, not a broken worker.
            raise ShelfmarkError(ErrorCode.INVALID_PAYLOAD, f"unsupported job kind: {job.kind}")
        payload = job.payload
        source = _path(payload, "source")
        if not source.is_dir():
            raise ShelfmarkError(ErrorCode.SOURCE_MISSING, f"source is not a directory: {source}")
        dest = _path(payload, "dest", source)
        trash = _path(payload, "trash", source / "trash")
        from main import apply_extracts, apply_plan, build_plan

        options = {
            "source": source,
            "dest": dest,
            "trash": trash,
            "folder_format": str(payload.get("format", "year-title")),
            "keep_names": bool(payload.get("keep_names", False)),
            "include_non_cover_images": bool(payload.get("keep_images", False)),
            "media_mode": str(payload.get("media", "auto")),
            "trash_unknown": bool(payload.get("trash_unknown", False)),
        }
        plan = build_plan(**options)
        manifest = JsonlManifest(
            self.settings.manifest_root / f"{job.id}.jsonl", actor=self.worker_id
        )
        manifest.event("plan_created", summary=_plan_summary(plan))
        if job.kind == "organize_preview":
            return _plan_summary(plan)

        if self.database.cancellation_requested(job.id):
            raise JobCancelled("cancel requested before apply")
        if plan.extracts:
            self._record_plan(manifest, plan)
            errors = apply_extracts(plan, trash=trash, dry_run=False, copy=bool(payload.get("copy", False)))
            if errors:
                raise ShelfmarkError(
                    ErrorCode.EXTRACTION_FAILED, "; ".join(errors), details={"errors": errors}
                )
            plan = build_plan(**options)
            manifest.event("plan_recreated", summary=_plan_summary(plan))
        self._record_plan(manifest, plan)
        apply_plan(plan, trash=trash, dry_run=False, copy=bool(payload.get("copy", False)))
        return _plan_summary(plan) | {"applied": True}

    @staticmethod
    def _record_plan(manifest: JsonlManifest, plan: Any) -> None:
        for book in plan.books:
            for operation in book.tracks + book.extras:
                if operation.kind == "trash":
                    continue
                checksum = None
                try:
                    if operation.src.is_file():
                        checksum = sha256_file(operation.src)
                except OSError:
                    pass
                manifest.event(
                    "operation_planned",
                    source=str(operation.src),
                    destination=str(operation.dest),
                    kind=operation.kind,
                    checksum=checksum,
                )
        for operation in plan.extracts:
            manifest.event(
                "extract_planned",
                source=str(operation.src),
                destination=str(operation.dest),
                kind=operation.kind,
            )

    def _reconcile_downloads(self, job: Job) -> dict[str, Any]:
        """Ask qBittorrent what has finished and start the pipeline for anything new.

        This is the RECONCILER, not a per-torrent watcher: it is enqueued
        periodically (see `_maybe_enqueue_reconcile` / `main()` below) and
        re-derives "what needs importing" from qBittorrent's own state every
        time it runs, instead of a long-lived task tracking one grab from
        submission to completion. A long-lived watcher would be stranded by a
        worker restart mid-watch -- and this process restarts on every
        deploy -- whereas a reconciler that starts fresh each pass and asks
        the same idempotent question ("what's complete that I haven't
        imported?") self-heals from any failure, including one of its own.
        """
        if not self.settings.qbittorrent_url:
            raise ShelfmarkError(ErrorCode.PROVIDER_NOT_CONFIGURED, "qBittorrent integration is not configured")
        if not self.settings.qbittorrent_api_key and not (
            self.settings.qbittorrent_username and self.settings.qbittorrent_password
        ):
            raise ShelfmarkError(ErrorCode.PROVIDER_NOT_CONFIGURED, "qBittorrent credentials are not configured")
        client = QBittorrentClient(
            self.settings.qbittorrent_url,
            username=self.settings.qbittorrent_username,
            password=self.settings.qbittorrent_password,
            api_key=self.settings.qbittorrent_api_key,
            timeout=self.settings.http_timeout,
            retries=self.settings.http_retries,
            breaker_failure_threshold=self.settings.circuit_breaker_failure_threshold,
            breaker_cooldown_seconds=self.settings.circuit_breaker_cooldown_seconds,
        )
        category = self.settings.qbittorrent_category
        try:
            client.login()
            torrents = client.torrents(category=category)
        except ServiceError as exc:
            raise _upstream_failure(exc) from exc
        if not isinstance(torrents, list):
            torrents = []
        claimed_ids: list[str] = []
        completed = 0
        for torrent in torrents:
            if not isinstance(torrent, dict) or not _is_torrent_complete(torrent):
                continue
            completed += 1
            torrent_hash = str(torrent.get("hash") or "").strip()
            name = str(torrent.get("name") or "").strip()
            if not torrent_hash or not name:
                # Nothing safe to key off of or transfer -- skip rather than
                # guess, the same way a missing item_id or library_id above
                # is treated as invalid input rather than filled in.
                continue
            # `claim_torrent_import` is the idempotency gate: it records this
            # hash and enqueues its `transfer_completed` job in one
            # transaction, and returns None if a previous pass (before or
            # after any restart) already claimed it. That is what makes this
            # safe to run every `SHELFMARK_RECONCILE_INTERVAL_SECONDS` forever
            # -- qBittorrent keeps reporting a seeding torrent as complete
            # indefinitely, but a hash is only ever handed to the pipeline
            # once.
            claimed = self.database.claim_torrent_import(
                torrent_hash,
                name,
                "transfer_completed",
                {
                    "remote_path": name,
                    "_reconcile_hash": torrent_hash,
                    "_reconcile_name": name,
                },
                actor="reconciler",
            )
            if claimed is not None:
                claimed_ids.append(claimed.id)
        return {
            "category": category,
            "seen": len(torrents),
            "completed": completed,
            "claimed_jobs": claimed_ids,
        }

    def _chain_after_success(self, job: Job, result: dict[str, Any]) -> None:
        """Advance a reconciler-started job to its next pipeline stage.

        Only jobs the reconciler itself enqueued carry `_reconcile_hash` in
        their payload -- a `transfer_completed` submitted by hand through
        `POST /api/v1/transfers/pull`, or an `organize_apply` queued from
        `/organize-preview`, has no such key and is left exactly as it
        behaved before this feature existed. This is what keeps the chain
        "separate jobs, not one mega-job": each stage is its own row in
        `jobs`, independently retryable and visible to `/job`, and this
        method is the only thing that stitches them together.
        """
        torrent_hash = job.payload.get("_reconcile_hash")
        if not torrent_hash:
            return
        name = job.payload.get("_reconcile_name") or torrent_hash
        if job.kind == "transfer_completed":
            if not result.get("verified"):
                # execute() already raises VERIFICATION_FAILED rather than
                # returning normally when RsyncTransfer.verify() finds a
                # mismatch (see the transfer_completed branch above), so this
                # should be unreachable. It is checked again here anyway
                # because organize_apply is destructive, and "never organize
                # an unverified transfer" is a hard constraint worth enforcing
                # at the one remaining place that could chain into it, not
                # just trusted from upstream.
                return
            local_path = result.get("local_path")
            if not local_path:
                return
            self.database.enqueue(
                "organize_apply",
                {"source": local_path, "_reconcile_hash": torrent_hash, "_reconcile_name": name},
                actor="reconciler",
            )
        elif job.kind == "organize_apply":
            library_id = self.settings.audiobookshelf_library_id
            if not (self.settings.audiobookshelf_url and self.settings.audiobookshelf_token and library_id):
                # The book is already filed into the library at this point --
                # Audiobookshelf's own periodic scan (or a manual `/scan`)
                # will pick it up. Not configured is a stopping point, not a
                # failure to page about.
                self._notify(
                    f":white_check_mark: **{name}** organized. Audiobookshelf is not "
                    "configured for an automatic scan, so it will appear on the next "
                    "scheduled or manual scan."
                )
                return
            self.database.enqueue(
                "library_scan",
                {"library_id": library_id, "_reconcile_hash": torrent_hash, "_reconcile_name": name},
                actor="reconciler",
            )
        elif job.kind == "library_scan":
            self._notify(f":white_check_mark: **{name}** is in the library.")

    def _notify_chain_failure(self, job: Job, message: str, code: str) -> None:
        """Page Discord when an automatic pipeline stage fails.

        Gated on `_reconcile_hash` the same way `_chain_after_success` is: a
        manually-submitted job failing (a bad `/organize-preview` path, say)
        is already visible to whoever ran it through `/job` and does not need
        an unsolicited Discord message.
        """
        torrent_hash = job.payload.get("_reconcile_hash")
        if not torrent_hash:
            return
        label = job.payload.get("_reconcile_name") or torrent_hash
        self._notify(
            f":warning: Automatic import of **{label}** failed at `{job.kind}`: "
            f"{message[:300]} (`{code}`). See `/job {job.id}`."
        )

    def _notify(self, content: str) -> None:
        """Best-effort Discord notification via a plain webhook POST.

        The worker must not import discord.py: that dependency belongs to
        discord_bot.py's slash-command adapter, a separate long-running
        process that holds a gateway connection. A Discord webhook is just an
        HTTP POST any client can make, so this reuses the same `HttpClient`
        every other integration in this file already goes through -- with its
        own circuit breaker keyed on "discord-webhook", so a Discord outage
        degrades the same way an Audiobookshelf or Prowlarr outage does,
        rather than the worker growing a second notification stack.

        Never raises: a Discord outage must not fail (or retry-loop) a job
        that already succeeded or failed on its own merits. The chain's true
        state lives in the jobs table and `/job`; this is a courtesy layered
        on top of it, not the source of truth.
        """
        url = (self.settings.discord_webhook_url or "").strip()
        if not url:
            return
        base, path = _split_webhook_url(url)
        if not base:
            logger.warning("SHELFMARK_DISCORD_WEBHOOK_URL is not a usable webhook URL")
            return
        try:
            HttpClient(
                base,
                service="discord-webhook",
                timeout=self.settings.http_timeout,
                retries=1,
            ).request(path, method="POST", json_body={"content": content[:2000]})
        except (ServiceError, ValueError):
            logger.warning("Discord webhook notification failed", exc_info=True)


def _maybe_enqueue_reconcile(database: Database, settings: Settings, now: float, last_reconcile: float) -> float:
    """Enqueue a `reconcile_downloads` job if one is due, returning the new timer value.

    Split out from `main()`'s loop so "is a reconcile due, and is one
    already in flight" is a plain function `main()`'s own infinite
    `while not stopping` loop does not have to be running for a test to
    exercise. `now`/`last_reconcile` are both `time.monotonic()` values
    (not wall clock) so a system clock adjustment can never cause a burst of
    skipped or duplicated reconciles.

    Reconciliation is enqueued as an ordinary job -- processed by
    `Worker.execute()`'s `reconcile_downloads` branch like anything else the
    worker does -- rather than run inline in this loop, so a broken
    qBittorrent login shows up in `/job` and `/api/v1/jobs` exactly like any
    other failure, instead of in a silent side channel nothing would think to
    check.
    """
    if not settings.download_automation_enabled:
        return last_reconcile
    if now - last_reconcile < settings.reconcile_interval_seconds:
        return last_reconcile
    if database.has_active_job("reconcile_downloads"):
        # One pass is already queued or running (a slow qBittorrent, or the
        # worker busy on a big organize job) -- leave `last_reconcile`
        # untouched so the NEXT loop iteration checks again immediately once
        # it clears, rather than waiting a full new interval on top.
        return last_reconcile
    database.enqueue("reconcile_downloads", {}, actor="reconciler")
    return now


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("SHELFMARK_LOG_LEVEL", "INFO"),
        format='{"level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
    )
    settings = Settings.from_env()
    database = Database(settings.database_path)
    database.initialize()
    database.requeue_stale(settings.worker_stale_seconds, actor=settings.worker_id)
    worker = Worker(database, settings)
    stopping = False

    def stop(_signum: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    # 0.0 is always "due": `time.monotonic()` starts counting from an
    # arbitrary epoch that is never negative, and this guarantees the first
    # loop iteration considers a reconcile rather than waiting a full
    # SHELFMARK_RECONCILE_INTERVAL_SECONDS after every restart.
    last_reconcile = 0.0
    while not stopping:
        database.requeue_stale(settings.worker_stale_seconds, actor=settings.worker_id)
        last_reconcile = _maybe_enqueue_reconcile(database, settings, time.monotonic(), last_reconcile)
        if not worker.run_once():
            time.sleep(settings.poll_interval)


if __name__ == "__main__":
    main()

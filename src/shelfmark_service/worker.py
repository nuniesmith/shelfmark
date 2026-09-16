"""Database-backed worker for organizer jobs."""

from __future__ import annotations

import logging
import os
import posixpath
import signal
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any

from .clients import AudiobookshelfClient, HttpClient, QBittorrentClient, ServiceError
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


def _release_download_source(release: dict[str, Any], qbittorrent_prowlarr_base_url: str) -> str:
    """Resolve the URL `grab_release` hands to qBittorrent from one release.

    Prowlarr's own `/api/v1/search` grab endpoint routes to WHATEVER download
    client Prowlarr itself has configured -- on the live system that is one
    client, with category `prowlarr`, not `shelfmark-books`. The reconciler
    only ever watches the latter, so a release grabbed that way sits in
    qBittorrent forever and never reaches the pipeline. The fix is to skip
    Prowlarr's routing entirely and hand qBittorrent the release directly, in
    the category the reconciler actually watches (see the `grab_release`
    branch of `execute()` below) -- which means resolving a URL qBittorrent
    itself can fetch, from data the release object already carries.

    A magnet URI needs no rewriting: it has no proxying host in front of it,
    just an info-hash qBittorrent resolves over DHT/trackers directly. An
    http(s) `downloadUrl` is different -- for a private tracker (IPTorrents)
    it is a PROWLARR-proxied link
    (`http://<prowlarr-host>:9696/1/download?apikey=...&link=...`): Prowlarr
    fetches the real .torrent from the tracker using its own credentials and
    serves it back, which is what lets qBittorrent fetch it with no tracker
    auth of its own. But the host in that URL is Prowlarr's OWN view of
    itself, which is not necessarily reachable from qBittorrent's network
    namespace -- verified live from inside the qBittorrent container on
    Sullivan: `sullivan:9696` (Prowlarr's own hostname) refused the
    connection, while `prowlarr:9696` -- the name both containers resolve on
    their shared `sullivan_download` Docker network -- answered fine. So only
    the scheme and host are rewritten, to `qbittorrent_prowlarr_base_url`;
    the path and the ENTIRE query string are passed through untouched,
    because `apikey` and `link` both live there and are what actually
    authorizes the download. Passing the original host through unchanged
    would have qBittorrent silently accept the add and then never fetch
    anything -- there would be no error, just nothing arriving.

    Release results never carry a `downloadClientId`, so there is no way to
    redirect Prowlarr's OWN grab to a different one of its download clients
    through the release body -- going around Prowlarr's routing entirely, as
    this function does, is the only lever available.
    """
    download_url = release.get("downloadUrl")
    magnet_url = release.get("magnetUrl")
    source = (
        download_url
        if isinstance(download_url, str) and download_url.strip()
        else magnet_url if isinstance(magnet_url, str) and magnet_url.strip() else None
    )
    if not source:
        raise ShelfmarkError(
            ErrorCode.INVALID_PAYLOAD, "release has neither a downloadUrl nor a magnetUrl"
        )
    if source.startswith("magnet:"):
        return source
    parsed = urllib.parse.urlsplit(source)
    if not parsed.scheme or not parsed.netloc:
        # Deliberately never interpolate `source` (or any part of it) into
        # this message: it is a Prowlarr downloadUrl, and its query string
        # carries Prowlarr's own apikey. A job's stored error is exactly the
        # kind of place `_upstream_failure` above already refuses to leak
        # upstream detail into -- the same rule applies here.
        raise ShelfmarkError(ErrorCode.INVALID_PAYLOAD, "release downloadUrl is not a valid http(s) URL")
    base = urllib.parse.urlsplit(qbittorrent_prowlarr_base_url)
    if not base.scheme or not base.netloc:
        raise ShelfmarkError(
            ErrorCode.PROVIDER_NOT_CONFIGURED,
            "QBITTORRENT_PROWLARR_BASE_URL is not a valid http(s) URL",
        )
    return urllib.parse.urlunsplit((base.scheme, base.netloc, parsed.path, parsed.query, parsed.fragment))


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
            # NOT ProwlarrClient.grab() -- that POSTs to Prowlarr's own
            # /api/v1/search, which hands the release to WHATEVER download
            # client Prowlarr itself has configured. On the live system that
            # is one client, fixed to category `prowlarr`, never
            # `shelfmark-books` -- so a release grabbed that way sat in
            # qBittorrent forever, in a category the reconciler does not
            # watch, and never reached the pipeline. This adds the release to
            # qBittorrent directly, in the SAME category the reconciler reads
            # (`self.settings.qbittorrent_category`), so the two can never
            # drift apart. See `_release_download_source` for how the URL
            # itself is resolved.
            if not self.settings.qbittorrent_url:
                raise ShelfmarkError(ErrorCode.PROVIDER_NOT_CONFIGURED, "qBittorrent integration is not configured")
            if not self.settings.qbittorrent_api_key and not (
                self.settings.qbittorrent_username and self.settings.qbittorrent_password
            ):
                raise ShelfmarkError(ErrorCode.PROVIDER_NOT_CONFIGURED, "qBittorrent credentials are not configured")
            release = job.payload.get("release")
            if not isinstance(release, dict) or not release:
                raise ShelfmarkError(ErrorCode.INVALID_PAYLOAD, "job payload requires a release object")
            add_url = _release_download_source(release, self.settings.qbittorrent_prowlarr_base_url)
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
            try:
                client.login()
                upstream = client.add_urls([add_url], category=self.settings.qbittorrent_category)
            except ServiceError as exc:
                raise _upstream_failure(exc) from exc
            return {"release": release, "upstream": upstream, "submitted": True}
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
        from main import (
            apply_extracts,
            apply_plan,
            build_plan,
            dirs_the_plan_empties,
            remove_empty_dirs,
        )

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
        copy = bool(payload.get("copy", False))
        apply_plan(plan, trash=trash, dry_run=False, copy=copy)
        # The CLI sweeps these (see main.py's run()); this path did not, so
        # every automatic import left the release's folder skeleton behind
        # forever — after one real download /incoming held seven empty
        # directories and no files. Not harmful, but it accumulates one tree
        # per book and makes "is anything still being imported?" unanswerable
        # by looking.
        #
        # Skipped under --copy for the same reason the CLI skips it: nothing
        # was taken out of those directories, so their emptiness is the
        # operator's, not ours to tidy.
        if not copy:
            remove_empty_dirs(source, {source, dest, trash}, dirs_the_plan_empties(plan))
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
            self._chain_transfer_completed(result, torrent_hash, name)
        elif job.kind == "organize_apply":
            self._chain_organize_apply(job, result, torrent_hash, name)
        elif job.kind == "library_scan":
            self._notify(f":white_check_mark: **{name}** is in the library.")

    def _chain_transfer_completed(self, result: dict[str, Any], torrent_hash: str, name: str) -> None:
        """Queue one `organize_apply` per configured media root.

        `source` is the whole INCOMING ROOT, never the just-landed book path.
        Two things break if it is the book path instead:

          1. `dest` would default to `source` (see `execute()`'s
             `dest = _path(payload, "dest", source)`), which means the book
             gets "organized" in place -- i.e. not organized at all, silently.
          2. Even with an explicit `dest`, `build_plan` reads author/title/
             year off the name of the folder directly under `source`. Point
             `source` AT that folder and there is no folder above it left to
             read the name from -- every track becomes its own book with no
             author, which is exactly the corruption a book showing up as
             "in the library" in this feature's own Discord notification must
             never paper over.

        Scanning the whole root instead of just this torrent's folder is safe
        specifically because the worker is single-process and runs one job at
        a time (see `Worker.run_once`): nothing else can be mid-write into
        `incoming_root` while this organize job runs, and `transfer_completed`
        never lands a folder there until its own `wait_until_stable` +
        `verify` have both passed. So everything under `incoming_root` at any
        moment is either a fully verified, complete book, or not there yet --
        never a partial tree an organize pass could catch mid-transfer.

        `dest` is never left to default, and a download can hold both an
        audiobook and an ebook -- `build_plan` takes exactly one `dest` per
        call, and Audiobookshelf and Shelfmark's own ebook index are two
        different roots. So this queues one pass per CONFIGURED root, each
        scoped to its own `media` type; a pass whose type is not present in
        this torrent's content plans zero books (see `build_plan`'s
        `do_audio`/`do_ebook` gating) rather than erroring, and its sibling
        pass still runs independently.
        """
        if not result.get("verified"):
            # execute() already raises VERIFICATION_FAILED rather than
            # returning normally when RsyncTransfer.verify() finds a mismatch
            # (see the transfer_completed branch above), so this should be
            # unreachable. It is checked again here anyway because
            # organize_apply is destructive, and "never organize an
            # unverified transfer" is a hard constraint worth enforcing at
            # the one remaining place that could chain into it, not just
            # trusted from upstream.
            return
        # Matches transfer_completed's OWN fallback (`local_root =
        # self.settings.incoming_root or Path("/incoming")`) so this scans
        # the exact directory the transfer actually wrote into, even when
        # SHELFMARK_INCOMING_ROOT is unset.
        incoming_root = self.settings.incoming_root or Path("/incoming")
        audio_root = self.settings.audio_root
        ebook_root = self.settings.ebook_root
        if audio_root is None and ebook_root is None:
            # Stop here, loudly -- rather than falling back to some default
            # destination, which is the exact shape of bug this method
            # exists to not repeat.
            self._notify(
                f":warning: Cannot organize **{name}**: neither SHELFMARK_AUDIOBOOKS_ROOT "
                "nor SHELFMARK_EBOOKS_ROOT is configured."
            )
            return
        if audio_root is not None:
            self.database.enqueue(
                "organize_apply",
                {
                    "source": str(incoming_root),
                    "dest": str(audio_root),
                    "media": "audio",
                    "_reconcile_hash": torrent_hash,
                    "_reconcile_name": name,
                },
                actor="reconciler",
            )
        if ebook_root is not None:
            self.database.enqueue(
                "organize_apply",
                {
                    "source": str(incoming_root),
                    "dest": str(ebook_root),
                    "media": "ebook",
                    "_reconcile_hash": torrent_hash,
                    "_reconcile_name": name,
                },
                actor="reconciler",
            )

    def _chain_organize_apply(self, job: Job, result: dict[str, Any], torrent_hash: str, name: str) -> None:
        """Scan (audio) or just announce (ebook) once a pass actually organized something.

        `result["books"]` is 0 when this pass's `media` type was not present
        in the torrent (an ebook-only download under the audio pass, or vice
        versa, since both passes always run when both roots are configured --
        see `_chain_transfer_completed`). That is normal, not a failure, but
        it must not chain into `library_scan` or announce "in the library":
        that was exactly the bug report -- a scan/notification fired for a
        book that never actually arrived at that pass's destination.
        """
        if not result.get("books"):
            return
        if job.payload.get("media") == "ebook":
            # Audiobookshelf has no ebook library (see README's "Ebooks"
            # section) -- Shelfmark serves ebooks by walking
            # SHELFMARK_EBOOKS_ROOT directly on every request, so there is no
            # scan step on this side of the chain; organized is the end of it.
            self._notify(f":white_check_mark: **{name}** (ebook) organized.")
            return
        library_id = self.settings.audiobookshelf_library_id
        if not (self.settings.audiobookshelf_url and self.settings.audiobookshelf_token and library_id):
            # The book is already filed into the library at this point --
            # Audiobookshelf's own periodic scan (or a manual `/scan`) will
            # pick it up. Not configured is a stopping point, not a failure
            # to page about.
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


def _maybe_record_liveness(database: Database, settings: Settings, now: float, last_liveness: float) -> float:
    """Write this worker's `worker_liveness` row if the throttle window has elapsed.

    Returns the new timer value, mirroring `_maybe_enqueue_reconcile` below
    in every respect: `now`/`last_liveness` are both `time.monotonic()`
    values, not wall clock, for the same reason -- a system clock step must
    never cause a burst of skipped or duplicated writes -- and it is split
    out from `main()` so a test can exercise "is a write due" without an
    actual infinite loop running.

    Throttled to once per `poll_interval` rather than on every single call:
    when the queue is empty, `run_once()` returns False and the loop already
    sleeps `poll_interval` before calling this again, so the throttle changes
    nothing in that steady state -- it still writes every idle iteration,
    which is the case per-job heartbeats miss. It only matters when jobs are
    queued back to back with no sleep between them: without it, a backlog of
    many quick jobs (metadata_*, grab_release, reconcile_downloads) would
    write this row once per job for no benefit, since /readyz only needs to
    know the worker ticked SOME time inside `worker_liveness_stale_seconds`,
    not the exact millisecond of its most recent job. A single-row upsert is
    cheap on its own (SQLite WAL, no fsync under synchronous=NORMAL), but a
    deep backlog of sub-second jobs could otherwise turn this into thousands
    of extra writes with zero effect on what any monitor would observe.

    Note this does NOT tick while a single job is actually executing --
    `run_once()` runs one job to completion synchronously (see the module
    docstring for why: single process, no threads), so a long transfer job
    blocks the loop, and with it this write, for its whole duration. That
    job's own `heartbeat_at` (set at claim and at completion) is the signal
    for that case; this table exists specifically for the gap heartbeats
    leave, an EMPTY queue, not to add a second clock ticking mid-job.
    """
    if now - last_liveness < settings.poll_interval:
        return last_liveness
    database.record_liveness(settings.worker_id, pid=os.getpid())
    return now


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


def _maybe_sweep_retention(database: Database, settings: Settings, now: float, last_sweep: float) -> float:
    """Delete terminal jobs (and their audit_events) past their retention window, if due.

    Mirrors `_maybe_enqueue_reconcile`/`_maybe_record_liveness` in every
    respect that matters: `now`/`last_sweep` are both `time.monotonic()`
    values, not wall clock, so a system clock step can never cause a burst
    of skipped or duplicated sweeps; it is split out of `main()` so a test
    can ask "is a sweep due" without an actual infinite loop running; and it
    runs inside THIS same single-threaded loop rather than a second thread,
    process, or cron entry, for the identical reason neither of those two
    functions does either (see the module docstring: no threads). Deleting
    is a SQLite write like any job claim, so it goes through the one loop
    that already owns every other write to this database.

    Throttled to once per `settings.retention_sweep_interval_seconds`
    (default 1h): `reconcile_downloads` ticks every
    `reconcile_interval_seconds` (60s default), so running this on every
    idle loop iteration would mean re-issuing all four retention SELECTs
    roughly sixty times more often than even the shortest tier
    (`retention_reconcile_empty_seconds`, itself 1h by default) actually
    changes -- all cost, no benefit, since nothing new becomes eligible for
    deletion between two sweeps a few seconds apart.
    """
    if now - last_sweep < settings.retention_sweep_interval_seconds:
        return last_sweep
    deleted = database.sweep_job_retention(
        reconcile_empty_retention_seconds=settings.retention_reconcile_empty_seconds,
        reconcile_claimed_or_failed_retention_seconds=settings.retention_reconcile_seconds,
        pipeline_retention_seconds=settings.retention_pipeline_seconds,
        pipeline_failed_retention_seconds=settings.retention_pipeline_failed_seconds,
    )
    total = sum(deleted.values())
    if total:
        # Silent when there is nothing to report -- an hourly "deleted
        # nothing" line for the entire lifetime of a quiet deployment would
        # be pure log noise, the same reasoning `run_once` already applies
        # by only logging when a job actually existed to claim.
        logger.info(
            "retention sweep deleted total=%s reconcile_empty=%s "
            "reconcile_claimed_or_failed=%s pipeline=%s pipeline_failed=%s",
            total,
            deleted["reconcile_empty"],
            deleted["reconcile_claimed_or_failed"],
            deleted["pipeline"],
            deleted["pipeline_failed"],
        )
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
    # loop iteration considers a reconcile -- and records this worker's first
    # liveness row -- rather than waiting a full interval after every
    # restart. A fresh deploy's very first liveness write happens here,
    # before the loop has claimed a single job, which is what keeps the
    # "never seen" window (see db.py's `latest_worker_liveness`) to well
    # under a second rather than a full `worker_liveness_stale_seconds`.
    last_reconcile = 0.0
    last_liveness = 0.0
    last_retention_sweep = 0.0
    while not stopping:
        database.requeue_stale(settings.worker_stale_seconds, actor=settings.worker_id)
        now = time.monotonic()
        last_liveness = _maybe_record_liveness(database, settings, now, last_liveness)
        last_reconcile = _maybe_enqueue_reconcile(database, settings, now, last_reconcile)
        last_retention_sweep = _maybe_sweep_retention(database, settings, now, last_retention_sweep)
        if not worker.run_once():
            time.sleep(settings.poll_interval)


def check_liveness_cli() -> None:
    """Console entry point for the `shelfmark-worker` Docker healthcheck.

    Exits 0 for `unknown`/`ok`/`busy` (nothing wrong, or too soon to judge),
    1 for `stale`. This calls `Database.worker_liveness_status` -- the exact
    same classifier `/readyz` uses in api.py -- rather than re-implementing
    any part of the rule here.

    This used to be an inline `python -c` one-liner directly in
    docker-compose.yml, checking only `worker_liveness` age with no
    exception for a job that is legitimately still running. That meant
    `docker ps` reported `shelfmark-worker` as unhealthy during any long
    transfer even after `/readyz` was fixed to say `busy` for the very same
    situation -- two health signals disagreeing, which is worse than either
    alone, since it teaches an operator not to trust the one they check
    first. A one-liner also could not grow the busy-job exception without
    becoming unreadable and untestable. A named entry point fixes both: the
    compose healthcheck becomes one word, and this function is unit-testable
    like everything else in this module.
    """
    settings = Settings.from_env()
    database = Database(settings.database_path)
    status = database.worker_liveness_status(
        settings.worker_liveness_stale_seconds, settings.transfer_timeout_seconds
    )["status"]
    sys.exit(1 if status == "stale" else 0)


if __name__ == "__main__":
    main()

"""Database-backed worker for organizer jobs."""

from __future__ import annotations

import logging
import os
import posixpath
import signal
import time
from pathlib import Path
from typing import Any

from .clients import AudiobookshelfClient, ProwlarrClient
from .config import Settings
from .db import Database, Job
from .manifest import JsonlManifest, sha256_file
from .transfer import RsyncTransfer, wait_until_stable

logger = logging.getLogger("shelfmark.worker")


class JobCancelled(RuntimeError):
    pass


def _path(payload: dict[str, Any], name: str, default: Path | None = None) -> Path:
    value = payload.get(name)
    if value is None:
        if default is None:
            raise ValueError(f"job payload requires {name}")
        return default
    return Path(str(value)).expanduser().resolve()


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
        except JobCancelled as exc:
            self.database.cancel_running(job.id, worker_id=self.worker_id)
            manifest.event("job_cancelled", job_id=job.id, reason=str(exc))
            logger.info("job cancelled id=%s reason=%s", job.id, exc)
        except Exception as exc:  # noqa: BLE001 - failure belongs in the job record
            self.database.fail(job.id, self.worker_id, str(exc))
            manifest.event("job_failed", job_id=job.id, error=str(exc))
            logger.exception("job failed id=%s", job.id)
        return True

    def execute(self, job: Job) -> dict[str, Any]:
        if job.kind in {"metadata_update", "metadata_match", "library_scan"}:
            if not self.settings.audiobookshelf_url or not self.settings.audiobookshelf_token:
                raise ValueError("Audiobookshelf integration is not configured")
            client = AudiobookshelfClient(
                self.settings.audiobookshelf_url,
                self.settings.audiobookshelf_token,
                timeout=self.settings.http_timeout,
                retries=self.settings.http_retries,
            )
            if job.kind == "metadata_update":
                item_id = str(job.payload.get("item_id", ""))
                media = job.payload.get("media")
                if not item_id or not isinstance(media, dict):
                    raise ValueError("metadata_update requires item_id and media")
                return {"item_id": item_id, "upstream": client.update_media(item_id, media), "updated": True}
            if job.kind == "metadata_match":
                item_id = str(job.payload.get("item_id", ""))
                if not item_id:
                    raise ValueError("metadata_match requires item_id")
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
                raise ValueError("library_scan requires library_id")
            return {
                "library_id": library_id,
                "upstream": client.scan(library_id, force=bool(job.payload.get("force", False))),
                "scan_started": True,
            }
        if job.kind == "transfer_completed":
            if not self.settings.sullivan_host or not self.settings.sullivan_user:
                raise ValueError("Sullivan transfer is not configured")
            raw_remote = str(job.payload.get("remote_path", "")).strip()
            if not raw_remote:
                raise ValueError("job payload requires remote_path")
            base_remote = posixpath.normpath(self.settings.sullivan_completed_root)
            remote = posixpath.normpath(
                raw_remote if raw_remote.startswith("/") else posixpath.join(base_remote, raw_remote)
            )
            if remote != base_remote and not remote.startswith(base_remote.rstrip("/") + "/"):
                raise ValueError("remote_path must stay within Sullivan's Shelfmark category")
            local_root = self.settings.incoming_root or Path("/incoming")
            local = _path(job.payload, "local_path", local_root)
            if local != local_root and local_root not in local.parents:
                raise ValueError("local_path must stay within the configured incoming root")
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
            transfer.pull(remote, local)
            # Where the book actually landed. rsync now preserves the remote
            # directory name, so the tree is under local/<name> rather than
            # loose in the incoming root — and it is that path an organize job
            # has to be pointed at, not the shared root holding every download.
            name = posixpath.basename(remote.rstrip("/"))
            landed = local / name if name else local
            snapshot = wait_until_stable(
                landed,
                settle_seconds=self.settings.transfer_settle_seconds,
                poll_seconds=self.settings.transfer_poll_seconds,
                timeout_seconds=self.settings.transfer_timeout_seconds,
            )
            # Verify AFTER the tree has settled, not straight after the pull:
            # comparing a tree still being written reports differences that are
            # simply the write in progress.
            differences = transfer.verify(remote, local)
            if differences:
                raise RuntimeError(
                    "transfer does not match Sullivan after copying "
                    f"({len(differences)} file(s) differ): {', '.join(differences[:5])}"
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
                raise ValueError("Prowlarr integration is not configured")
            release = job.payload.get("release")
            if not isinstance(release, dict) or not release:
                raise ValueError("job payload requires a release object")
            client = ProwlarrClient(
                self.settings.prowlarr_url,
                self.settings.prowlarr_api_key,
                timeout=self.settings.http_timeout,
                retries=self.settings.http_retries,
            )
            return {"release": release, "upstream": client.grab(release), "submitted": True}
        if job.kind not in {"organize_preview", "organize_apply"}:
            raise ValueError(f"unsupported job kind: {job.kind}")
        payload = job.payload
        source = _path(payload, "source")
        if not source.is_dir():
            raise ValueError(f"source is not a directory: {source}")
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
                raise RuntimeError("; ".join(errors))
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
    while not stopping:
        database.requeue_stale(settings.worker_stale_seconds, actor=settings.worker_id)
        if not worker.run_once():
            time.sleep(settings.poll_interval)


if __name__ == "__main__":
    main()

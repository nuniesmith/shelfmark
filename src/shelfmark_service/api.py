"""FastAPI surface for job submission and health checks.

This is intentionally a small internal API.  Search providers, Audiobookshelf,
and Discord adapters will be added behind the same job model after the service
has been staged on Freddy.
"""

from __future__ import annotations

import secrets
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .clients import AudiobookshelfClient, ProwlarrClient, QBittorrentClient, ServiceError
from .config import Settings
from .db import Database, Job
from .ebooks import EbookNotFound, list_ebooks, resolve_ebook


settings = Settings.from_env()
database = Database(settings.database_path)


class JobRequest(BaseModel):
    kind: Literal[
        "organize_preview",
        "organize_apply",
        "grab_release",
        "metadata_update",
        "metadata_match",
        "library_scan",
    ]
    payload: dict[str, Any] = Field(default_factory=dict)


class ReleaseGrabRequest(BaseModel):
    release: dict[str, Any]


class TransferRequest(BaseModel):
    remote_path: str = Field(min_length=1, max_length=500)
    local_path: str | None = Field(default=None, max_length=500)


class MetadataUpdateRequest(BaseModel):
    media: dict[str, Any]


class MetadataMatchRequest(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    author: str | None = Field(default=None, max_length=300)
    provider: str | None = Field(default=None, max_length=80)
    isbn: str | None = Field(default=None, max_length=40)
    asin: str | None = Field(default=None, max_length=40)
    override_defaults: bool = False


class LibraryScanRequest(BaseModel):
    force: bool = False


def _job_response(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "attempts": job.attempts,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "heartbeat_at": job.heartbeat_at,
        "worker_id": job.worker_id,
        "error": job.error,
        "code": job.error_code,
        "result": job.result,
        "cancel_requested": job.cancel_requested,
    }


def _actor(request: Request) -> str:
    expected = settings.api_token
    if not expected:
        # The API is intended to be bound to Freddy's private network until an
        # external identity provider is configured.
        return "local"
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.casefold() != "bearer" or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    actor = request.headers.get("x-shelfmark-actor", "bearer").strip()
    return actor[:200] or "bearer"


def _abs_client() -> AudiobookshelfClient:
    if not all(
        (
            settings.audiobookshelf_url,
            settings.audiobookshelf_token,
            settings.audiobookshelf_library_id,
        )
    ):
        raise HTTPException(status_code=503, detail="Audiobookshelf integration is not configured")
    return AudiobookshelfClient(
        settings.audiobookshelf_url or "",
        settings.audiobookshelf_token or "",
        timeout=settings.http_timeout,
        retries=settings.http_retries,
        breaker_failure_threshold=settings.circuit_breaker_failure_threshold,
        breaker_cooldown_seconds=settings.circuit_breaker_cooldown_seconds,
    )


def _prowlarr_client() -> ProwlarrClient:
    if not settings.prowlarr_url or not settings.prowlarr_api_key:
        raise HTTPException(status_code=503, detail="Prowlarr integration is not configured")
    return ProwlarrClient(
        settings.prowlarr_url,
        settings.prowlarr_api_key,
        timeout=settings.http_timeout,
        retries=settings.http_retries,
        breaker_failure_threshold=settings.circuit_breaker_failure_threshold,
        breaker_cooldown_seconds=settings.circuit_breaker_cooldown_seconds,
    )


def _qbittorrent_client() -> QBittorrentClient:
    if not settings.qbittorrent_url:
        raise HTTPException(status_code=503, detail="qBittorrent integration is not configured")
    if not settings.qbittorrent_api_key and not (
        settings.qbittorrent_username and settings.qbittorrent_password
    ):
        raise HTTPException(status_code=503, detail="qBittorrent credentials are not configured")
    return QBittorrentClient(
        settings.qbittorrent_url,
        username=settings.qbittorrent_username,
        password=settings.qbittorrent_password,
        api_key=settings.qbittorrent_api_key,
        timeout=settings.http_timeout,
        retries=settings.http_retries,
        breaker_failure_threshold=settings.circuit_breaker_failure_threshold,
        breaker_cooldown_seconds=settings.circuit_breaker_cooldown_seconds,
    )


def _ebook_root() -> Path:
    if not settings.ebook_root:
        raise HTTPException(status_code=503, detail="The ebooks folder is not configured")
    return settings.ebook_root


def _upstream_error(exc: ServiceError) -> HTTPException:
    # Do not return upstream response bodies: they can contain release URLs,
    # credentials, or other data that should stay in service logs.
    return HTTPException(
        status_code=502,
        detail={
            "service": exc.service,
            "status": exc.status,
            "message": "upstream request failed",
        },
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    database.initialize()
    yield


app = FastAPI(title="Shelfmark API", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "shelfmark"}


def _worker_liveness() -> dict[str, Any]:
    """The `worker` object /readyz reports: unknown / ok / busy / stale.

    A thin wrapper around `Database.worker_liveness_status` -- the ONE
    implementation of this rule, shared with worker.py's
    `check_liveness_cli` (the `shelfmark-worker` Docker healthcheck). See
    that method's docstring for the full rule and for why it used to be
    two separate implementations that could (and did) disagree.
    """
    return database.worker_liveness_status(
        settings.worker_liveness_stale_seconds, settings.transfer_timeout_seconds
    )


@app.get("/readyz")
def readyz() -> dict[str, Any]:
    try:
        database.initialize()
        missing = [name for name, path in settings.configured_roots().items() if not path.is_dir()]
    except OSError as exc:
        raise HTTPException(status_code=503, detail={"status": "not_ready", "error": str(exc)}) from exc
    if missing:
        raise HTTPException(status_code=503, detail={"status": "not_ready", "missing": missing})
    worker = _worker_liveness()
    if worker["status"] == "stale":
        # A 200 saying "worker stale" in the body is invisible to a plain
        # HTTP-up monitor (Uptime Kuma included) -- it only reads the status
        # code. /readyz, not /healthz, is where this belongs: the API
        # process itself is fine (that's what /healthz asserts, and it must
        # keep asserting only that -- Compose's own healthcheck for
        # shelfmark-api targets /healthz, and restarting the API container
        # would do nothing to revive a dead worker in a different
        # container). A stale worker is exactly the kind of "a dependency
        # this service relies on is unavailable" fact /readyz already
        # reports for missing media roots, so it fails the same way: 503.
        raise HTTPException(status_code=503, detail={"status": "not_ready", "worker": worker})
    return {"status": "ready", "database": str(settings.database_path), "worker": worker}


@app.get("/api/v1/library/search")
def library_search(
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=12, ge=1, le=100),
    _actor: str = Depends(_actor),
) -> dict[str, Any]:
    client = _abs_client()
    try:
        return {
            "results": client.search(settings.audiobookshelf_library_id or "", q, limit)
        }
    except ServiceError as exc:
        raise _upstream_error(exc) from exc


@app.get("/api/v1/items/{item_id}")
def library_item(item_id: str, _actor: str = Depends(_actor)) -> Any:
    try:
        return _abs_client().get_item(item_id, expanded=True)
    except ServiceError as exc:
        raise _upstream_error(exc) from exc


@app.patch("/api/v1/items/{item_id}/media", status_code=status.HTTP_202_ACCEPTED)
def update_metadata(
    item_id: str, request: MetadataUpdateRequest, actor: str = Depends(_actor)
) -> dict[str, Any]:
    if not settings.audiobookshelf_url or not settings.audiobookshelf_token:
        raise HTTPException(status_code=503, detail="Audiobookshelf integration is not configured")
    return _job_response(
        database.enqueue(
            "metadata_update",
            {"item_id": item_id, "media": request.media},
            actor=actor,
        )
    )


@app.post("/api/v1/items/{item_id}/match", status_code=status.HTTP_202_ACCEPTED)
def match_metadata(
    item_id: str, request: MetadataMatchRequest, actor: str = Depends(_actor)
) -> dict[str, Any]:
    if not settings.audiobookshelf_url or not settings.audiobookshelf_token:
        raise HTTPException(status_code=503, detail="Audiobookshelf integration is not configured")
    payload = request.model_dump(exclude_none=True)
    payload["item_id"] = item_id
    return _job_response(database.enqueue("metadata_match", payload, actor=actor))


@app.post("/api/v1/libraries/{library_id}/scan", status_code=status.HTTP_202_ACCEPTED)
def scan_library(
    library_id: str, request: LibraryScanRequest, actor: str = Depends(_actor)
) -> dict[str, Any]:
    if not settings.audiobookshelf_url or not settings.audiobookshelf_token:
        raise HTTPException(status_code=503, detail="Audiobookshelf integration is not configured")
    return _job_response(
        database.enqueue(
            "library_scan",
            {"library_id": library_id, "force": request.force},
            actor=actor,
        )
    )


@app.get("/api/v1/releases/search")
def release_search(
    q: str = Query(min_length=1, max_length=200),
    search_type: str | None = Query(default=None, alias="type", max_length=40),
    categories: list[int] | None = Query(default=None),
    book_only: bool = Query(
        default=False,
        description=(
            "Restrict to the configured book categories (PROWLARR_BOOK_CATEGORIES, "
            "default 7000) instead of naming a category id the indexer may not advertise."
        ),
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _actor: str = Depends(_actor),
) -> dict[str, Any]:
    # `/ebook-request` sends book_only=true rather than a hardcoded category
    # id: the one indexer configured here advertises 7000/7010/7030/7050 but
    # neither 7020 (EBook) nor 7060 (Audiobook), so a literal 7020 filter
    # would silently return zero results every time.
    if book_only and categories is None:
        categories = list(settings.prowlarr_book_categories)
    try:
        return {
            "results": _prowlarr_client().search(
                q, search_type=search_type, categories=categories, limit=limit, offset=offset
            )
        }
    except ServiceError as exc:
        raise _upstream_error(exc) from exc


@app.get("/api/v1/ebooks/search")
def ebook_search(
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=10, ge=1, le=25),
    _actor: str = Depends(_actor),
) -> dict[str, Any]:
    root = _ebook_root()
    return {
        "results": [
            {
                "id": book.id,
                "title": book.title,
                "author": book.author,
                "relpath": book.relpath,
                "size": book.size,
                "ext": book.ext,
            }
            for book in list_ebooks(root, q, limit)
        ]
    }


@app.get("/api/v1/ebooks/{ebook_id}/download")
def ebook_download(ebook_id: str, actor: str = Depends(_actor)) -> FileResponse:
    root = _ebook_root()
    try:
        path = resolve_ebook(root, ebook_id)
    except EbookNotFound as exc:
        # Deliberately the same 404 whether the id is malformed, unknown, or
        # named a file whose resolved location fell outside the root — never
        # confirm anything about what does or doesn't exist on disk.
        raise HTTPException(status_code=404, detail="ebook not found") from exc
    response = FileResponse(path, filename=path.name, media_type="application/octet-stream")
    # The bot fetches this over plain urllib (see discord_bot.fetch_ebook),
    # not the shared HttpClient, because HttpClient decodes every response as
    # UTF-8 text and that corrupts binary epub/pdf/mobi bytes. Content-
    # Disposition parsing to recover a filename is unnecessary work when a
    # dedicated header can just carry it, percent-encoded in case of accents
    # in an author or title.
    response.headers["X-Shelfmark-Filename"] = urllib.parse.quote(path.name)
    return response


@app.get("/api/v1/downloads")
def downloads(
    category: str = Query(default="shelfmark-books", min_length=1, max_length=100),
    _actor: str = Depends(_actor),
) -> dict[str, Any]:
    client = _qbittorrent_client()
    try:
        client.login()
        return {"downloads": client.torrents(category=category)}
    except ServiceError as exc:
        raise _upstream_error(exc) from exc


@app.post("/api/v1/releases/grab", status_code=status.HTTP_202_ACCEPTED)
def grab_release(request: ReleaseGrabRequest, actor: str = Depends(_actor)) -> dict[str, Any]:
    if not settings.prowlarr_url or not settings.prowlarr_api_key:
        raise HTTPException(status_code=503, detail="Prowlarr integration is not configured")
    return _job_response(database.enqueue("grab_release", {"release": request.release}, actor=actor))


@app.post("/api/v1/transfers/pull", status_code=status.HTTP_202_ACCEPTED)
def pull_transfer(request: TransferRequest, actor: str = Depends(_actor)) -> dict[str, Any]:
    if not settings.sullivan_host or not settings.sullivan_user:
        raise HTTPException(status_code=503, detail="Sullivan transfer is not configured")
    payload = {"remote_path": request.remote_path}
    if request.local_path:
        payload["local_path"] = request.local_path
    return _job_response(database.enqueue("transfer_completed", payload, actor=actor))


@app.get("/api/v1/jobs")
def list_jobs(
    _actor: str = Depends(_actor),
    job_status: Literal["queued", "running", "succeeded", "failed", "cancelled"] | None = Query(
        default=None, alias="status"
    ),
    limit: int = Query(default=50, ge=1, le=200),
    include_reconciler: bool = Query(default=False),
) -> dict[str, Any]:
    """List recent jobs, newest first.

    `include_reconciler` defaults to False: `reconcile_downloads` ticks once
    every `SHELFMARK_RECONCILE_INTERVAL_SECONDS` (60s default) whether or
    not there is anything to import, and on the live database it was 98.6%
    of every job row and, at one point, the last 40 rows in a row --
    burying every real pipeline job (grab, transfer, organize, scan) under
    reconciler noise. `Database.list_jobs` itself still defaults to True
    (every existing caller keeps seeing every kind unless it asks
    otherwise) -- this endpoint is the one place that flips the default,
    since it is the one a human actually reads. Pass
    `?include_reconciler=true` to see reconciler ticks again, e.g. to check
    the reconciler is alive at all.
    """
    return {
        "jobs": [
            _job_response(job)
            for job in database.list_jobs(job_status, limit, include_reconciler=include_reconciler)
        ]
    }


@app.post("/api/v1/jobs", status_code=status.HTTP_202_ACCEPTED)
def create_job(request: JobRequest, actor: str = Depends(_actor)) -> dict[str, Any]:
    return _job_response(database.enqueue(request.kind, request.payload, actor=actor))


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str, _actor: str = Depends(_actor)) -> dict[str, Any]:
    job = database.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_response(job)


@app.post("/api/v1/jobs/{job_id}/cancel")
def cancel_job(job_id: str, actor: str = Depends(_actor)) -> dict[str, Any]:
    job = database.cancel(job_id, actor=actor)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_response(job)


def main() -> None:
    import uvicorn

    uvicorn.run(
        "shelfmark_service.api:app",
        host="0.0.0.0",
        port=8000,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()

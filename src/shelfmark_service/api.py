"""FastAPI surface for job submission and health checks.

This is intentionally a small internal API.  Search providers, Audiobookshelf,
and Discord adapters will be added behind the same job model after the service
has been staged on Freddy.
"""

from __future__ import annotations

import secrets
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from .clients import AudiobookshelfClient, ProwlarrClient, ServiceError
from .config import Settings
from .db import Database, Job


settings = Settings.from_env()
database = Database(settings.database_path)


class JobRequest(BaseModel):
    kind: Literal["organize_preview", "organize_apply", "grab_release"]
    payload: dict[str, Any] = Field(default_factory=dict)


class ReleaseGrabRequest(BaseModel):
    release: dict[str, Any]


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
    return "bearer"


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
    )


def _prowlarr_client() -> ProwlarrClient:
    if not settings.prowlarr_url or not settings.prowlarr_api_key:
        raise HTTPException(status_code=503, detail="Prowlarr integration is not configured")
    return ProwlarrClient(
        settings.prowlarr_url,
        settings.prowlarr_api_key,
        timeout=settings.http_timeout,
        retries=settings.http_retries,
    )


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


@app.get("/readyz")
def readyz() -> dict[str, Any]:
    try:
        database.initialize()
        missing = [name for name, path in settings.configured_roots().items() if not path.is_dir()]
    except OSError as exc:
        raise HTTPException(status_code=503, detail={"status": "not_ready", "error": str(exc)}) from exc
    if missing:
        raise HTTPException(status_code=503, detail={"status": "not_ready", "missing": missing})
    return {"status": "ready", "database": str(settings.database_path)}


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


@app.get("/api/v1/releases/search")
def release_search(
    q: str = Query(min_length=1, max_length=200),
    search_type: str | None = Query(default=None, alias="type", max_length=40),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _actor: str = Depends(_actor),
) -> dict[str, Any]:
    try:
        return {
            "results": _prowlarr_client().search(
                q, search_type=search_type, limit=limit, offset=offset
            )
        }
    except ServiceError as exc:
        raise _upstream_error(exc) from exc


@app.post("/api/v1/releases/grab", status_code=status.HTTP_202_ACCEPTED)
def grab_release(request: ReleaseGrabRequest, actor: str = Depends(_actor)) -> dict[str, Any]:
    if not settings.prowlarr_url or not settings.prowlarr_api_key:
        raise HTTPException(status_code=503, detail="Prowlarr integration is not configured")
    return _job_response(database.enqueue("grab_release", {"release": request.release}, actor=actor))


@app.get("/api/v1/jobs")
def list_jobs(
    _actor: str = Depends(_actor),
    job_status: Literal["queued", "running", "succeeded", "failed", "cancelled"] | None = Query(
        default=None, alias="status"
    ),
    limit: int = Query(default=50, ge=1, le=200),
) -> dict[str, Any]:
    return {"jobs": [_job_response(job) for job in database.list_jobs(job_status, limit)]}


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

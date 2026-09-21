"""FastAPI surface for job submission and health checks.

This is intentionally a small internal API.  Search providers, Audiobookshelf,
and Discord adapters will be added behind the same job model after the service
has been staged on Freddy.
"""

from __future__ import annotations

import secrets
import threading
import time
import urllib.parse
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal

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


# `_actor` falls back to exactly these two literal strings when no Discord
# identity is attached to the call: "local" when SHELFMARK_API_TOKEN isn't
# set at all (the API is meant to be bound to Freddy's private network, i.e.
# the operator only), "bearer" when a valid bearer token was presented but
# the caller didn't set X-Shelfmark-Actor (a manual curl, a monitoring
# script). Both are exempted from rate limiting entirely rather than "very
# generously limited": the tracker-account risk this feature exists for is a
# permitted DISCORD USER's runaway loop (the bot always sends a real
# `discord:<user>:<guild>:<channel>` actor), not the operator's own terminal.
# Deliberately an exact-match set, not "anything not discord:-prefixed" --
# X-Shelfmark-Actor is caller-supplied once the bearer token checks out (the
# same trust already extended to it for audit-log attribution before this
# change), so only the two sentinel values `_actor()` itself produces are
# exempt, not any custom label a bearer-token holder chooses to send.
_RATE_LIMIT_EXEMPT_ACTORS = frozenset({"local", "bearer"})


@dataclass
class _RateLimitBucket:
    """One actor's token-bucket state for one tier (read or action).

    Plain data with no lock of its own -- RateLimiter below holds ONE lock
    for the whole table rather than one per bucket. Buckets are cheap,
    short-lived dict entries and the traffic here (two trusted Discord
    users) never makes per-bucket locking worth the extra complexity.
    """

    tokens: float
    updated_at: float


def _consume_token(
    bucket: _RateLimitBucket, now: float, capacity: float, refill_per_second: float
) -> float | None:
    """Try to take one token from `bucket`, refilling it for elapsed time first.

    A continuously-refilling bucket, not a fixed calendar window, so a burst
    landing on a window boundary can't get double budget -- 10 requests at
    0:59.9 plus 10 more at 0:60.1 would be 20 in a fraction of a second under
    a fixed-window design; a bucket refilling every tick never allows that.

    Split out as its own function, and left to mutate the bucket it's
    handed rather than reach into a dict/lock itself, so the decision can be
    tested directly against explicit `now` values -- no sleeping, no
    FastAPI object graph -- the same reason `is_permitted` and `_too_large`
    live in discord_bot.py as plain functions. `RateLimiter.check` below is
    the only caller in production; tests call this directly too (see
    tests/test_api.py's `ConsumeTokenTests`).

    Returns None when the request is allowed (a token was spent), or the
    number of seconds until the next token becomes available otherwise --
    exactly the number a 429's "try again in Ns" message needs.
    """
    elapsed = max(0.0, now - bucket.updated_at)
    bucket.tokens = min(capacity, bucket.tokens + elapsed * refill_per_second)
    bucket.updated_at = now
    if bucket.tokens >= 1.0:
        bucket.tokens -= 1.0
        return None
    if refill_per_second <= 0:
        # Only reachable if a caller builds a Settings with a non-positive
        # window directly (from_env's _float_from_env floors it at 0.1) --
        # treat "never refills" as "wait forever" rather than divide by zero.
        return float("inf")
    return (1.0 - bucket.tokens) / refill_per_second


# A ceiling on how many (tier, actor) buckets are tracked at once. Two
# Discord users across a handful of channels need under ten; the headroom is
# for the operator's own scripts and anything else that sets an actor label.
_MAX_TRACKED_ACTORS = 512
_IDLE_EVICTION_SECONDS = 3600.0


class RateLimiter:
    """Per-actor, per-tier token buckets, held in this process's memory.

    In-memory and per-process is safe here ONLY because shelfmark-api runs
    as a single process: `main()` below calls `uvicorn.run(...)` with no
    `workers=` argument (uvicorn defaults to 1), and docker-compose.yml runs
    exactly one `shelfmark-api` container from a plain `command:
    ["shelfmark-api"]` -- nothing forks multiple copies of this table. If
    that ever changes (more uvicorn workers, multiple replicas behind a
    proxy), each process would keep its own counts and this would silently
    allow N times the configured limit -- move the state to something
    shared (Redis, or the sqlite database already used elsewhere) before
    doing either.

    Mirrors clients.CircuitBreaker's shape on purpose (injectable clock,
    one lock guarding a small dict, a `check`/`before_call`-style method) --
    the same trade-offs applied there (module-scoped shared state, a clock
    tests can control instead of sleeping) apply to this table too.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_actors: int = _MAX_TRACKED_ACTORS,
        idle_eviction_seconds: float = _IDLE_EVICTION_SECONDS,
    ):
        self._clock = clock
        self._lock = threading.Lock()
        self._max_actors = max_actors
        self._idle_eviction_seconds = idle_eviction_seconds
        self._buckets: dict[tuple[str, str], _RateLimitBucket] = {}

    def _evict_idle(self, now: float) -> None:
        """Drop buckets nobody has touched in an hour. Caller holds the lock.

        Keying on the actor string makes this table grow with DISTINCT
        callers, and the actor is caller-supplied: `_actor` returns whatever
        X-Shelfmark-Actor says once the bearer token checks out, so a client
        sending a new label per request would otherwise grow this dict
        without bound for the life of the container.

        Evicting an idle bucket is free rather than a trade-off, which is
        why an hour is the threshold and not a tuned number: both windows
        are measured in SECONDS, so a bucket untouched for an hour has long
        since refilled to capacity, and a full bucket is indistinguishable
        from the fresh one `check` would create in its place. Nobody gains
        or loses budget.
        """
        cutoff = now - self._idle_eviction_seconds
        for key in [key for key, b in self._buckets.items() if b.updated_at < cutoff]:
            del self._buckets[key]
        # If every tracked actor is genuinely recent, evict the stalest one
        # anyway so the table is bounded by a number rather than by a rate.
        # Dropping a bucket only ever GIVES its actor budget back, so the
        # worst case of getting this wrong is one caller being treated
        # generously -- never a real user refused to save memory.
        while len(self._buckets) >= self._max_actors:
            stalest = min(self._buckets, key=lambda k: self._buckets[k].updated_at)
            del self._buckets[stalest]

    def check(
        self, tier: str, actor: str, *, capacity: float, refill_per_second: float
    ) -> float | None:
        """Returns None if `actor` may proceed under `tier`'s budget, else the
        number of seconds until it may retry."""
        now = self._clock()
        key = (tier, actor)
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self._max_actors:
                    self._evict_idle(now)
                # A brand new actor starts with a FULL bucket, not an empty
                # one -- the first search or grab of a session must never be
                # the one that gets refused.
                bucket = _RateLimitBucket(tokens=capacity, updated_at=now)
                self._buckets[key] = bucket
            return _consume_token(bucket, now, capacity, refill_per_second)


# Module-scoped, like clients._BREAKER_REGISTRY, so every request in this
# one process shares the same actor -> bucket table for the life of the
# container.
_rate_limiter = RateLimiter()


def _rate_limited_error(retry_after_seconds: float, tier: str) -> HTTPException:
    """Build the 429 for a rate-limited request.

    Includes BOTH a machine-readable `retry_after_seconds` in the JSON body
    (discord_bot.py reads this to build "try again in N minutes" -- a raw
    status code or a bare body means nothing to someone tapping a button on
    their phone) and a standard `Retry-After` header (for any other client
    that knows to look for it, e.g. curl or a future non-Discord caller).
    Unlike `_upstream_error`, this body is entirely ours to construct -- there
    is no upstream response to accidentally leak here -- so it can say
    exactly what happened.
    """
    retry_after = max(1, int(retry_after_seconds + 0.999))  # round UP; never advertise 0s
    kind = "searches" if tier == "read" else "actions"
    plural = "s" if retry_after != 1 else ""
    message = f"Too many {kind} from this user. Try again in {retry_after} second{plural}."
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={"error": "rate_limited", "retry_after_seconds": retry_after, "message": message},
        headers={"Retry-After": str(retry_after)},
    )


def _enforce_rate_limit(tier: str, actor: str) -> None:
    """Raise 429 if `actor` has exhausted `tier`'s budget this window.

    THE reason this exists, not a generic anti-abuse measure: every
    `/api/v1/releases/search` call is a LIVE search against IPTorrents
    through Prowlarr -- the one indexer configured here -- and every
    `/api/v1/releases/grab` hands qBittorrent a URL that fetches the
    .torrent through Prowlarr's own proxy, another real hit on that same
    private-tracker account. A slip, a stuck retry loop, or someone holding
    down a Discord button hammers a tracker account that can be rate-limited
    or flagged for abuse by IPTorrents itself -- and losing that account is
    not something a redeploy fixes. This function is the ONE place that risk
    is capped, reached from every route that can get to Prowlarr or enqueue
    work toward it (see `_read_actor`/`_action_actor` below), regardless of
    which Discord command -- or bug in the bot -- got here.
    """
    if actor in _RATE_LIMIT_EXEMPT_ACTORS:
        return
    if tier == "read":
        capacity = settings.rate_limit_read_max_requests
        window = settings.rate_limit_read_window_seconds
    else:
        capacity = settings.rate_limit_action_max_requests
        window = settings.rate_limit_action_window_seconds
    refill_per_second = capacity / window if window > 0 else float("inf")
    retry_after = _rate_limiter.check(tier, actor, capacity=capacity, refill_per_second=refill_per_second)
    if retry_after is not None:
        raise _rate_limited_error(retry_after, tier)


def _read_actor(actor: str = Depends(_actor)) -> str:
    """Actor dependency for every GET route except /healthz and /readyz.

    A generous budget whose only job is stopping a runaway loop -- see
    Settings.rate_limit_read_max_requests for the numbers and why they were
    chosen. Deliberately never used by healthz/readyz: neither route takes
    this (or any actor) dependency at all, so a monitoring probe (Uptime
    Kuma polls /readyz every 60s) can never be rate-limited by construction,
    not by a case in this function remembering to skip it.
    """
    _enforce_rate_limit("read", actor)
    return actor


def _action_actor(actor: str = Depends(_actor)) -> str:
    """Actor dependency for every mutating route -- grabs, transfer pulls,
    job creation/cancellation, metadata updates/matches, library scans.

    A much tighter budget than `_read_actor`'s -- see
    Settings.rate_limit_action_max_requests for the numbers and why.
    """
    _enforce_rate_limit("action", actor)
    return actor


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
    q: str = Query(default="", max_length=200),
    limit: int = Query(default=25, ge=1, le=100),
    _actor: str = Depends(_read_actor),
) -> dict[str, Any]:
    """Audiobooks already on the server. An empty `q` lists the library.

    `results` is a FLAT LIST of library items, always. It used to be
    whatever Audiobookshelf returned, which for a search is an object
    (`{"book": [...], "authors": [...], "series": [...]}`) — and the bot's
    `_result_list` looks for a list under "results" and then for a top-level
    "book", so it found neither and every `/library type:audiobook` search
    answered "No matching library items found." for a library of 190 books.
    The unwrapping belongs here, where there is one shape to produce, rather
    than in a caller that has to guess which of two Audiobookshelf endpoints
    its payload came from.
    """
    client = _abs_client()
    library_id = settings.audiobookshelf_library_id or ""
    try:
        if q:
            payload = client.search(library_id, q, limit)
            entries = payload.get("book", []) if isinstance(payload, dict) else []
        else:
            # Author order, matching how the library reads on disk
            # (`/audiobooks/<author>/<year> - <title>/`), so browsing it in
            # Discord and browsing it in a file manager agree.
            payload = client.list_items(
                library_id, limit=limit, sort="media.metadata.authorName"
            )
            entries = payload.get("results", []) if isinstance(payload, dict) else []
    except ServiceError as exc:
        raise _upstream_error(exc) from exc
    return {
        "results": [
            entry.get("libraryItem", entry) if isinstance(entry, dict) else entry
            for entry in entries
        ]
    }


@app.get("/api/v1/items/{item_id}")
def library_item(item_id: str, _actor: str = Depends(_read_actor)) -> Any:
    try:
        return _abs_client().get_item(item_id, expanded=True)
    except ServiceError as exc:
        raise _upstream_error(exc) from exc


@app.patch("/api/v1/items/{item_id}/media", status_code=status.HTTP_202_ACCEPTED)
def update_metadata(
    item_id: str, request: MetadataUpdateRequest, actor: str = Depends(_action_actor)
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
    item_id: str, request: MetadataMatchRequest, actor: str = Depends(_action_actor)
) -> dict[str, Any]:
    if not settings.audiobookshelf_url or not settings.audiobookshelf_token:
        raise HTTPException(status_code=503, detail="Audiobookshelf integration is not configured")
    payload = request.model_dump(exclude_none=True)
    payload["item_id"] = item_id
    return _job_response(database.enqueue("metadata_match", payload, actor=actor))


@app.post("/api/v1/libraries/{library_id}/scan", status_code=status.HTTP_202_ACCEPTED)
def scan_library(
    library_id: str, request: LibraryScanRequest, actor: str = Depends(_action_actor)
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
    media_type: Literal["ebook", "audiobook"] | None = Query(
        default=None,
        description=(
            "Restrict to one media type's configured categories: 'ebook' uses "
            "PROWLARR_BOOK_CATEGORIES (default 7000), 'audiobook' uses "
            "PROWLARR_AUDIOBOOK_CATEGORIES (default 3030,100064 — the standard "
            "Newznab Audiobook bucket plus this indexer's own AudioBook "
            "category; book_only's 7000 alone returns zero audiobooks, "
            "measured against the live indexer). Takes precedence over "
            "book_only when set — see that parameter for why they coexist."
        ),
    ),
    book_only: bool = Query(
        # Defaults to TRUE. Shelfmark is a book library, and Prowlarr indexes
        # everything — with no category filter, `/release-search dune` came
        # back with "Dune Part Two 2024 BluRay 1080p" (category 2050) and a
        # Car SOS episode about a dune buggy (5010), and not one book. The
        # operator's wife hit exactly that on her first real search.
        #
        # Defaulting to False made the unfiltered, useless answer the one you
        # get by forgetting a parameter. Pass book_only=false to search every
        # category deliberately; nothing here does.
        #
        # Kept alongside `media_type` rather than replaced by it: book_only is
        # the generic "this is a book library, filter out the movies and TV"
        # switch and stays the default for any caller that doesn't know or
        # care whether it wants an ebook or an audiobook specifically.
        # `media_type` is strictly more specific — ebook-vs-audiobook — and
        # wins when both would apply, since `/request` always sends it.
        default=True,
        description=(
            "Restrict to the configured book categories (PROWLARR_BOOK_CATEGORIES, "
            "default 7000) instead of naming a category id the indexer may not "
            "advertise. Defaults to true: this is a book library. Ignored when "
            "media_type is set."
        ),
    ),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _actor: str = Depends(_read_actor),
) -> dict[str, Any]:
    # `/request` sends media_type rather than a hardcoded category id: the one
    # indexer configured here advertises 7000/7010/7030/7050 for books and
    # 3030/100064 for audiobooks, but neither 7020 (EBook specifically) nor
    # 7060 (a made-up "Audiobook" id) — a literal filter on either of those
    # would silently return zero results every time.
    if categories is None:
        if media_type == "audiobook":
            categories = list(settings.prowlarr_audiobook_categories)
        elif media_type == "ebook":
            categories = list(settings.prowlarr_book_categories)
        elif book_only:
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
    q: str = Query(default="", max_length=200),
    # Ceiling raised from 25 so an empty `q` can return a whole shelf, not
    # an arbitrary first slice of one.
    limit: int = Query(default=10, ge=1, le=200),
    _actor: str = Depends(_read_actor),
) -> dict[str, Any]:
    """Ebooks on the server. An empty `q` lists them all, author order.

    `list_ebooks` already treats an empty needle as "match everything" —
    this route simply stopped rejecting it, so someone who does not already
    know what is on the shelf can look instead of having to guess a word
    that happens to be in a title.
    """
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
def ebook_download(ebook_id: str, actor: str = Depends(_read_actor)) -> FileResponse:
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
    _actor: str = Depends(_read_actor),
) -> dict[str, Any]:
    client = _qbittorrent_client()
    try:
        client.login()
        return {"downloads": client.torrents(category=category)}
    except ServiceError as exc:
        raise _upstream_error(exc) from exc


@app.post("/api/v1/releases/grab", status_code=status.HTTP_202_ACCEPTED)
def grab_release(request: ReleaseGrabRequest, actor: str = Depends(_action_actor)) -> dict[str, Any]:
    if not settings.prowlarr_url or not settings.prowlarr_api_key:
        raise HTTPException(status_code=503, detail="Prowlarr integration is not configured")
    return _job_response(database.enqueue("grab_release", {"release": request.release}, actor=actor))


@app.post("/api/v1/transfers/pull", status_code=status.HTTP_202_ACCEPTED)
def pull_transfer(request: TransferRequest, actor: str = Depends(_action_actor)) -> dict[str, Any]:
    if not settings.sullivan_host or not settings.sullivan_user:
        raise HTTPException(status_code=503, detail="Sullivan transfer is not configured")
    payload = {"remote_path": request.remote_path}
    if request.local_path:
        payload["local_path"] = request.local_path
    return _job_response(database.enqueue("transfer_completed", payload, actor=actor))


@app.get("/api/v1/jobs")
def list_jobs(
    _actor: str = Depends(_read_actor),
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
def create_job(request: JobRequest, actor: str = Depends(_action_actor)) -> dict[str, Any]:
    return _job_response(database.enqueue(request.kind, request.payload, actor=actor))


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str, _actor: str = Depends(_read_actor)) -> dict[str, Any]:
    job = database.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return _job_response(job)


@app.post("/api/v1/jobs/{job_id}/cancel")
def cancel_job(job_id: str, actor: str = Depends(_action_actor)) -> dict[str, Any]:
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

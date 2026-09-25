"""Discord slash-command adapter for the internal Shelfmark API.

The bot acknowledges every interaction before doing network work.  Search
results are shown ephemerally, and a button creates an asynchronous API job so
the interaction token is never used as a long-running task channel.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable, Collection, Iterable, Sequence
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from .clients import HttpClient, ServiceError
from .config import Settings

DEFAULT_MAX_ATTACHMENT_MB = 10.0


def _int_set(value: str | None) -> set[int]:
    result: set[int] = set()
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError as exc:
            raise ValueError("SHELFMARK_DISCORD_ALLOWED_ROLE_IDS must contain integer IDs") from exc
    return result


def _max_attachment_bytes(value: str | None) -> int:
    """Parse SHELFMARK_DISCORD_MAX_ATTACHMENT_MB, given in MB for a human to set.

    Discord caps attachments at 10 MB on an unboosted server, but a boosted
    one raises that — so this has to be adjustable per-deployment rather than
    a hardcoded 10, and it must fail closed on garbage input rather than
    silently uploading nothing (an unparsable limit) or blocking every send
    (a zero limit taken as-is), which is why the input is clamped rather than
    passed straight through.
    """
    text = (value or "").strip()
    if not text:
        return int(DEFAULT_MAX_ATTACHMENT_MB * 1_000_000)
    try:
        megabytes = float(text)
    except ValueError as exc:
        raise ValueError("SHELFMARK_DISCORD_MAX_ATTACHMENT_MB must be a number") from exc
    return int(max(0.1, megabytes) * 1_000_000)


def _too_large(size: Any, limit: int) -> bool:
    """Whether a file this size must be refused before any network call.

    Split out from the button callback so the decision can be tested without
    standing up a Discord object graph — the same reason `is_permitted` below
    is tested this way rather than through a fake interaction. A search
    result missing a size (not a number) is treated as fine to attempt rather
    than refused, since there's nothing to compare against.
    """
    return isinstance(size, (int, float)) and size > limit


def _needs_confirmation(size: Any, threshold_bytes: int) -> bool:
    """Whether a Grab press must stop for confirmation instead of queuing.

    Deliberately the OPPOSITE of `_too_large`'s call on a missing size.
    `_too_large` treats "no number to compare" as fine to attempt, because
    EbookView re-checks the real fetched byte count immediately afterward --
    a stale or absent search-result size there is caught before anything
    reaches the user. A grab has no such second look: the job is handed to a
    remote worker and its real size is never seen again on this side.
    Treating a missing size as "fine" here would let exactly the release this
    guard exists for -- a mis-ranked, wrongly-categorized result with no
    usable size field -- slip through on the one un-confirmed press this
    feature is meant to stop (a real `/request "the stand"` search returned a
    26 GB "Westerns ... GraphicAudio Collection" ranked above the book
    actually searched for, because it matched on "Stand-Alone" containing
    "stand"). So an unusable size is treated as the risky case, not exempted
    from it.
    """
    if not isinstance(size, (int, float)):
        return True
    return size > threshold_bytes


def _human_size(num_bytes: int) -> str:
    if num_bytes >= 1_000_000:
        return f"{num_bytes / 1_000_000:.1f} MB"
    if num_bytes >= 1_000:
        return f"{num_bytes / 1_000:.1f} KB"
    return f"{num_bytes} B"


def _rate_limit_wait_text(body: str) -> str:
    """Turn a 429's raw response body into a phone-readable wait time.

    `body` is `ServiceError.message` as HttpClient.request builds it: the
    raw, undecoded response text (see clients.py), which for a 429 from
    api._rate_limited_error is a JSON object shaped like
    `{"detail": {"retry_after_seconds": 42, ...}}`. A bare "429" or a dumped
    JSON blob means nothing to someone tapping a Discord button on their
    phone -- this is the one place that gets turned into "try again in N
    minutes". Split out and tested directly (see RateLimitWaitTextTests in
    tests/test_discord_bot.py) rather than folded into an `except
    ServiceError` block, the same reason `_job_status_message` above is its
    own function: it can be checked against exact JSON strings without
    standing up a Discord interaction.

    Falls back to a generic wait message on anything that fails to parse --
    a malformed or unexpected body must not raise a SECOND exception from
    inside code that is already handling one.
    """
    try:
        parsed = json.loads(body)
    except (TypeError, ValueError):
        return "Try again in a minute."
    detail = parsed.get("detail") if isinstance(parsed, dict) else None
    retry_after = detail.get("retry_after_seconds") if isinstance(detail, dict) else None
    if not isinstance(retry_after, (int, float)) or retry_after <= 0:
        return "Try again in a minute."
    retry_after = int(retry_after)
    if retry_after >= 60:
        minutes = max(1, round(retry_after / 60))
        return f"Try again in {minutes} minute{'s' if minutes != 1 else ''}."
    return f"Try again in {retry_after} second{'s' if retry_after != 1 else ''}."


def _rate_limit_message(kind: str, exc: ServiceError) -> str:
    """The full sentence for a 429 from the Shelfmark API -- "you have run
    too many searches, try again in N minutes" rather than a bare status
    code. `kind` names what was being attempted ("searches", "grabs", ...)
    so every command reads like an explanation of what to do next, not an
    error dump. Every `except ServiceError` below checks `exc.status == 429`
    and calls this FIRST, falling back to its existing generic message
    otherwise -- an ordinary upstream outage (Prowlarr down, a timeout)
    still reads exactly as it did before this change.
    """
    return f"You have run too many {kind}. {_rate_limit_wait_text(exc.message)}"


# `/request` and `/library` used to show five results and stop -- a real
# `/request type:audiobook query:"the stand"` search put two *Creativity,
# Inc* results and a 26 GB Westerns collection ahead of the Stephen King
# audiobook actually searched for, which landed at position 3. That time it
# was still inside the top five; nothing stops the same ranking noise from
# landing a real result at position 8 or 15 on a different query, and the
# non-technical primary user has no good way to "guess a narrower query"
# their way to it. The fix is paging through results ALREADY fetched, five
# at a time, rather than re-querying per page turn (a Prowlarr search takes
# seconds; re-running it on every Next press would make paging feel
# broken) or asking for a better query. `_PAGE_SIZE` is 5 because that is
# also Discord's own per-row button cap, which is what makes five the
# natural width for a page of action buttons, not just a display choice.
_PAGE_SIZE = 5

# A page of a view that has NO per-item buttons -- the audiobook listing --
# is bound by how much an embed can readably hold, not by Discord's
# five-buttons-per-row limit. Ten `_library_label` lines is roughly a
# third of the embed description cap, and turns a 190-book library from 38
# page presses into 19.
_LISTING_PAGE_SIZE = 10


def _page_count(total: int, page_size: int = _PAGE_SIZE) -> int:
    """How many pages `total` items make, always at least 1.

    A `total` of 0 still returns 1 rather than 0, so a page number always
    has somewhere valid to land -- callers that reach this with zero items
    are being defensive, since the "no results" message is sent before any
    paged view is ever built.
    """
    if total <= 0:
        return 1
    return -(-total // page_size)  # ceiling division without importing math


def _clamp_page(page: int, total: int, page_size: int = _PAGE_SIZE) -> int:
    """Keep a page number inside [0, last page] rather than erroring.

    Covers a stale Previous/Next press racing a result count that changed
    underneath it, and a target page computed one step past either end.
    """
    last_page = _page_count(total, page_size) - 1
    return max(0, min(page, last_page))


def _page_slice(items: Sequence[Any], page: int, page_size: int = _PAGE_SIZE) -> list[Any]:
    """The items shown on `page` (0-indexed) -- what the embed text and the
    action buttons are both built from for that page."""
    page = _clamp_page(page, len(items), page_size)
    start = page * page_size
    return list(items[start : start + page_size])


def _resolve_page_item(
    items: Sequence[Any], page: int, local_index: int, page_size: int = _PAGE_SIZE
) -> Any | None:
    """The item a page's Nth action button must act on.

    THE bug this whole function exists to prevent: page 2's local button 0
    (labelled "Grab 6") must resolve to `items[5]`, not `items[0]`. A view
    built once from `releases[:5]`, with buttons bound to that fixed slice
    and only relabelled on a page turn, keeps grabbing items 0-4 forever no
    matter which page is showing -- and there is no user-visible sign of
    it: the button reads "Grab 6", a job queues, and the wrong book arrives
    with nothing to explain why. Recomputing `page * page_size +
    local_index` HERE, against `self.page` read fresh at the moment of the
    press rather than an index captured when the button was constructed, is
    what keeps that from happening. Every Grab/Send callback in this module
    goes through this function instead of indexing its stored list itself.
    """
    absolute_index = page * page_size + local_index
    if 0 <= absolute_index < len(items):
        return items[absolute_index]
    return None


def _has_previous_page(page: int) -> bool:
    return page > 0


def _has_next_page(page: int, total: int, page_size: int = _PAGE_SIZE) -> bool:
    return (page + 1) < _page_count(total, page_size)


def _page_position_text(page: int, total: int, page_size: int = _PAGE_SIZE) -> str:
    """'6-10 of 25' -- so paging never happens blind."""
    if total <= 0:
        return "0 of 0"
    page = _clamp_page(page, total, page_size)
    start = page * page_size + 1
    end = min(start + page_size - 1, total)
    return f"{start}-{end} of {total}"


def _numbered_lines(
    items: Sequence[dict[str, Any]], start: int, label_fn: Callable[[dict[str, Any]], str]
) -> str:
    """One numbered line per item, counting up from `start` instead of
    always restarting at 1 -- page 2 must read "6. ...", "7. ...", to agree
    with the "Grab 6"/"Grab 7" buttons beside it (see `_resolve_page_item`).
    Resetting the count on every page would make the embed's numbers and
    the buttons' numbers disagree with each other.
    """
    return "\n".join(f"{start + offset}. {label_fn(item)}" for offset, item in enumerate(items))


def _idle(reasons: Sequence[str]) -> None:
    """Stay up doing nothing, rather than exiting.

    The bot runs under `restart: unless-stopped`, which restarts the container
    on ANY exit — a clean one included. Raising on missing configuration
    therefore produces a container that dies and respawns forever, scrolling
    the one message the operator needed off the top of `docker logs`.

    Idling keeps that message readable until the next deployment supplies the
    value. It matches how the rest of this stack behaves: a service with a
    missing credential should sit there waiting for one, not take the node's
    log budget with it.
    """
    print("Shelfmark Discord bot is NOT running. Reason(s):", flush=True)
    for reason in reasons:
        print(f"  - {reason}", flush=True)
    print(
        "Set the missing value(s) as repository secrets on nuniesmith/freddy and "
        "redeploy; the container is recreated with the new environment.",
        flush=True,
    )
    while True:
        time.sleep(3600)


class ShelfmarkApi:
    def __init__(self, base_url: str, token: str):
        self.token = token
        self.http = HttpClient(base_url, service="shelfmark-api", retries=2, backoff=0.25)

    def request(self, path: str, *, method: str = "GET", params: dict[str, Any] | None = None, json_body: Any = None, actor: str | None = None) -> Any:
        headers = {"Authorization": f"Bearer {self.token}"}
        if actor:
            headers["X-Shelfmark-Actor"] = actor
        return self.http.request(
            path,
            method=method,
            params=params,
            json_body=json_body,
            headers=headers,
        )

    async def get(self, path: str, *, params: dict[str, Any] | None = None, actor: str | None = None) -> Any:
        return await asyncio.to_thread(self.request, path, params=params, actor=actor)

    async def post(self, path: str, *, json_body: Any, actor: str | None = None) -> Any:
        return await asyncio.to_thread(self.request, path, method="POST", json_body=json_body, actor=actor)

    def fetch_ebook(self, ebook_id: str, actor: str) -> tuple[bytes, str]:
        """Fetch raw file bytes for one ebook, bypassing HttpClient.

        HttpClient._decode() always treats a response as UTF-8 text (see
        clients.py), which would corrupt an epub/pdf/mobi's binary content.
        This is the one place the bot needs the actual bytes back, so it
        talks to urllib directly instead.
        """
        url = f"{self.http.base_url}/api/v1/ebooks/{urllib.parse.quote(ebook_id, safe='')}/download"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "X-Shelfmark-Actor": actor,
                "Accept": "application/octet-stream",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.http.timeout) as response:
                data = response.read()
                filename_header = response.headers.get("X-Shelfmark-Filename", "")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            exc.close()
            raise ServiceError("shelfmark-api", detail or exc.reason or f"HTTP {exc.code}", status=exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ServiceError("shelfmark-api", str(exc)) from exc
        filename = urllib.parse.unquote(filename_header) if filename_header else ebook_id
        return data, filename

    def ebook_link(self, ebook_id: str, actor: str) -> dict[str, Any]:
        """A signed, expiring download link for one ebook (see links.py): what
        the bot posts instead of an attachment Discord would refuse."""
        return self.request(
            f"/api/v1/ebooks/{urllib.parse.quote(ebook_id, safe='')}/link",
            method="POST",
            json_body={},
            actor=actor,
        )


ACTIVE_JOB_STATUSES = frozenset({"queued", "running"})


def _cancellable(jobs: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """The jobs `/cancel` may act on, newest first as the API returned them.

    Terminal jobs are filtered out here rather than offered and refused on
    press. `Database.cancel` does NOT reject a finished job — it records the
    request and returns the row untouched — so a Cancel button beside a
    succeeded job would appear to work and change nothing.
    """
    return [
        job
        for job in jobs
        if isinstance(job, dict) and str(job.get("status") or "") in ACTIVE_JOB_STATUSES
    ]


def _job_label(job: dict[str, Any]) -> str:
    kind = str(job.get("kind") or "job")
    status = str(job.get("status") or "unknown")
    job_id = str(job.get("id") or "")
    return f"`{status}` **{kind}** — `{job_id[:8]}`"


def _cancel_outcome_message(payload: dict[str, Any], job_id: str) -> str:
    """What actually happened, which is not always "cancelled".

    Three different outcomes reach this from one 200 response, and saying
    "cancelled" for all of them would be wrong twice:

    * `queued` -> `cancelled` immediately. It never ran.
    * `running` -> only `cancel_requested` is set; the worker stops when it
      next checks. The job is still running at the moment we reply, and
      telling someone it is cancelled invites them to go looking for why the
      download is still moving.
    * already terminal -> nothing changed at all. `Database.cancel` writes
      the audit row and returns the row untouched, so the API still answers
      200 with a succeeded job. `_cancellable` keeps these out of the picker,
      but a hand-typed id can still land here.
    """
    status = str(payload.get("status") or "unknown")
    short = str(payload.get("id") or job_id)
    if status == "cancelled":
        return f"Cancelled **{short}**. It had not started yet."
    if status == "running":
        return (
            f"Asked the worker to stop **{short}**. It is still finishing the "
            "step it is on — check `/job` in a moment."
        )
    if status in {"succeeded", "failed"}:
        return f"**{short}** already {status} — there was nothing to cancel."
    return f"**{short}** is now `{status}`."


def _actor(interaction: discord.Interaction) -> str:
    user_id = getattr(interaction.user, "id", "unknown")
    guild_id = interaction.guild_id or "dm"
    channel_id = interaction.channel_id or "unknown"
    return f"discord:{user_id}:{guild_id}:{channel_id}"


def _result_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    # "jobs" is GET /api/v1/jobs. Its absence here is what made
    # /library type:audiobook answer "nothing found" for every query for
    # weeks: the route returned a shape this function could not see, and
    # each half looked correct on its own. /cancel reads the job list the
    # same way, so it would have reported "nothing is running" forever.
    for key in ("results", "book", "podcast", "items", "downloads", "jobs"):
        value = payload.get(key)
        if isinstance(value, list):
            # Audiobookshelf search wraps each result in a libraryItem object.
            return [
                item.get("libraryItem", item)
                if isinstance(item, dict)
                else {}
                for item in value
            ]
    return []


def _release_label(release: dict[str, Any]) -> str:
    title = str(release.get("title") or release.get("name") or "untitled")
    indexer = str(release.get("indexer") or release.get("indexerName") or "indexer")
    size = release.get("size")
    size_text = f" · {int(size) / 1_000_000:.0f} MB" if isinstance(size, (int, float)) else ""
    return f"{title[:70]} · {indexer[:24]}{size_text}"


def _job_status_message(payload: dict[str, Any], job_id: str) -> str:
    """Render a `GET /api/v1/jobs/{id}` payload for `/job`.

    Split out from the command handler for the same reason `is_permitted` is:
    it can be checked directly, without a Discord interaction object graph, on
    the exact question that motivated this module — that a failed job's code
    (`extraction_failed`, `provider_not_configured`, ...) is now visible next
    to its status, not just the free-text `error` sentence that used to be the
    only signal and changed wording every time someone edited it.

    The code line only appears for `failed`/`cancelled` jobs: a queued or
    running job has none yet, and a succeeded one never will.
    """
    status = payload.get("status", "unknown")
    lines = [
        f"Job **{payload.get('id', job_id)}**: `{status}`",
        f"Attempts: {payload.get('attempts', 0)}",
    ]
    if status in {"failed", "cancelled"}:
        # `code` is None on a job that failed before this column existed
        # (migration 2 backfills NULL, not a guess) — "unknown" says that
        # plainly instead of the line silently vanishing.
        lines.append(f"Code: `{payload.get('code') or 'unknown'}`")
        error = payload.get("error")
        if error:
            lines.append(f"Error: {str(error)[:300]}")
    return "\n".join(lines)


def _ebook_label(item: dict[str, Any]) -> str:
    title = str(item.get("title") or "untitled")
    author = item.get("author")
    author_text = f" — {author}" if author else ""
    size = item.get("size")
    size_text = f" · {_human_size(int(size))}" if isinstance(size, (int, float)) else ""
    return f"{title[:70]}{author_text}{size_text}"


def _library_label(item: dict[str, Any]) -> str:
    media = item.get("media") if isinstance(item.get("media"), dict) else item
    metadata = media.get("metadata") if isinstance(media, dict) and isinstance(media.get("metadata"), dict) else media
    title = str(metadata.get("title") or item.get("title") or "untitled")
    authors = metadata.get("authors") if isinstance(metadata, dict) else None
    if isinstance(authors, list) and authors:
        author = authors[0].get("name") if isinstance(authors[0], dict) else str(authors[0])
    else:
        author = "unknown author"
    return f"{title[:80]} — {str(author)[:50]}"


# `/library`'s two type choices hit two different backends the wife has no
# reason to know about: Audiobookshelf for audiobooks she already owns, the
# on-disk ebooks root (walked by ebooks.py) for ebooks she already owns. This
# mapping is the one fact that split used to leak into two separate commands
# — pulled into a plain function so the type->backend choice is asserted
# directly, without a Discord interaction object graph (the same reason
# is_permitted and _job_status_message are split out above).
# How many results to fetch when BROWSING (no query) rather than
# searching. A browse has nothing to narrow it — the whole point is seeing
# the shelf — so this has to clear the WHOLE library or it truncates it
# with nothing on screen to say so. The first version shipped at 100
# against a library of 190 and showed a little over half of it, which is
# the failure mode a browse exists to remove. 500 is chosen to outlive a
# good deal of growth; both endpoints now cap above it.
_BROWSE_LIMIT = 500


def _library_query(kind: str, query: str) -> tuple[str, dict[str, Any]]:
    """Endpoint and params for `/library`. An empty `query` means BROWSE.

    Browsing is the case this command was missing: `query` used to be
    required, so someone who did not already know what was on the server
    had to guess a word from a title to find out. Both backends treat an
    empty `q` as "everything", so the only difference here is how much to
    ask for.
    """
    limit = _BROWSE_LIMIT if not query else 25
    if kind == "ebook":
        # Raised from 10 to 25 -- api.ebook_search's own ceiling at the
        # time -- once pagination made a result past the old cutoff
        # reachable instead of never being fetched at all.
        return "/api/v1/ebooks/search", {"q": query, "limit": limit}
    # Explicit 25 rather than leaving this on api.library_search's own
    # default: a smaller number would strand a couple of fetched-but-unseen
    # results on a half-full last page instead of giving the same full
    # pages of headroom /library type:ebook now gets.
    return "/api/v1/library/search", {"q": query, "limit": limit}


# `/request`'s two type choices hit the SAME Prowlarr endpoint — unlike
# /library, there is only one backend for "find something new" — but need
# different category filters, which is what `media_type` tells the API route
# to apply (see api.release_search). This used to be two commands
# (/ebook-request and /release-search) that had drifted into calling this
# exact endpoint with this exact book_only flag; the only real difference
# left was cosmetic (embed title, an unused limit), which is why they were
# collapsed into one command with a type choice instead of kept apart.
def _request_query(kind: str, query: str) -> tuple[str, dict[str, Any]]:
    # Raised from 25 to 50: Prowlarr ranks on text match, not on what was
    # actually asked for, and a mis-ranked real result can land past
    # position 25 on a noisy query the same way it landed at position 3 of
    # the 5 that used to be shown for "the stand" (see module docstring).
    # Paging is client-side over ONE search response, so a higher limit is
    # an extra cost paid once at search time, not per page turn -- 50
    # stays well under api.release_search's own `le=200` ceiling while
    # doubling the pageable headroom from 5 pages to 10.
    return "/api/v1/releases/search", {"q": query, "media_type": kind, "limit": 50}


def _grab_outcome_message(payload: dict[str, Any], job_id: str) -> str:
    """What the grab actually did, in a sentence.

    This used to be "Queued release <uuid>" in every case, which is the same
    thing it said when the download started, when qBittorrent silently
    ignored a release already in the client, and when the link was dead.
    A real report of that ("I don't think it's working") was a DUPLICATE:
    the book had been grabbed five days earlier and was already on the
    shelf, and nothing anywhere said so.

    The job id is still offered, but last and only where it is useful --
    a UUID is not an answer to "did that work?".
    """
    state = str(payload.get("state") or "")
    name = str(payload.get("name") or "that release")
    if state == "duplicate":
        return (
            f"**{name}** is already on the server — it was downloaded before, "
            "so there is nothing new to fetch. Try `/library` to read it."
        )
    if state == "rejected":
        return (
            f"qBittorrent would not accept **{name}**. Nothing is downloading. "
            "This usually means the tracker refused the link."
        )
    if state == "bad_link":
        return (
            f"The download link for **{name}** did not return a torrent — it has "
            "probably expired. Run the search again to get a fresh one."
        )
    if state == "link_error":
        status = payload.get("http_status")
        detail = f"HTTP {status}" if status else "an error"
        return (
            f"The indexer returned {detail} fetching **{name}**, so nothing is "
            "downloading. That is the indexer, not the release — try again in a "
            "moment."
        )
    if state == "added":
        return (
            f"Downloading **{name}**. `/downloads` for progress; it will appear "
            "in the library on its own when it lands."
        )
    # state == "unknown", or an older job queued before this existed.
    job = payload.get("id", job_id)
    return (
        f"Queued **{name}**, but nothing new appeared in the download client "
        f"and the reason could not be determined. Check `/downloads`, or "
        f"`/job {job}`."
    )


async def _await_job(
    api: "ShelfmarkApi",
    job_id: str,
    actor: str,
    *,
    attempts: int = 8,
    delay: float = 1.0,
) -> dict[str, Any] | None:
    """Poll a job until it finishes, or give up and return None.

    A grab job completes in about a second, so this almost always returns on
    the first or second look. The cap exists so a stuck worker degrades to
    "still working, check /job" instead of holding the interaction open.

    Deliberately short and few: every poll is a rate-limited read, and the
    point is to answer a person waiting on a button press, not to follow the
    job to the end of the pipeline.
    """
    for attempt in range(attempts):
        if attempt:
            await asyncio.sleep(delay)
        try:
            payload = await api.get(f"/api/v1/jobs/{urllib.parse.quote(job_id, safe='')}", actor=actor)
        except ServiceError:
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("status") in {"succeeded", "failed", "cancelled"}:
            return payload
    return None


async def _queue_grab(api: ShelfmarkApi, release: dict[str, Any], interaction: discord.Interaction) -> None:
    """Queue the grab, wait for it, and say what actually happened.

    Shared by ReleaseView's immediate Grab press and _ConfirmGrabView's
    confirmed one -- the only difference between the two paths is whether a
    size-confirmation round trip happened first.

    It waits because the grab is a JOB: the id comes back before any work is
    done. Replying with that id was the whole defect -- "Queued release
    <uuid>" reads identically whether the book is downloading, was already on
    the shelf, or was refused outright.
    """
    await interaction.response.defer(ephemeral=True, thinking=True)
    actor = _actor(interaction)
    try:
        result = await api.post(
            "/api/v1/releases/grab", json_body={"release": release}, actor=actor
        )
    except ServiceError as exc:
        if exc.status == 429:
            await interaction.followup.send(_rate_limit_message("grabs", exc), ephemeral=True)
            return
        await interaction.followup.send(
            "The Shelfmark API could not queue that release.", ephemeral=True
        )
        return

    job_id = result.get("id", "unknown") if isinstance(result, dict) else "unknown"
    finished = await _await_job(api, job_id, actor)
    if finished is None:
        await interaction.followup.send(
            f"Queued **{job_id}** — still working. `/job {job_id}` for status.",
            ephemeral=True,
        )
        return
    if finished.get("status") != "succeeded":
        await interaction.followup.send(_job_status_message(finished, job_id), ephemeral=True)
        return
    outcome = finished.get("result")
    await interaction.followup.send(
        _grab_outcome_message(outcome if isinstance(outcome, dict) else {}, job_id),
        ephemeral=True,
    )


class _ConfirmGrabView(discord.ui.View):
    """Second confirmation for one release ReleaseView has already flagged.

    Not a parallel implementation of ReleaseView: it holds no release list
    and makes no size decision -- `_needs_confirmation` already made that
    call, in ReleaseView's own callback. This class exists only because the
    size-warning message is a NEW ephemeral reply with its own component row
    (a `discord.ui.View` cannot be reattached to the message that first
    carried it), so a second, single-release view is the minimum needed to
    carry a Confirm/Cancel pair for it.
    """

    def __init__(
        self,
        api: ShelfmarkApi,
        release: dict[str, Any],
        guard: Callable[[discord.Interaction], Awaitable[bool]],
    ) -> None:
        super().__init__(timeout=900)
        self.api = api
        self.release = release
        self.guard = guard

    @discord.ui.button(label="Grab anyway", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.response.is_done():
            return
        # Re-checked HERE, not assumed from the /request press that led to
        # this message: a role can be revoked during the up-to-15-minute
        # window this view stays alive, and THIS button is the action that
        # actually queues a download -- the earlier Grab press only
        # previewed the size. Someone who could not have started a grab must
        # not be able to complete one just because the size warning is still
        # sitting in their DM/channel.
        if not await self.guard(interaction):
            return
        await _queue_grab(self.api, self.release, interaction)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.response.is_done():
            return
        await interaction.response.edit_message(content="Cancelled -- nothing was queued.", view=None)


class _PagedView(discord.ui.View):
    """Previous/Next paging chrome shared by every search-results view.

    Holds ONLY the page arithmetic and message chrome (nav buttons, the
    embed, the on-timeout message) -- a view with nothing to click but
    Previous and Next (`/library type:audiobook`, which offers no action
    button at all: an Audiobookshelf catalog entry isn't a file this server
    can hand over) is a complete, usable instance of this class on its own.
    `ReleaseView` and `EbookView` below subclass it to add their own row of
    action buttons. Kept as one class specifically so the guard re-check and
    the on-timeout message are written ONCE, rather than three times with
    room for one copy to drift from the others.

    Discord allows 5 components per row and 5 rows; an action row of up to
    5 buttons (row 0) plus this class's Previous/Next (row 1) is 2 rows and
    at most 5 components in any one row, comfortably inside both limits.
    """

    # How many items one page holds. A CLASS attribute, not a constructor
    # argument, because it is a property of what the view contains rather
    # than a choice the calling command should be making: this base class
    # is a plain listing with nothing to click but Previous/Next, so it is
    # bound by how much an embed readably holds, while `ReleaseView` and
    # `EbookView` below override it back to `_PAGE_SIZE` because every one
    # of their lines carries a button and Discord allows five per row.
    page_size: int = _LISTING_PAGE_SIZE

    def __init__(
        self,
        items: list[dict[str, Any]],
        title: str,
        label_fn: Callable[[dict[str, Any]], str],
        guard: Callable[[discord.Interaction], Awaitable[bool]],
    ) -> None:
        super().__init__(timeout=900)
        self.items = items
        self.title = title
        self.label_fn = label_fn
        self.guard = guard
        self.page = 0
        # Set by the command handler right after sending -- see
        # `on_timeout` for why the edit it enables can still fail anyway.
        self.message: discord.Message | None = None
        self.previous_button = discord.ui.Button(
            label="Previous",
            style=discord.ButtonStyle.secondary,
            custom_id="shelfmark:page:previous",
            row=1,
        )
        self.previous_button.callback = self._go_previous  # type: ignore[method-assign]
        self.next_button = discord.ui.Button(
            label="Next",
            style=discord.ButtonStyle.secondary,
            custom_id="shelfmark:page:next",
            row=1,
        )
        self.next_button.callback = self._go_next  # type: ignore[method-assign]
        # Added here, in the BASE __init__, not left for a subclass to wire
        # up -- a plain `_PagedView` (the audiobook library listing, which
        # subclasses nothing) must have working Previous/Next on its own.
        # discord.py lays out rows from each button's explicit `row`, not
        # add order, so it does not matter that a subclass's own row-0
        # action buttons are added to `self` after this runs.
        self.add_item(self.previous_button)
        self.add_item(self.next_button)
        self._sync_nav_buttons()

    def _sync_nav_buttons(self) -> None:
        # Disable rather than error: pressing a Previous/Next that
        # shouldn't exist (first/last page) never reaches the callback at
        # all once Discord greys it out.
        self.previous_button.disabled = not _has_previous_page(self.page)
        self.next_button.disabled = not _has_next_page(self.page, len(self.items), self.page_size)

    def _sync_action_buttons(self) -> None:
        """Hook for a subclass's per-item buttons; a plain `_PagedView`
        (the audiobook library listing) has none, so this is a no-op."""

    def render_embed(self) -> discord.Embed:
        page_items = _page_slice(self.items, self.page, self.page_size)
        embed = discord.Embed(title=self.title)
        start = _clamp_page(self.page, len(self.items), self.page_size) * self.page_size + 1
        embed.description = _numbered_lines(page_items, start, self.label_fn) or "(no results)"
        embed.set_footer(text=_page_position_text(self.page, len(self.items), self.page_size))
        return embed

    async def _go_previous(self, interaction: discord.Interaction) -> None:
        await self._turn_page(interaction, self.page - 1)

    async def _go_next(self, interaction: discord.Interaction) -> None:
        await self._turn_page(interaction, self.page + 1)

    async def _turn_page(self, interaction: discord.Interaction, target_page: int) -> None:
        if interaction.response.is_done():
            return
        # Re-checked on EVERY page turn, not assumed from whichever command
        # opened this view. Paging is the first control in this file that
        # invites sitting on a view and actively using it for its whole
        # 900-second lifetime rather than pressing once and being done --
        # someone whose role is revoked partway through a paging session
        # must be stopped on the very next Previous/Next, not just at the
        # /request or /library press that started it. Same reasoning as the
        # re-check in `_ConfirmGrabView.confirm`.
        if not await self.guard(interaction):
            return
        self.page = _clamp_page(target_page, len(self.items), self.page_size)
        self._sync_nav_buttons()
        self._sync_action_buttons()
        await interaction.response.edit_message(embed=self.render_embed(), view=self)

    async def on_timeout(self) -> None:
        """Say something instead of leaving a dead message.

        discord.py stops listening for this view's components after 900s,
        but Discord does not grey out the buttons on its own -- a press
        after that reaches a bot with no handler left registered for it,
        and the client shows a bare "This interaction failed" with nothing
        to explain why. Editing the message to drop the controls and say
        what happened turns a dead end into a comprehensible one.
        """
        if self.message is None:
            return
        try:
            await self.message.edit(
                content="This search has expired. Run the command again to search once more.",
                view=None,
            )
        except discord.HTTPException:
            # This edit rides the same interaction webhook token that is
            # already ~15 minutes old by the time this timeout fires -- if
            # Discord's clock expires that token a beat before this runs,
            # the edit itself 401s. Best-effort: the user still gets
            # nothing, but the bot must not crash over a message that is
            # about to look stale to them either way.
            pass


class ReleaseView(_PagedView):
    """Grab buttons, five per page, over EVERYTHING `/request` fetched --
    not just the first five. See module-level `_resolve_page_item` for the
    bug this is built around: an action button must resolve against the
    CURRENT page, not an index frozen when the view was first built.
    """

    # Overrides the base listing width: each line here carries its own
    # button, and Discord allows five components per row.
    page_size: int = _PAGE_SIZE

    def __init__(
        self,
        api: ShelfmarkApi,
        releases: list[dict[str, Any]],
        actor: str,
        large_release_threshold_bytes: int,
        guard: Callable[[discord.Interaction], Awaitable[bool]],
        title: str,
    ):
        super().__init__(releases, title, _release_label, guard)
        self.api = api
        self.releases = releases
        self.actor = actor
        self.large_release_threshold_bytes = large_release_threshold_bytes
        self._action_buttons: list[discord.ui.Button] = []
        for local_index in range(self.page_size):
            button = discord.ui.Button(
                style=discord.ButtonStyle.primary,
                custom_id=f"shelfmark:grab:{local_index}",
                row=0,
            )
            button.callback = self._make_callback(local_index)  # type: ignore[method-assign]
            self._action_buttons.append(button)
            self.add_item(button)
        self._sync_action_buttons()

    def _sync_action_buttons(self) -> None:
        for local_index, button in enumerate(self._action_buttons):
            release = _resolve_page_item(self.releases, self.page, local_index)
            if release is None:
                # A partial last page (e.g. 23 results -> the 5th page has
                # 3, not 5) -- disable rather than leave a button that
                # would resolve to nothing.
                button.label = "—"
                button.disabled = True
            else:
                absolute_number = self.page * self.page_size + local_index + 1
                button.label = f"Grab {absolute_number}"
                button.disabled = False

    def _make_callback(self, local_index: int):
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.response.is_done():
                return
            release = _resolve_page_item(self.releases, self.page, local_index)
            if release is None:
                # Unreachable via a normal press -- the slot's button is
                # disabled whenever this would be true (`_sync_action_
                # buttons`) -- but fail loudly rather than let a stale
                # client-side button state reach `_queue_grab` with
                # nothing to grab.
                await interaction.response.send_message(
                    "That slot is empty on this page.", ephemeral=True
                )
                return
            size = release.get("size")
            if _needs_confirmation(size, self.large_release_threshold_bytes):
                # Stop here instead of queuing: a large or unusably-sized
                # release gets one extra, explicit press rather than being
                # pulled by the same accidental tap that would have grabbed
                # a normal audiobook.
                title = str(release.get("title") or release.get("name") or "That release")
                size_text = (
                    _human_size(int(size)) if isinstance(size, (int, float)) else "an unknown size"
                )
                await interaction.response.send_message(
                    f"**{title[:100]}** is {size_text} -- at or above the "
                    f"{_human_size(self.large_release_threshold_bytes)} confirmation "
                    "threshold. A mis-ranked search result can be many times the size "
                    "of what was actually searched for. Confirm to grab it anyway.",
                    view=_ConfirmGrabView(self.api, release, self.guard),
                    ephemeral=True,
                )
                return
            await _queue_grab(self.api, release, interaction)

        return callback


class EbookView(_PagedView):
    """Buttons that fetch an on-server ebook and attach it to the reply,
    five per page over EVERYTHING `/library type:ebook` fetched.

    Follows ReleaseView's defer/act/followup shape, but the action is a
    binary file fetch rather than a job enqueue. The size guard below runs
    BEFORE that fetch: letting a too-large file reach `interaction.followup
    .send(file=...)` means discord.py raises its own HTTPException there,
    whose message ("Payload Too Large") means nothing to someone reading it
    on a phone and does not say what to do about it.
    """

    # Overrides the base listing width: each line here carries its own
    # button, and Discord allows five components per row.
    page_size: int = _PAGE_SIZE

    def __init__(
        self,
        api: ShelfmarkApi,
        books: list[dict[str, Any]],
        actor: str,
        max_attachment_bytes: int,
        guard: Callable[[discord.Interaction], Awaitable[bool]],
        title: str,
    ):
        super().__init__(books, title, _ebook_label, guard)
        self.api = api
        self.books = books
        self.actor = actor
        self.max_attachment_bytes = max_attachment_bytes
        self._action_buttons: list[discord.ui.Button] = []
        for local_index in range(self.page_size):
            button = discord.ui.Button(
                style=discord.ButtonStyle.primary,
                custom_id=f"shelfmark:ebook-send:{local_index}",
                row=0,
            )
            button.callback = self._make_callback(local_index)  # type: ignore[method-assign]
            self._action_buttons.append(button)
            self.add_item(button)
        self._sync_action_buttons()

    def _sync_action_buttons(self) -> None:
        for local_index, button in enumerate(self._action_buttons):
            book = _resolve_page_item(self.books, self.page, local_index)
            if book is None:
                button.label = "—"
                button.disabled = True
            else:
                absolute_number = self.page * self.page_size + local_index + 1
                button.label = f"Send {absolute_number}"
                button.disabled = False

    def _make_callback(self, local_index: int):
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.response.is_done():
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            book = _resolve_page_item(self.books, self.page, local_index)
            if book is None:
                # Unreachable via a normal press -- see ReleaseView's
                # identical guard above.
                await interaction.followup.send(
                    "That slot is empty on this page.", ephemeral=True
                )
                return
            title = str(book.get("title") or "That book")
            size = book.get("size")
            book_id = str(book.get("id") or "")
            if _too_large(size, self.max_attachment_bytes):
                await self._send_link(interaction, book_id, title, int(size))
                return
            try:
                data, filename = await asyncio.to_thread(self.api.fetch_ebook, book_id, self.actor)
            except ServiceError as exc:
                if exc.status == 429:
                    await interaction.followup.send(
                        _rate_limit_message("ebook downloads", exc), ephemeral=True
                    )
                    return
                await interaction.followup.send(
                    "That book could not be fetched from the server.", ephemeral=True
                )
                return
            if _too_large(len(data), self.max_attachment_bytes):
                # The search result's size can be stale by the time this
                # button is pressed (someone re-downloaded a different
                # format in between) — trust the bytes actually read over
                # the number quoted in the earlier search response.
                await self._send_link(interaction, book_id, filename, len(data))
                return
            await interaction.followup.send(
                file=discord.File(io.BytesIO(data), filename=filename),
                ephemeral=True,
            )

        return callback

    async def _send_link(
        self, interaction: discord.Interaction, book_id: str, title: str, size: int
    ) -> None:
        """For a book too big to attach: a download link instead of a refusal.

        Discord caps attachments at 10 MB on an unboosted server, and a real
        epub or PDF can be well over that; before links, those books simply
        could not be had through the bot at all. The link is posted
        ephemerally because it is the credential -- whoever holds it can fetch
        the book until it expires. When the API cannot make one (links not set
        up there, or an error), the reply still says why nothing arrived.
        """
        too_big = (
            f"**{title}** is {_human_size(size)}, over this server's "
            f"{_human_size(self.max_attachment_bytes)} attachment limit"
        )
        try:
            link = await asyncio.to_thread(self.api.ebook_link, book_id, self.actor)
        except ServiceError as exc:
            if exc.status == 429:
                await interaction.followup.send(
                    _rate_limit_message("ebook downloads", exc), ephemeral=True
                )
                return
            await interaction.followup.send(
                f"{too_big}. It can't be sent through Discord this way.", ephemeral=True
            )
            return
        lines = [f"{too_big}, so here is a download link instead:", str(link["url"])]
        expires_at = link.get("expires_at")
        if isinstance(expires_at, int):
            # Discord renders <t:...:R> as "in 24 hours" in the reader's locale.
            lines.append(f"The link stops working <t:{expires_at}:R>.")
        if link.get("note"):
            lines.append(str(link["note"]))
        await interaction.followup.send("\n".join(lines), ephemeral=True)


class CancelView(_PagedView):
    """Cancel buttons over the jobs that can still be cancelled.

    Exists because the alternative is typing a UUID. The job id only ever
    appears in an EPHEMERAL reply, which the person who needs it has very
    likely already dismissed — and the case this command is for is a mis-
    pressed Grab on a 26 GB release, where the useful window is seconds.

    Same shape as ReleaseView/EbookView: five per page, resolved against the
    CURRENT page so a Next press cannot leave a button pointing at the row it
    used to sit beside.
    """

    page_size: int = _PAGE_SIZE

    def __init__(
        self,
        api: ShelfmarkApi,
        jobs: list[dict[str, Any]],
        actor: str,
        guard: Callable[[discord.Interaction], Awaitable[bool]],
        title: str,
    ):
        super().__init__(jobs, title, _job_label, guard)
        self.api = api
        self.jobs = jobs
        self.actor = actor
        self._action_buttons: list[discord.ui.Button] = []
        for local_index in range(self.page_size):
            button = discord.ui.Button(
                style=discord.ButtonStyle.danger,
                custom_id=f"shelfmark:job-cancel:{local_index}",
                row=0,
            )
            button.callback = self._make_callback(local_index)  # type: ignore[method-assign]
            self._action_buttons.append(button)
            self.add_item(button)
        self._sync_action_buttons()

    def _sync_action_buttons(self) -> None:
        for local_index, button in enumerate(self._action_buttons):
            job = _resolve_page_item(self.jobs, self.page, local_index)
            if job is None:
                button.label = "—"
                button.disabled = True
            else:
                absolute_number = self.page * self.page_size + local_index + 1
                button.label = f"Cancel {absolute_number}"
                button.disabled = False

    def _make_callback(self, local_index: int):
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.response.is_done():
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            job = _resolve_page_item(self.jobs, self.page, local_index)
            if job is None:
                await interaction.followup.send(
                    "That slot is empty on this page.", ephemeral=True
                )
                return
            job_id = str(job.get("id") or "")
            try:
                payload = await self.api.post(
                    f"/api/v1/jobs/{urllib.parse.quote(job_id, safe='')}/cancel",
                    json_body={},
                    actor=self.actor,
                )
            except ServiceError as exc:
                if exc.status == 429:
                    await interaction.followup.send(
                        _rate_limit_message("cancellations", exc), ephemeral=True
                    )
                    return
                if exc.status == 404:
                    # Retention can remove a finished job between the list and
                    # the press; that is not an error worth a stack trace.
                    await interaction.followup.send(
                        f"**{job_id[:8]}** is no longer on the server.", ephemeral=True
                    )
                    return
                await interaction.followup.send(
                    "That job could not be cancelled.", ephemeral=True
                )
                return
            await interaction.followup.send(
                _cancel_outcome_message(payload if isinstance(payload, dict) else {}, job_id),
                ephemeral=True,
            )

        return callback


def is_permitted(member_role_ids: Iterable[int], allowed_roles: Collection[int]) -> bool:
    """Decide access from role IDs alone.

    Fails CLOSED on an empty allow-list. It used to return True, so a bot
    deployed before its role IDs were configured let every member of the server
    run every command — including `/organize-preview`, which takes an arbitrary
    absolute path and reports what is at it.

    The dangerous part was that nothing looked wrong. The bot answered, the
    commands worked, and the access control appeared to be in force precisely
    because it was sitting there in the config waiting to be filled in.

    Split out from the interaction handler so the decision can be tested
    without standing up a Discord object graph — the reason the original was
    never covered.
    """
    if not allowed_roles:
        return False
    return bool(set(member_role_ids) & set(allowed_roles))


def install_commands(
    bot: commands.Bot,
    api: ShelfmarkApi,
    allowed_roles: set[int],
    max_attachment_bytes: int = int(DEFAULT_MAX_ATTACHMENT_MB * 1_000_000),
    large_release_threshold_bytes: int = int(Settings().discord_large_release_threshold_mb * 1_000_000),
) -> None:
    def permitted(interaction: discord.Interaction) -> bool:
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        if member is None:
            return False  # a DM has no roles, so it can never be allowed
        return is_permitted((role.id for role in member.roles), allowed_roles)

    async def guard(interaction: discord.Interaction) -> bool:
        if permitted(interaction):
            return True
        # Say which of the two it is. "You are not allowed" sent to the server
        # owner, on a bot with no roles configured, is a confusing half-truth.
        if not allowed_roles:
            message = (
                "Shelfmark has no allowed roles configured, so every command is "
                "refused. Set `SHELFMARK_DISCORD_ALLOWED_ROLE_IDS` and redeploy."
            )
        else:
            message = "You are not allowed to use Shelfmark."
        await interaction.response.send_message(message, ephemeral=True)
        return False

    # Both commands below take a TYPE CHOICE, not free text, so Discord
    # renders a picker (Audiobook / Ebook) instead of asking a non-technical
    # user to type tracker jargon like "release" or know that audiobooks and
    # ebooks live in different places on the server.
    #
    # This replaces four commands that used to exist:
    #   /library-search  -> /library type:audiobook  (Audiobookshelf, unchanged)
    #   /ebook-search    -> /library type:ebook       (on-disk root, unchanged)
    #   /ebook-request   -> /request type:ebook       (Prowlarr, unchanged)
    #   /release-search  -> /request type:audiobook or type:ebook
    # /release-search and /ebook-request had drifted into being the exact
    # same call (same endpoint, same book_only=true, same ReleaseView) with
    # only a cosmetic difference left, which is what made merging them safe.
    #
    # Retired outright rather than kept as aliases: there are exactly two
    # users of this bot, one of whom is the operator, and `SHELFMARK_DISCORD_
    # GUILD_ID` makes the new commands appear the instant this syncs (no
    # week-long global-propagation gap to bridge with a fallback). A thin
    # alias here would be permanent maintenance load — two more commands to
    # keep in sync with every future change to /library and /request — for a
    # transition that a single Discord message ("it's /library and /request
    # now") covers just as well.
    _TYPE_CHOICES = [
        app_commands.Choice(name="Audiobook", value="audiobook"),
        app_commands.Choice(name="Ebook", value="ebook"),
    ]

    @bot.tree.command(name="library", description="Browse or search books already on the server")
    @app_commands.describe(
        type="Audiobook or ebook",
        query="Title, author, or series — leave empty to list everything",
    )
    @app_commands.choices(type=_TYPE_CHOICES)
    async def library(
        interaction: discord.Interaction, type: app_commands.Choice[str], query: str = ""
    ) -> None:
        # `query` is optional so the shelf can be BROWSED. It used to be
        # required, which quietly assumed the person already knew what was
        # on the server -- the opposite of true for the reader this command
        # exists for, who wants to see what there is and pick one.
        if not await guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        kind = type.value
        query = query.strip()
        endpoint, params = _library_query(kind, query)
        try:
            payload = await api.get(endpoint, params=params, actor=_actor(interaction))
        except ServiceError as exc:
            if exc.status == 429:
                await interaction.followup.send(_rate_limit_message("searches", exc), ephemeral=True)
                return
            # Audiobookshelf and the on-disk ebook walk are two independent
            # failure surfaces (a remote API vs. a local directory read) —
            # naming which one is down saves a round trip of "which command
            # did you mean" before anyone can even start diagnosing it.
            service = "Ebook search" if kind == "ebook" else "Audiobookshelf search"
            await interaction.followup.send(f"{service} is unavailable.", ephemeral=True)
            return
        # The full fetch, not a client-side [:5]/[:10] slice -- these are
        # now paged five at a time rather than truncated, so everything
        # `_library_query` asked the backend for stays reachable.
        results = _result_list(payload)
        if not results:
            if not query:
                # Not "nothing matched" — nothing was asked for. An empty
                # shelf and a failed search need different sentences or the
                # reader goes looking for a better search term that does
                # not exist.
                message = (
                    "There are no ebooks on the server yet."
                    if kind == "ebook"
                    else "There are no audiobooks in the library yet."
                )
            else:
                message = (
                    "No ebooks on the server matched that search."
                    if kind == "ebook"
                    else "No matching library items found."
                )
            await interaction.followup.send(message, ephemeral=True)
            return
        title = (
            f"{type.name}s on the server" if not query
            else f"{type.name} library results for {query}"
        )
        if kind == "ebook":
            # Only ebooks get the Send-to-phone button: an audiobook result
            # is an Audiobookshelf catalog entry, not a file this server can
            # hand over as a Discord attachment.
            view: _PagedView = EbookView(
                api, results, _actor(interaction), max_attachment_bytes, guard, title
            )
        else:
            # No per-item buttons here, so nothing binds this to Discord's
            # five-per-row cap — and browsing 190 audiobooks five at a time
            # would be 38 presses.
            view = _PagedView(results, title, _library_label, guard)
        sent = await interaction.followup.send(embed=view.render_embed(), view=view, ephemeral=True)
        view.message = sent

    @bot.tree.command(name="request", description="Search Prowlarr for a new audiobook or ebook to download")
    @app_commands.describe(type="Audiobook or ebook", query="Title, author, ISBN, or other search text")
    @app_commands.choices(type=_TYPE_CHOICES)
    async def request(interaction: discord.Interaction, type: app_commands.Choice[str], query: str) -> None:
        if not await guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        kind = type.value
        endpoint, params = _request_query(kind, query)
        try:
            payload = await api.get(endpoint, params=params, actor=_actor(interaction))
            # The full fetch (up to `_request_query`'s limit=50), not a
            # client-side [:5] slice -- these are now paged five at a time.
            results = _result_list(payload)
            if not results:
                await interaction.followup.send(
                    f"No matching {kind}s were found to download.", ephemeral=True
                )
                return
            title = f"{type.name} downloads for {query}"
            view = ReleaseView(
                api, results, _actor(interaction), large_release_threshold_bytes, guard, title
            )
            sent = await interaction.followup.send(
                embed=view.render_embed(), view=view, ephemeral=True
            )
            view.message = sent
        except ServiceError as exc:
            if exc.status == 429:
                await interaction.followup.send(_rate_limit_message("searches", exc), ephemeral=True)
                return
            await interaction.followup.send("Prowlarr search is unavailable.", ephemeral=True)

    @bot.tree.command(name="downloads", description="Show Shelfmark downloads in qBittorrent")
    async def downloads(interaction: discord.Interaction) -> None:
        if not await guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            payload = await api.get("/api/v1/downloads", actor=_actor(interaction))
            results = _result_list(payload)[:10]
            if not results:
                await interaction.followup.send("No Shelfmark downloads are active.", ephemeral=True)
                return
            lines = []
            for item in results:
                name = str(item.get("name") or item.get("hash") or "unknown")
                progress = item.get("progress")
                state = str(item.get("state") or "unknown")
                progress_text = f"{float(progress) * 100:.1f}%" if isinstance(progress, (int, float)) else "?"
                lines.append(f"• **{name[:80]}** — {progress_text} — `{state}`")
            await interaction.followup.send("\n".join(lines), ephemeral=True)
        except ServiceError as exc:
            if exc.status == 429:
                await interaction.followup.send(_rate_limit_message("download checks", exc), ephemeral=True)
                return
            await interaction.followup.send("qBittorrent download status is unavailable.", ephemeral=True)

    @bot.tree.command(name="job", description="Show recent Shelfmark jobs, or one by ID")
    @app_commands.describe(job_id="Job ID — leave empty to list what has run recently")
    async def job_status(interaction: discord.Interaction, job_id: str = "") -> None:
        # Optional for the same reason /cancel's is: the id only ever appears
        # in an ephemeral reply, so requiring it meant the command could only
        # be used by someone who still had that message open.
        if not await guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        actor = _actor(interaction)
        job_id = job_id.strip()

        if job_id:
            try:
                payload = await api.get(
                    f"/api/v1/jobs/{urllib.parse.quote(job_id, safe='')}", actor=actor
                )
            except ServiceError as exc:
                if exc.status == 429:
                    await interaction.followup.send(
                        _rate_limit_message("job checks", exc), ephemeral=True
                    )
                    return
                if exc.status == 404:
                    await interaction.followup.send(
                        f"No job **{job_id}** on the server.", ephemeral=True
                    )
                    return
                await interaction.followup.send("That job could not be loaded.", ephemeral=True)
                return
            await interaction.followup.send(_job_status_message(payload, job_id), ephemeral=True)
            return

        try:
            payload = await api.get("/api/v1/jobs", params={"limit": 50}, actor=actor)
        except ServiceError as exc:
            if exc.status == 429:
                await interaction.followup.send(
                    _rate_limit_message("job checks", exc), ephemeral=True
                )
                return
            await interaction.followup.send("The job list is unavailable.", ephemeral=True)
            return

        jobs = _result_list(payload)
        if not jobs:
            await interaction.followup.send("No jobs have run recently.", ephemeral=True)
            return
        view = _PagedView(jobs, "Recent Shelfmark jobs", _job_label, guard)
        sent = await interaction.followup.send(
            embed=view.render_embed(), view=view, ephemeral=True
        )
        view.message = sent

    @bot.tree.command(name="cancel", description="Stop a Shelfmark job that is queued or running")
    @app_commands.describe(job_id="Job ID — leave empty to pick from what is running")
    async def cancel(interaction: discord.Interaction, job_id: str = "") -> None:
        # `job_id` is optional for the same reason `/library`'s query is: the
        # id only ever appeared in an ephemeral reply, and the case this
        # command exists for is a mis-pressed Grab on a very large release,
        # where the useful window is seconds rather than however long it
        # takes to find a UUID.
        if not await guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        actor = _actor(interaction)
        job_id = job_id.strip()

        if job_id:
            try:
                payload = await api.post(
                    f"/api/v1/jobs/{urllib.parse.quote(job_id, safe='')}/cancel",
                    json_body={},
                    actor=actor,
                )
            except ServiceError as exc:
                if exc.status == 429:
                    await interaction.followup.send(
                        _rate_limit_message("cancellations", exc), ephemeral=True
                    )
                    return
                if exc.status == 404:
                    await interaction.followup.send(
                        f"No job **{job_id}** on the server.", ephemeral=True
                    )
                    return
                await interaction.followup.send(
                    "That job could not be cancelled.", ephemeral=True
                )
                return
            await interaction.followup.send(
                _cancel_outcome_message(payload if isinstance(payload, dict) else {}, job_id),
                ephemeral=True,
            )
            return

        try:
            payload = await api.get(
                "/api/v1/jobs", params={"limit": 50}, actor=actor
            )
        except ServiceError as exc:
            if exc.status == 429:
                await interaction.followup.send(
                    _rate_limit_message("job checks", exc), ephemeral=True
                )
                return
            await interaction.followup.send("The job list is unavailable.", ephemeral=True)
            return

        jobs = _cancellable(_result_list(payload))
        if not jobs:
            await interaction.followup.send(
                "Nothing is queued or running right now.", ephemeral=True
            )
            return
        view = CancelView(api, jobs, actor, guard, "Jobs you can cancel")
        sent = await interaction.followup.send(
            embed=view.render_embed(), view=view, ephemeral=True
        )
        view.message = sent

    @bot.tree.command(name="scan", description="Ask Audiobookshelf to re-scan the library")
    @app_commands.describe(force="Re-read every book instead of just what changed")
    async def scan(interaction: discord.Interaction, force: bool = False) -> None:
        # No library argument. There is one Audiobookshelf library here and
        # the API already knows its id, so asking a human to paste a UUID to
        # name the only possible target was friction with nothing behind it.
        if not await guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await api.post(
                "/api/v1/libraries/scan",
                json_body={"force": force},
                actor=_actor(interaction),
            )
            await interaction.followup.send(
                f"Queued library scan job **{result.get('id', 'unknown')}**.",
                ephemeral=True,
            )
        except ServiceError as exc:
            if exc.status == 429:
                await interaction.followup.send(_rate_limit_message("library scans", exc), ephemeral=True)
                return
            await interaction.followup.send("The library scan job could not be queued.", ephemeral=True)

def blocking_problems() -> list[str]:
    """Configuration without which the bot cannot connect at all.

    Deliberately NOT including the allow-list: a bot with no roles configured
    can still connect and answer, refusing each command with a message that
    says why. That is far easier to diagnose from Discord than a container
    that never appears.
    """
    problems: list[str] = []
    if not (os.environ.get("DISCORD_BOT_TOKEN") or "").strip():
        problems.append("DISCORD_BOT_TOKEN is not set")
    if not (os.environ.get("SHELFMARK_API_TOKEN") or "").strip():
        problems.append("SHELFMARK_API_TOKEN is not set (the bot calls the API with it)")
    return problems


def build_bot() -> commands.Bot:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise ValueError("DISCORD_BOT_TOKEN is required")
    api_url = os.environ.get("SHELFMARK_API_URL", "http://127.0.0.1:8000")
    api_token = os.environ.get("SHELFMARK_API_TOKEN")
    if not api_token:
        raise ValueError("SHELFMARK_API_TOKEN is required for the bot")
    guild_id_text = (os.environ.get("SHELFMARK_DISCORD_GUILD_ID") or "").strip()
    try:
        guild_id = int(guild_id_text) if guild_id_text else None
    except ValueError:
        # Not worth refusing to start over. Commands sync globally instead,
        # which works — it is just slower to appear.
        print(
            f"SHELFMARK_DISCORD_GUILD_ID is not a number ({guild_id_text!r}); "
            "syncing commands globally instead, which can take up to an hour "
            "to appear.",
            flush=True,
        )
        guild_id = None
    intents = discord.Intents.none()
    intents.guilds = True

    class ShelfmarkBot(commands.Bot):
        async def setup_hook(self) -> None:
            if guild_id:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                await self.tree.sync(guild=guild)
            else:
                await self.tree.sync()

        async def on_ready(self) -> None:
            if self.user:
                print(f"Shelfmark bot connected as {self.user} (guild={guild_id or 'global'})")

    try:
        allowed_roles = _int_set(os.environ.get("SHELFMARK_DISCORD_ALLOWED_ROLE_IDS"))
    except ValueError as exc:
        # An unparseable allow-list must not become an ABSENT allow-list, and
        # must not crash-loop either. Empty is now fail-closed, so refusing
        # everything is the safe reading of "I could not tell who is allowed".
        print(f"{exc}. Refusing every command until it is corrected.", flush=True)
        allowed_roles = set()
    if not allowed_roles:
        print(
            "SHELFMARK_DISCORD_ALLOWED_ROLE_IDS is empty — every command will be "
            "refused. Set it to the ID of the role permitted to use Shelfmark.",
            flush=True,
        )
    try:
        max_attachment_bytes = _max_attachment_bytes(
            os.environ.get("SHELFMARK_DISCORD_MAX_ATTACHMENT_MB")
        )
    except ValueError as exc:
        # Same reasoning as the allow-list above: garbage input must not
        # silently become "no limit" — fall back to Discord's own default
        # rather than trust a value that failed to parse.
        print(f"{exc}. Using the {DEFAULT_MAX_ATTACHMENT_MB:g} MB default.", flush=True)
        max_attachment_bytes = _max_attachment_bytes(None)
    try:
        settings = Settings.from_env()
    except ValueError as exc:
        # Same reasoning again: a bad value in ANY Settings field (this bot
        # only reads discord_large_release_threshold_mb from it) must not
        # crash-loop the container -- fall back to Settings' own defaults.
        print(f"{exc}. Using the default Shelfmark settings.", flush=True)
        settings = Settings()
    large_release_threshold_bytes = int(settings.discord_large_release_threshold_mb * 1_000_000)
    bot = ShelfmarkBot(command_prefix=commands.when_mentioned, intents=intents)
    install_commands(
        bot,
        ShelfmarkApi(api_url, api_token),
        allowed_roles,
        max_attachment_bytes,
        large_release_threshold_bytes,
    )
    return bot


def main() -> None:
    problems = blocking_problems()
    if problems:
        _idle(problems)
        return
    token = os.environ["DISCORD_BOT_TOKEN"]
    try:
        build_bot().run(token)
    except discord.LoginFailure:
        # A WRONG token, as distinct from a missing one. Retrying cannot fix
        # it, and under `restart: unless-stopped` an exit here would retry it
        # forever — against Discord's login endpoint, which is a good way to
        # get the application rate-limited.
        _idle(["DISCORD_BOT_TOKEN was rejected by Discord — the token is wrong or was reset"])


if __name__ == "__main__":
    main()

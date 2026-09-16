"""Discord slash-command adapter for the internal Shelfmark API.

The bot acknowledges every interaction before doing network work.  Search
results are shown ephemerally, and a button creates an asynchronous API job so
the interaction token is never used as a long-running task channel.
"""

from __future__ import annotations

import asyncio
import io
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
    for key in ("results", "book", "podcast", "items", "downloads"):
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
def _library_query(kind: str, query: str) -> tuple[str, dict[str, Any]]:
    if kind == "ebook":
        return "/api/v1/ebooks/search", {"q": query, "limit": 10}
    return "/api/v1/library/search", {"q": query}


# `/request`'s two type choices hit the SAME Prowlarr endpoint — unlike
# /library, there is only one backend for "find something new" — but need
# different category filters, which is what `media_type` tells the API route
# to apply (see api.release_search). This used to be two commands
# (/ebook-request and /release-search) that had drifted into calling this
# exact endpoint with this exact book_only flag; the only real difference
# left was cosmetic (embed title, an unused limit), which is why they were
# collapsed into one command with a type choice instead of kept apart.
def _request_query(kind: str, query: str) -> tuple[str, dict[str, Any]]:
    return "/api/v1/releases/search", {"q": query, "media_type": kind, "limit": 25}


async def _queue_grab(api: ShelfmarkApi, release: dict[str, Any], interaction: discord.Interaction) -> None:
    """POST the grab and report the queued job id.

    Shared by ReleaseView's immediate Grab press and _ConfirmGrabView's
    confirmed one -- the only difference between the two paths is whether a
    size-confirmation round trip happened first. The payload and endpoint are
    identical either way.
    """
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        result = await api.post(
            "/api/v1/releases/grab",
            json_body={"release": release},
            actor=_actor(interaction),
        )
        job_id = result.get("id", "unknown") if isinstance(result, dict) else "unknown"
        await interaction.followup.send(
            f"Queued release **{job_id}**. Use `/job {job_id}` for status.",
            ephemeral=True,
        )
    except ServiceError:
        await interaction.followup.send("The Shelfmark API could not queue that release.", ephemeral=True)


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


class ReleaseView(discord.ui.View):
    def __init__(
        self,
        api: ShelfmarkApi,
        releases: list[dict[str, Any]],
        actor: str,
        large_release_threshold_bytes: int,
        guard: Callable[[discord.Interaction], Awaitable[bool]],
    ):
        super().__init__(timeout=900)
        self.api = api
        self.releases = releases
        self.actor = actor
        self.large_release_threshold_bytes = large_release_threshold_bytes
        self.guard = guard
        for index, release in enumerate(releases[:5]):
            button = discord.ui.Button(
                label=f"Grab {index + 1}",
                style=discord.ButtonStyle.primary,
                custom_id=f"shelfmark:grab:{index}",
            )
            button.callback = self._callback(index)  # type: ignore[method-assign]
            self.add_item(button)

    def _callback(self, index: int):
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.response.is_done():
                return
            release = self.releases[index]
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


class EbookView(discord.ui.View):
    """Buttons that fetch an on-server ebook and attach it to the reply.

    Follows ReleaseView's defer/act/followup shape, but the action is a
    binary file fetch rather than a job enqueue. The size guard below runs
    BEFORE that fetch: letting a too-large file reach `interaction.followup
    .send(file=...)` means discord.py raises its own HTTPException there,
    whose message ("Payload Too Large") means nothing to someone reading it
    on a phone and does not say what to do about it.
    """

    def __init__(
        self,
        api: ShelfmarkApi,
        books: list[dict[str, Any]],
        actor: str,
        max_attachment_bytes: int,
    ):
        super().__init__(timeout=900)
        self.api = api
        self.books = books
        self.actor = actor
        self.max_attachment_bytes = max_attachment_bytes
        for index, book in enumerate(books[:5]):
            button = discord.ui.Button(
                label=f"Send {index + 1}",
                style=discord.ButtonStyle.primary,
                custom_id=f"shelfmark:ebook-send:{index}",
            )
            button.callback = self._callback(index)  # type: ignore[method-assign]
            self.add_item(button)

    def _callback(self, index: int):
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.response.is_done():
                return
            await interaction.response.defer(ephemeral=True, thinking=True)
            book = self.books[index]
            title = str(book.get("title") or "That book")
            size = book.get("size")
            if _too_large(size, self.max_attachment_bytes):
                await interaction.followup.send(
                    f"**{title}** is {_human_size(int(size))}, over this server's "
                    f"{_human_size(self.max_attachment_bytes)} attachment limit. "
                    "It can't be sent through Discord this way.",
                    ephemeral=True,
                )
                return
            book_id = str(book.get("id") or "")
            try:
                data, filename = await asyncio.to_thread(self.api.fetch_ebook, book_id, self.actor)
            except ServiceError:
                await interaction.followup.send(
                    "That book could not be fetched from the server.", ephemeral=True
                )
                return
            if _too_large(len(data), self.max_attachment_bytes):
                # The search result's size can be stale by the time this
                # button is pressed (someone re-downloaded a different
                # format in between) — trust the bytes actually read over
                # the number quoted in the earlier search response.
                await interaction.followup.send(
                    f"**{filename}** turned out to be {_human_size(len(data))}, over the "
                    f"{_human_size(self.max_attachment_bytes)} limit. It can't be sent this way.",
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                file=discord.File(io.BytesIO(data), filename=filename),
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

    @bot.tree.command(name="library", description="Search books already on the server")
    @app_commands.describe(type="Audiobook or ebook", query="Title, author, or series to search for")
    @app_commands.choices(type=_TYPE_CHOICES)
    async def library(interaction: discord.Interaction, type: app_commands.Choice[str], query: str) -> None:
        if not await guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        kind = type.value
        endpoint, params = _library_query(kind, query)
        try:
            payload = await api.get(endpoint, params=params, actor=_actor(interaction))
        except ServiceError:
            # Audiobookshelf and the on-disk ebook walk are two independent
            # failure surfaces (a remote API vs. a local directory read) —
            # naming which one is down saves a round trip of "which command
            # did you mean" before anyone can even start diagnosing it.
            service = "Ebook search" if kind == "ebook" else "Audiobookshelf search"
            await interaction.followup.send(f"{service} is unavailable.", ephemeral=True)
            return
        results = _result_list(payload)[: 5 if kind == "ebook" else 10]
        if not results:
            message = (
                "No ebooks on the server matched that search."
                if kind == "ebook"
                else "No matching library items found."
            )
            await interaction.followup.send(message, ephemeral=True)
            return
        label = _ebook_label if kind == "ebook" else _library_label
        embed = discord.Embed(title=f"{type.name} library results for {query}")
        embed.description = "\n".join(f"{index + 1}. {label(item)}" for index, item in enumerate(results))
        if kind == "ebook":
            # Only ebooks get the Send-to-phone button: an audiobook result
            # is an Audiobookshelf catalog entry, not a file this server can
            # hand over as a Discord attachment.
            await interaction.followup.send(
                embed=embed,
                view=EbookView(api, results, _actor(interaction), max_attachment_bytes),
                ephemeral=True,
            )
        else:
            await interaction.followup.send(embed=embed, ephemeral=True)

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
            results = _result_list(payload)[:5]
            if not results:
                await interaction.followup.send(
                    f"No matching {kind}s were found to download.", ephemeral=True
                )
                return
            embed = discord.Embed(title=f"{type.name} downloads for {query}")
            embed.description = "\n".join(f"{index + 1}. {_release_label(item)}" for index, item in enumerate(results))
            await interaction.followup.send(
                embed=embed,
                view=ReleaseView(
                    api, results, _actor(interaction), large_release_threshold_bytes, guard
                ),
                ephemeral=True,
            )
        except ServiceError:
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
        except ServiceError:
            await interaction.followup.send("qBittorrent download status is unavailable.", ephemeral=True)

    @bot.tree.command(name="job", description="Show a Shelfmark job")
    @app_commands.describe(job_id="UUID returned when the job was queued")
    async def job_status(interaction: discord.Interaction, job_id: str) -> None:
        if not await guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            payload = await api.get(f"/api/v1/jobs/{job_id}", actor=_actor(interaction))
            await interaction.followup.send(_job_status_message(payload, job_id), ephemeral=True)
        except ServiceError:
            await interaction.followup.send("That job could not be loaded.", ephemeral=True)

    @bot.tree.command(name="metadata-match", description="Queue an Audiobookshelf metadata match")
    @app_commands.describe(item_id="Audiobookshelf library item ID", title="Optional title hint", author="Optional author hint")
    async def metadata_match(interaction: discord.Interaction, item_id: str, title: str | None = None, author: str | None = None) -> None:
        if not await guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        payload: dict[str, Any] = {}
        if title:
            payload["title"] = title
        if author:
            payload["author"] = author
        try:
            result = await api.post(
                f"/api/v1/items/{item_id}/match",
                json_body=payload,
                actor=_actor(interaction),
            )
            await interaction.followup.send(
                f"Queued metadata match job **{result.get('id', 'unknown')}**.",
                ephemeral=True,
            )
        except ServiceError:
            await interaction.followup.send("The metadata match job could not be queued.", ephemeral=True)

    @bot.tree.command(name="scan", description="Queue an Audiobookshelf library scan")
    @app_commands.describe(library_id="Audiobookshelf library ID", force="Force a full rescan")
    async def scan(interaction: discord.Interaction, library_id: str, force: bool = False) -> None:
        if not await guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await api.post(
                f"/api/v1/libraries/{library_id}/scan",
                json_body={"force": force},
                actor=_actor(interaction),
            )
            await interaction.followup.send(
                f"Queued library scan job **{result.get('id', 'unknown')}**.",
                ephemeral=True,
            )
        except ServiceError:
            await interaction.followup.send("The library scan job could not be queued.", ephemeral=True)

    @bot.tree.command(name="organize-preview", description="Preview organizing an incoming folder")
    @app_commands.describe(source="Absolute path mounted in the worker", destination="Optional destination root")
    async def organize_preview(interaction: discord.Interaction, source: str, destination: str | None = None) -> None:
        if not await guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        payload = {"source": source}
        if destination:
            payload["dest"] = destination
        try:
            result = await api.post(
                "/api/v1/jobs",
                json_body={"kind": "organize_preview", "payload": payload},
                actor=_actor(interaction),
            )
            await interaction.followup.send(
                f"Queued preview job **{result.get('id', 'unknown')}**. Use `/job` for status.",
                ephemeral=True,
            )
        except ServiceError:
            await interaction.followup.send("The preview job could not be queued.", ephemeral=True)


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

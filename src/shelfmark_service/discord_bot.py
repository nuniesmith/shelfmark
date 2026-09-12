"""Discord slash-command adapter for the internal Shelfmark API.

The bot acknowledges every interaction before doing network work.  Search
results are shown ephemerally, and a button creates an asynchronous API job so
the interaction token is never used as a long-running task channel.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from .clients import HttpClient, ServiceError


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


class ReleaseView(discord.ui.View):
    def __init__(self, api: ShelfmarkApi, releases: list[dict[str, Any]], actor: str):
        super().__init__(timeout=900)
        self.api = api
        self.releases = releases
        self.actor = actor
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
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                result = await self.api.post(
                    "/api/v1/releases/grab",
                    json_body={"release": self.releases[index]},
                    actor=_actor(interaction),
                )
                job_id = result.get("id", "unknown") if isinstance(result, dict) else "unknown"
                await interaction.followup.send(
                    f"Queued release **{job_id}**. Use `/job {job_id}` for status.",
                    ephemeral=True,
                )
            except ServiceError:
                await interaction.followup.send("The Shelfmark API could not queue that release.", ephemeral=True)

        return callback


def install_commands(bot: commands.Bot, api: ShelfmarkApi, allowed_roles: set[int]) -> None:
    def permitted(interaction: discord.Interaction) -> bool:
        if not allowed_roles:
            return True
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        return bool(member and {role.id for role in member.roles} & allowed_roles)

    async def guard(interaction: discord.Interaction) -> bool:
        if permitted(interaction):
            return True
        await interaction.response.send_message("You are not allowed to use Shelfmark.", ephemeral=True)
        return False

    @bot.tree.command(name="library-search", description="Search books already in Audiobookshelf")
    @app_commands.describe(query="Title, author, or series to search for")
    async def library_search(interaction: discord.Interaction, query: str) -> None:
        if not await guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            payload = await api.get("/api/v1/library/search", params={"q": query}, actor=_actor(interaction))
            results = _result_list(payload)[:10]
            if not results:
                await interaction.followup.send("No matching library items found.", ephemeral=True)
                return
            embed = discord.Embed(title=f"Library results for {query}")
            embed.description = "\n".join(f"{index + 1}. {_library_label(item)}" for index, item in enumerate(results))
            await interaction.followup.send(embed=embed, ephemeral=True)
        except ServiceError:
            await interaction.followup.send("Audiobookshelf search is unavailable.", ephemeral=True)

    @bot.tree.command(name="release-search", description="Search Prowlarr for new audiobook or ebook releases")
    @app_commands.describe(query="Title, author, ISBN, or other release query", type="Prowlarr search type")
    async def release_search(interaction: discord.Interaction, query: str, type: str | None = None) -> None:
        if not await guard(interaction):
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            params = {"q": query, "limit": 50}
            if type:
                params["type"] = type
            payload = await api.get("/api/v1/releases/search", params=params, actor=_actor(interaction))
            results = _result_list(payload)[:5]
            if not results:
                await interaction.followup.send("No matching releases found.", ephemeral=True)
                return
            embed = discord.Embed(title=f"Release results for {query}")
            embed.description = "\n".join(f"{index + 1}. {_release_label(item)}" for index, item in enumerate(results))
            await interaction.followup.send(
                embed=embed,
                view=ReleaseView(api, results, _actor(interaction)),
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
            await interaction.followup.send(
                f"Job **{payload.get('id', job_id)}**: `{payload.get('status', 'unknown')}`\n"
                f"Attempts: {payload.get('attempts', 0)}",
                ephemeral=True,
            )
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


def build_bot() -> commands.Bot:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise ValueError("DISCORD_BOT_TOKEN is required")
    api_url = os.environ.get("SHELFMARK_API_URL", "http://127.0.0.1:8000")
    api_token = os.environ.get("SHELFMARK_API_TOKEN")
    if not api_token:
        raise ValueError("SHELFMARK_API_TOKEN is required for the bot")
    guild_id_text = os.environ.get("SHELFMARK_DISCORD_GUILD_ID")
    guild_id = int(guild_id_text) if guild_id_text else None
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

    bot = ShelfmarkBot(command_prefix=commands.when_mentioned, intents=intents)
    install_commands(bot, ShelfmarkApi(api_url, api_token), _int_set(os.environ.get("SHELFMARK_DISCORD_ALLOWED_ROLE_IDS")))
    return bot


def main() -> None:
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is required")
    build_bot().run(token)


if __name__ == "__main__":
    main()

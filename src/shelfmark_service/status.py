"""One live Discord message per book, edited as it moves through the pipeline.

Before this, a request produced the bot's reply to the grab and then, minutes
later, either "is in the library" or a warning. Everything between (still
downloading? copying? stuck?) was invisible unless someone ran `/downloads` or
`/job`. Now the worker keeps ONE webhook message per torrent and edits it at
every stage:

    **Some Book (m4b)**
    ✅ Downloaded
    ✅ Copied to freddy
    ⏳ Extracting and filing
    ▫️ Adding to the library

Best effort, like `Worker._notify`: a Discord outage never fails a job. The
`status_messages` row keeps the message id, so a worker restart edits the same
message instead of starting a new one, and a message someone deleted in
Discord is simply posted again.
"""

from __future__ import annotations

import logging
from typing import Any

from .clients import HttpClient, ServiceError
from .config import Settings
from .db import Database

logger = logging.getLogger("shelfmark.status")

STAGES = ("download", "copy", "organize", "library")

# (in progress, done) for each stage. A stage can override its label, e.g.
# an ebook's last stage, which has no Audiobookshelf scan behind it.
LABELS = {
    "download": ("Downloading", "Downloaded"),
    "copy": ("Copying to freddy", "Copied to freddy"),
    "organize": ("Extracting and filing", "Filed"),
    "library": ("Adding to the library", "Ready in the library"),
}

ICONS = {"pending": "▫️", "active": "⏳", "done": "✅", "failed": "❌"}

# Which stage each reconciler-chain job kind is.
STAGE_OF_KIND = {
    "transfer_completed": "copy",
    "organize_apply": "organize",
    "library_scan": "library",
}


def split_webhook_url(url: str) -> tuple[str, str]:
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


def render(name: str, stages: dict[str, dict[str, Any]]) -> str:
    """The message text: the book's name, then one line per stage."""
    lines = [f"**{name}**"]
    for stage in STAGES:
        entry = stages.get(stage) or {}
        state = entry.get("state") if entry.get("state") in ICONS else "pending"
        active_label, done_label = LABELS[stage]
        label = entry.get("label") or (done_label if state == "done" else active_label)
        line = f"{ICONS[state]} {label}"
        detail = entry.get("detail")
        if detail:
            line += f": {detail}" if state == "failed" else f" ({detail})"
        lines.append(line)
    return "\n".join(lines)[:2000]


def fresh_stages() -> dict[str, dict[str, Any]]:
    return {stage: {"state": "pending"} for stage in STAGES}


class StatusBoard:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings

    def update(self, torrent_hash: str, name: str, changes: dict[str, dict[str, Any]]) -> None:
        """Apply `changes` ({stage: {"state", "detail"?, "label"?}}) and post or edit.

        Never raises. The first update for a hash posts the message; later
        ones edit it. `name` is used only for the first one, so the title a
        person saw when they asked for the book stays put.
        """
        try:
            self._update(torrent_hash, name, changes)
        except Exception:  # noqa: BLE001 - a courtesy, never a job failure
            logger.warning("status message update failed", exc_info=True)

    def _update(self, torrent_hash: str, name: str, changes: dict[str, dict[str, Any]]) -> None:
        url = (self.settings.discord_webhook_url or "").strip()
        if not self.settings.status_messages_enabled or not url or not torrent_hash:
            return
        h = torrent_hash.strip().lower()
        row = self.database.get_status_message(h)
        stages = row["stages"] if row else fresh_stages()
        title = row["name"] if row else (name or h)
        for stage, change in changes.items():
            if stage in STAGES:
                stages[stage] = dict(change)
        message_id = self._send(url, render(title, stages), row["message_id"] if row else None)
        self.database.save_status_message(h, title, message_id, stages)

    def _send(self, url: str, content: str, message_id: str | None) -> str | None:
        base, token = split_webhook_url(url)
        if not base or not token:
            logger.warning("SHELFMARK_DISCORD_WEBHOOK_URL is not a usable webhook URL")
            return message_id
        client = HttpClient(base, service="discord-webhook", timeout=self.settings.http_timeout, retries=1)
        # Book titles are plain text: never let one ping @everyone.
        body = {"content": content, "allowed_mentions": {"parse": []}}
        if message_id:
            try:
                client.request(f"{token}/messages/{message_id}", method="PATCH", json_body=body)
                return message_id
            except ServiceError as exc:
                if getattr(exc, "status", None) != 404:
                    logger.warning("status message edit failed (%s)", exc)
                    return message_id
                # Deleted in Discord: post a fresh one below.
        try:
            posted = client.request(token, method="POST", params={"wait": "true"}, json_body=body)
        except (ServiceError, ValueError) as exc:
            logger.warning("status message post failed (%s)", exc)
            return message_id
        if isinstance(posted, dict) and posted.get("id"):
            return str(posted["id"])
        return message_id

"""Environment-backed settings shared by the API and worker."""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from pathlib import Path


def _path_from_env(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else None


def _float_from_env(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return max(0.1, float(value))
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc


@dataclass(frozen=True)
class Settings:
    """Runtime paths and service settings.

    Empty media roots are allowed during development.  The readiness endpoint
    checks only roots that have been configured, which lets the API start before
    the Freddy bind mounts are supplied by Compose.
    """

    database_path: Path = Path("/data/shelfmark.db")
    audio_root: Path | None = None
    ebook_root: Path | None = None
    incoming_root: Path | None = None
    work_root: Path | None = None
    quarantine_root: Path | None = None
    api_token: str | None = None
    worker_id: str = "shelfmark-worker"
    poll_interval: float = 2.0

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            database_path=_path_from_env("SHELFMARK_DB_PATH") or Path("/data/shelfmark.db"),
            audio_root=_path_from_env("SHELFMARK_AUDIOBOOKS_ROOT"),
            ebook_root=_path_from_env("SHELFMARK_EBOOKS_ROOT"),
            incoming_root=_path_from_env("SHELFMARK_INCOMING_ROOT"),
            work_root=_path_from_env("SHELFMARK_WORK_ROOT"),
            quarantine_root=_path_from_env("SHELFMARK_QUARANTINE_ROOT"),
            api_token=os.environ.get("SHELFMARK_API_TOKEN") or None,
            worker_id=os.environ.get("SHELFMARK_WORKER_ID") or socket.gethostname(),
            poll_interval=_float_from_env("SHELFMARK_WORKER_POLL_SECONDS", 2.0),
        )

    def configured_roots(self) -> dict[str, Path]:
        return {
            name: path
            for name, path in {
                "audio_root": self.audio_root,
                "ebook_root": self.ebook_root,
                "incoming_root": self.incoming_root,
                "work_root": self.work_root,
                "quarantine_root": self.quarantine_root,
            }.items()
            if path is not None
        }

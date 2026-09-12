"""Append-only transaction manifests for worker jobs."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


class JsonlManifest:
    """Write one fsynced JSON event per line so interrupted jobs stay inspectable."""

    def __init__(self, path: Path, actor: str | None = None):
        self.path = Path(path).expanduser()
        self.actor = actor
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def event(self, event: str, **details: Any) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "event": event,
            **details,
        }
        if self.actor:
            record["actor"] = self.actor
        encoded = (json.dumps(record, sort_keys=True, default=str) + "\n").encode("utf-8")
        with self.path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

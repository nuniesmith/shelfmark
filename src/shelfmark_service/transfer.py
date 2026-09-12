"""Restricted Sullivan-to-Freddy transfer helpers."""

from __future__ import annotations

import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence


class TransferError(RuntimeError):
    pass


def snapshot_tree(root: Path) -> dict[str, tuple[int, int]]:
    """Return relative-file size/mtime pairs for a completed download tree."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise TransferError(f"transfer path is not a directory: {root}")
    result: dict[str, tuple[int, int]] = {}
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            stat = path.stat()
            result[str(path.relative_to(root))] = (stat.st_size, stat.st_mtime_ns)
    return result


def wait_until_stable(
    root: Path,
    *,
    settle_seconds: float = 30.0,
    poll_seconds: float = 5.0,
    timeout_seconds: float = 3600.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, tuple[int, int]]:
    """Wait until a local tree has the same file snapshot twice.

    The age check prevents a just-created file from being declared stable when
    both samples happen within the same filesystem timestamp tick.
    """
    root = Path(root).expanduser().resolve()
    deadline = time.monotonic() + max(0.1, timeout_seconds)
    previous = snapshot_tree(root)
    if not previous:
        raise TransferError(f"no files found while waiting for transfer: {root}")
    while time.monotonic() < deadline:
        delay = min(max(0.1, poll_seconds), max(0.1, deadline - time.monotonic()))
        sleep(delay)
        current = snapshot_tree(root)
        if current != previous:
            previous = current
            continue
        newest_mtime = max(mtime_ns for _size, mtime_ns in current.values()) / 1_000_000_000
        if time.time() - newest_mtime >= max(0.0, settle_seconds):
            return current
    raise TransferError(f"transfer did not become stable before timeout: {root}")


@dataclass(frozen=True)
class RsyncTransfer:
    host: str
    user: str
    identity_file: Path | None = None
    port: int = 22
    timeout_seconds: float = 30.0
    retries: int = 2
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run

    def _ssh_command(self) -> str:
        args = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(self.timeout_seconds)}"]
        if self.port != 22:
            args.extend(["-p", str(self.port)])
        if self.identity_file:
            args.extend(["-i", str(Path(self.identity_file).expanduser())])
        return " ".join(shlex.quote(arg) for arg in args)

    def pull(self, remote_path: str, local_path: Path) -> None:
        if not remote_path or remote_path.startswith("-"):
            raise TransferError("remote_path must be a non-empty path")
        local_path = Path(local_path).expanduser().resolve()
        local_path.mkdir(parents=True, exist_ok=True)
        remote = f"{self.user}@{self.host}:{remote_path.rstrip('/')}/"
        command: Sequence[str] = (
            "rsync",
            "--archive",
            "--partial",
            "--protect-args",
            "--human-readable",
            "--itemize-changes",
            "-e",
            self._ssh_command(),
            remote,
            str(local_path) + os.sep,
        )
        last_error = "rsync failed"
        for attempt in range(max(0, self.retries) + 1):
            result = self.runner(
                list(command),
                check=False,
                capture_output=True,
                text=True,
                timeout=max(1.0, self.timeout_seconds),
            )
            if result.returncode == 0:
                return
            last_error = (result.stderr or result.stdout or last_error).strip()[-2000:]
            if attempt < self.retries:
                time.sleep(min(30.0, 2**attempt))
        raise TransferError(last_error)

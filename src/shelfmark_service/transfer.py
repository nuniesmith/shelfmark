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


def content_differences(itemized: str) -> list[str]:
    """Paths whose CONTENT differs, from `rsync --itemize-changes` output.

    Only content counts. A directory whose mtime differs, or a file whose
    permissions differ, is not a corrupt transfer, and treating it as one would
    make verification fail constantly for no reason.

    rsync's itemized format is an 11-character change string then the path:
    `>fcst......` means a file being received whose checksum (c), size (s) and
    time (t) differ. Position 0 is the update type, 1 the entry type.
    """
    differences: list[str] = []
    for line in itemized.splitlines():
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("*deleting"):
            differences.append(line.split(None, 1)[-1])
            continue
        if len(line) < 13 or line[1] != "f":
            continue  # not a file entry
        flags, path = line[:11], line[12:]
        if flags[0] in "<>ch" or "c" in flags[2:] or "s" in flags[2:]:
            differences.append(path)
    return differences


@dataclass(frozen=True)
class RsyncTransfer:
    host: str
    user: str
    identity_file: Path | None = None
    port: int = 22
    timeout_seconds: float = 30.0
    retries: int = 2
    known_hosts: Path | None = None
    strict_host_key: bool = False
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run

    def _ssh_command(self) -> str:
        args = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={int(self.timeout_seconds)}"]
        # Without these the container fails every transfer with "Host key
        # verification failed": it has no known_hosts entry for Sullivan, and
        # BatchMode=yes correctly refuses to prompt for one. The bug survived
        # because hand-run checks pass these options on the command line, so
        # the ACCOUNT verifies while the CODE PATH stays broken.
        #
        # `accept-new` trusts the key the first time and pins it thereafter, so
        # a later change is still refused. Set strict_host_key once the file
        # holds a key you have checked, and even the first contact must match.
        if self.known_hosts is not None:
            known = Path(self.known_hosts).expanduser()
            known.parent.mkdir(parents=True, exist_ok=True)
            args.extend(["-o", f"UserKnownHostsFile={known}"])
        args.extend(
            ["-o", f"StrictHostKeyChecking={'yes' if self.strict_host_key else 'accept-new'}"]
        )
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

    def verify(self, remote_path: str, local_path: Path) -> list[str]:
        """Re-compare the pulled tree against Sullivan by CHECKSUM.

        rsync already guards each transfer with its own rolling checksum, so
        this is not about a corrupted wire. It is about everything after: a
        truncated write, a full disk, a file changed on either side between the
        pull and the import. The rule from the plan is "verify size and
        checksum before organization", and the organiser is destructive, so
        that verification has to be an independent pass.

        `--checksum` forces a full content comparison rather than rsync's
        default size-and-mtime heuristic, and `--dry-run` means it reports
        without writing.

        **The differences are in the OUTPUT, not the exit status** — rsync
        exits 0 whether or not anything differs. Returns the differing paths;
        empty means the local copy is byte-identical.
        """
        if not remote_path or remote_path.startswith("-"):
            raise TransferError("remote_path must be a non-empty path")
        local_path = Path(local_path).expanduser().resolve()
        remote = f"{self.user}@{self.host}:{remote_path.rstrip('/')}/"
        command: Sequence[str] = (
            "rsync",
            "--archive",
            "--checksum",
            "--dry-run",
            "--itemize-changes",
            "--protect-args",
            "-e",
            self._ssh_command(),
            remote,
            str(local_path) + os.sep,
        )
        result = self.runner(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=max(1.0, self.timeout_seconds),
        )
        if result.returncode != 0:
            raise TransferError(
                (result.stderr or result.stdout or "verification failed").strip()[-2000:]
            )
        return content_differences(result.stdout or "")

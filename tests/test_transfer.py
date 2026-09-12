from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from src.shelfmark_service.transfer import RsyncTransfer, TransferError, wait_until_stable


class TransferTests(unittest.TestCase):
    def test_stable_snapshot_requires_no_changes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="shelfmark-transfer-") as tmp:
            root = Path(tmp)
            file = root / "book" / "01.mp3"
            file.parent.mkdir()
            file.write_bytes(b"audio")
            old = file.stat().st_mtime - 60
            os.utime(file, (old, old))
            snapshot = wait_until_stable(
                root,
                settle_seconds=0,
                poll_seconds=0.1,
                timeout_seconds=1,
                sleep=lambda _seconds: None,
            )
            self.assertEqual(snapshot["book/01.mp3"][0], 5)

    def test_rsync_uses_argument_list_and_restricted_ssh(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []

        def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory(prefix="shelfmark-transfer-") as tmp:
            transfer = RsyncTransfer(
                host="sullivan.internal",
                user="shelfmark-sync",
                identity_file=Path(tmp) / "id_ed25519",
                port=2222,
                retries=0,
                runner=runner,
            )
            transfer.pull("/complete/shelfmark-books/Book Name", Path(tmp) / "incoming")

        self.assertEqual(len(calls), 1)
        command = calls[0][0]
        self.assertEqual(command[0], "rsync")
        self.assertIn("--protect-args", command)
        self.assertIn("shelfmark-sync@sullivan.internal:/complete/shelfmark-books/Book Name/", command)
        ssh = command[command.index("-e") + 1]
        self.assertIn("BatchMode=yes", ssh)
        self.assertIn("ConnectTimeout=30", ssh)
        self.assertIn("-i", ssh)

    def test_rsync_rejects_empty_remote_path(self) -> None:
        transfer = RsyncTransfer("host", "user", retries=0)
        with tempfile.TemporaryDirectory(prefix="shelfmark-transfer-") as tmp:
            with self.assertRaises(TransferError):
                transfer.pull("", Path(tmp))


if __name__ == "__main__":
    unittest.main()

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
        # NOT --protect-args: rrsync refuses it ("option -s has been disabled
        # on this server") and every transfer dies in the protocol handshake.
        self.assertNotIn("--protect-args", command)
        self.assertNotIn("-s", command)
        # The path keeps its spaces, raw and unescaped. A forced command has no
        # shell to word-split them, and escaping them makes rrsync look for a
        # filename containing literal backslashes.
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


class HostKeyTests(unittest.TestCase):
    """The transfer failed every time with "Host key verification failed".

    The container has no known_hosts entry for Sullivan and BatchMode=yes
    correctly refuses to prompt for one. The bug survived because every
    hand-run check passes StrictHostKeyChecking and UserKnownHostsFile on the
    command line — proving the ACCOUNT works while the CODE PATH stayed broken.
    """

    @staticmethod
    def _ssh_for(**kwargs: object) -> str:
        captured: list[list[str]] = []

        def runner(command: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            captured.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory(prefix="shelfmark-hostkey-") as tmp:
            transfer = RsyncTransfer(
                host="sullivan", user="shelfmark-sync", retries=0, runner=runner, **kwargs
            )
            transfer.pull("/Book", Path(tmp) / "incoming")
        return captured[0][captured[0].index("-e") + 1]

    def test_host_key_checking_is_always_specified(self) -> None:
        ssh = self._ssh_for()
        self.assertIn("StrictHostKeyChecking=", ssh)

    def test_default_accepts_a_new_host_then_pins_it(self) -> None:
        with tempfile.TemporaryDirectory(prefix="shelfmark-hostkey-") as tmp:
            known = Path(tmp) / "nested" / "known_hosts"
            ssh = self._ssh_for(known_hosts=known)
            self.assertIn("StrictHostKeyChecking=accept-new", ssh)
            self.assertIn(f"UserKnownHostsFile={known}", ssh)
            # Created eagerly: ssh will not write into a directory that is not
            # there, and would fail the transfer rather than record the key.
            self.assertTrue(known.parent.is_dir())

    def test_strict_mode_refuses_an_unknown_host(self) -> None:
        ssh = self._ssh_for(strict_host_key=True)
        self.assertIn("StrictHostKeyChecking=yes", ssh)


class VerificationTests(unittest.TestCase):
    """`rsync --checksum --dry-run` exits 0 whether or not anything differs.

    The differences are in the OUTPUT. Reading the exit status instead would
    report every transfer as verified, including a corrupt one.
    """

    def _verify_with(self, stdout: str) -> list[str]:
        def runner(command: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            self.assertIn("--checksum", command)
            self.assertIn("--dry-run", command)
            return subprocess.CompletedProcess(command, 0, stdout, "")

        with tempfile.TemporaryDirectory(prefix="shelfmark-verify-") as tmp:
            transfer = RsyncTransfer("sullivan", "shelfmark-sync", retries=0, runner=runner)
            return transfer.verify("/Book", Path(tmp))

    def test_identical_tree_reports_no_differences(self) -> None:
        self.assertEqual(self._verify_with(""), [])

    def test_a_changed_checksum_is_reported(self) -> None:
        # Exactly what rrsync returned for a locally corrupted file.
        out = ">fcst...... Ursula K Le Guin - The Dispossessed (1974)/01.mp3"
        self.assertEqual(
            self._verify_with(out), ["Ursula K Le Guin - The Dispossessed (1974)/01.mp3"]
        )

    def test_a_missing_file_is_reported(self) -> None:
        self.assertEqual(self._verify_with(">f+++++++++ book/02.mp3"), ["book/02.mp3"])

    def test_directory_mtime_alone_is_not_a_difference(self) -> None:
        """A directory timestamp is not a corrupt transfer. Treating it as one
        would fail verification on essentially every pull."""
        self.assertEqual(self._verify_with(".d..t...... ./"), [])

    def test_file_permission_change_alone_is_not_a_difference(self) -> None:
        self.assertEqual(self._verify_with(".f....p.... book/01.mp3"), [])

    def test_a_failed_verification_run_raises_rather_than_passing(self) -> None:
        def runner(command: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, 255, "", "Host key verification failed.")

        with tempfile.TemporaryDirectory(prefix="shelfmark-verify-") as tmp:
            transfer = RsyncTransfer("sullivan", "shelfmark-sync", retries=0, runner=runner)
            with self.assertRaises(TransferError):
                transfer.verify("/Book", Path(tmp))


class RrsyncCompatibilityTests(unittest.TestCase):
    """Options the restricted forced command will not accept.

    The account is reached only through `rrsync -ro`, which allows a fixed set
    of options and refuses the rest before rsync's protocol handshake even
    completes. An option added for good reasons elsewhere can therefore break
    every transfer, and the failure reads as a protocol error rather than as a
    rejected flag.
    """

    def _command_for(self, method: str) -> list[str]:
        captured: list[list[str]] = []

        def runner(command: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
            captured.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        with tempfile.TemporaryDirectory(prefix="shelfmark-rrsync-") as tmp:
            transfer = RsyncTransfer("sullivan", "shelfmark-sync", retries=0, runner=runner)
            getattr(transfer, method)("/Book Name", Path(tmp))
        return captured[0]

    def test_neither_call_uses_an_option_rrsync_refuses(self) -> None:
        refused = {"--protect-args", "-s", "--secluded-args"}
        for method in ("pull", "verify"):
            with self.subTest(method=method):
                self.assertEqual(refused & set(self._command_for(method)), set())

    def test_spaces_in_the_remote_path_are_left_alone(self) -> None:
        for method in ("pull", "verify"):
            with self.subTest(method=method):
                spec = [a for a in self._command_for(method) if a.startswith("shelfmark-sync@")]
                self.assertEqual(spec, ["shelfmark-sync@sullivan:/Book Name/"])
                self.assertNotIn("\\", spec[0])

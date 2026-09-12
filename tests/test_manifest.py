from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from src.shelfmark_service.manifest import JsonlManifest, sha256_file


class ManifestTests(unittest.TestCase):
    def test_manifest_is_append_only_jsonl_with_checksum(self) -> None:
        with tempfile.TemporaryDirectory(prefix="shelfmark-manifest-") as tmp:
            root = Path(tmp)
            source = root / "book.mp3"
            source.write_bytes(b"audio")
            manifest = JsonlManifest(root / "manifests" / "job.jsonl")
            manifest.event("started", source=str(source))
            manifest.event("operation", checksum=sha256_file(source))

            lines = (root / "manifests" / "job.jsonl").read_text().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual([json.loads(line)["event"] for line in lines], ["started", "operation"])
            self.assertEqual(len(json.loads(lines[1])["checksum"]), 64)


if __name__ == "__main__":
    unittest.main()

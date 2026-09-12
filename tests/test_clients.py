from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from src.shelfmark_service.clients import (
    AudiobookshelfClient,
    HttpClient,
    ProwlarrClient,
    QBittorrentClient,
)


class _Handler(BaseHTTPRequestHandler):
    calls: list[tuple[str, str, dict[str, str], bytes]] = []
    retry_count = 0

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length)

    def _write(self, status: int, body: object, content_type: str = "application/json") -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        body = b""
        parsed = urlparse(self.path)
        if parsed.path == "/retry":
            type(self).retry_count += 1
            if type(self).retry_count == 1:
                self._write(503, {"error": "try again"})
                return
            body = {"ok": True}
        elif parsed.path == "/api/v1/search":
            body = [{"title": "The Book", "downloadUrl": "magnet:?xt=1"}]
        elif parsed.path == "/api/libraries/lib/search":
            body = {"book": [{"libraryItem": {"id": "li-1"}}]}
        elif parsed.path == "/api/v2/app/version":
            body = b"v5.0.0"
        else:
            body = {"path": parsed.path}
        self._write(200, body, "text/plain" if isinstance(body, str) else "application/json")
        type(self).calls.append(("GET", self.path, dict(self.headers), b""))

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        body = self._body()
        parsed = urlparse(self.path)
        type(self).calls.append(("POST", self.path, dict(self.headers), body))
        if parsed.path == "/api/v2/auth/login":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Set-Cookie", "SID=test; Path=/")
            self.send_header("Content-Length", "3")
            self.end_headers()
            self.wfile.write(b"Ok.")
            return
        self._write(200, {"ok": True})


class ClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        _Handler.calls = []
        _Handler.retry_count = 0
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_retry_and_json_decode(self) -> None:
        client = HttpClient(self.base, service="test", retries=1, backoff=0)
        self.assertEqual(client.request("/retry"), {"ok": True})
        self.assertEqual(_Handler.retry_count, 2)

    def test_service_headers_and_payloads(self) -> None:
        prowlarr = ProwlarrClient(self.base, "prow-key", retries=0)
        self.assertEqual(prowlarr.search("some book", indexer_ids=[7]), [{"title": "The Book", "downloadUrl": "magnet:?xt=1"}])
        prowlarr.grab({"guid": "release-1"})

        abs_client = AudiobookshelfClient(self.base, "abs-token", retries=0)
        self.assertEqual(abs_client.search("lib", "book")["book"][0]["libraryItem"]["id"], "li-1")

        qbit = QBittorrentClient(self.base, username="user", password="pass", retries=0)
        self.assertEqual(qbit.login(), "Ok.")
        self.assertEqual(qbit.version(), "v5.0.0")
        qbit.add_urls(["magnet:?xt=1", "https://example.test/a.torrent"], category="shelfmark-books")

        posts = [item for item in _Handler.calls if item[0] == "POST"]
        login = next(item for item in posts if "/auth/login" in item[1])
        self.assertIn("username=user", login[3].decode())
        self.assertIn("X-Api-Key", next(item for item in _Handler.calls if item[1].startswith("/api/v1/search"))[2])
        add = next(item for item in posts if "/torrents/add" in item[1])
        self.assertEqual(parse_qs(add[3].decode())["category"], ["shelfmark-books"])
        self.assertIn("SID=test", next(item for item in _Handler.calls if item[1].endswith("/api/v2/app/version"))[2].get("Cookie", ""))


if __name__ == "__main__":
    unittest.main()

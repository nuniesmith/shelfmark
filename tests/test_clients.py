from __future__ import annotations

import email.message
import io
import json
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from src.shelfmark_service.clients import (
    AudiobookshelfClient,
    CircuitBreaker,
    CircuitBreakerOpenError,
    HttpClient,
    ProwlarrClient,
    QBittorrentClient,
    ServiceError,
)


class _FakeClock:
    """A controllable stand-in for time.monotonic so breaker tests never sleep."""

    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _http_error(code: int, body: bytes = b"boom", headers: email.message.Message | None = None) -> urllib.error.HTTPError:
    """Build a real HTTPError without a socket, so the breaker's decode of the
    exception (code, headers, read/close) exercises the same code paths a
    live server response would."""
    return urllib.error.HTTPError("http://svc/x", code, "err", headers or email.message.Message(), io.BytesIO(body))


class _FakeHeaders:
    def __init__(self, content_type: str = "application/json"):
        self._content_type = content_type

    def get_content_type(self) -> str:
        return self._content_type


class _FakeResponse:
    """Minimal stand-in for the context-managed response HttpClient reads."""

    def __init__(self, body: bytes = b'{"ok":true}'):
        self._body = body
        self.headers = _FakeHeaders()

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_exc_info: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


class _ScriptedOpener:
    """A fake OpenerDirector: pops one scripted outcome per .open() call.

    Each outcome is either an exception instance to raise (URLError/OSError
    for a connection failure, HTTPError for a status response) or a
    _FakeResponse to return. Used so breaker tests control exactly how many
    real "network attempts" happen without any socket or server involved.
    """

    def __init__(self, script: list[object]):
        self._script = list(script)
        self.calls = 0

    def open(self, req: object, timeout: float | None = None) -> _FakeResponse:
        self.calls += 1
        if not self._script:
            raise AssertionError("opener.open called more times than scripted")
        outcome = self._script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


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


class CircuitBreakerUnitTests(unittest.TestCase):
    """Exercises CircuitBreaker directly: no sockets, no HttpClient, a fake
    clock instead of real sleeps -- these should run in well under a second."""

    def test_circuit_breaker_open_error_is_a_service_error(self) -> None:
        # Existing call sites (api.py, discord_bot.py) do `except ServiceError`;
        # a breaker trip must still be caught there, not blow past them.
        self.assertIsInstance(CircuitBreakerOpenError("svc", 1.0), ServiceError)

    def test_closed_state_allows_calls(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10, clock=clock)
        breaker.before_call("svc")  # must not raise

    def test_failures_below_threshold_do_not_open(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10, clock=clock)
        breaker.record_failure()
        breaker.record_failure()
        breaker.before_call("svc")  # 2 of 3: must still let calls through

    def test_reaching_threshold_opens_and_fails_fast(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10, clock=clock)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_failure()
        with self.assertRaises(CircuitBreakerOpenError):
            breaker.before_call("svc")

    def test_success_resets_the_consecutive_failure_count(self) -> None:
        # A one-off failure followed by a success is not two-of-three
        # consecutive failures; without the reset, unrelated occasional
        # failures across many jobs would eventually add up and trip the
        # breaker even though the provider is fine.
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10, clock=clock)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        breaker.record_failure()
        breaker.before_call("svc")  # still only 2 consecutive: must not raise

    def test_stays_open_until_cooldown_elapses(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=10, clock=clock)
        breaker.record_failure()
        clock.advance(5)
        with self.assertRaises(CircuitBreakerOpenError):
            breaker.before_call("svc")
        clock.advance(4.99)  # total 9.99s: still short of the 10s cooldown
        with self.assertRaises(CircuitBreakerOpenError):
            breaker.before_call("svc")
        clock.advance(0.01)  # total 10s: cooldown has now fully elapsed
        breaker.before_call("svc")  # the half-open trial: must not raise

    def test_only_one_half_open_trial_is_permitted(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=10, clock=clock)
        breaker.record_failure()
        clock.advance(10)
        breaker.before_call("svc")  # first caller: gets the trial
        with self.assertRaises(CircuitBreakerOpenError):
            # A second caller arriving while the trial is still unresolved
            # must not also get a live request through -- that would pile a
            # second attempt onto a provider we just decided was down.
            breaker.before_call("svc")

    def test_half_open_success_closes_the_circuit(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=10, clock=clock)
        breaker.record_failure()
        clock.advance(10)
        breaker.before_call("svc")  # the trial
        breaker.record_success()
        breaker.before_call("svc")  # closed now: must not raise, no more cooldown

    def test_half_open_failure_reopens_immediately(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=5, cooldown_seconds=10, clock=clock)
        for _ in range(5):
            breaker.record_failure()  # reach the threshold once, the normal way
        clock.advance(10)
        breaker.before_call("svc")  # the trial
        breaker.record_failure()  # only ONE new failure -- far below the threshold of 5
        # A failed probe re-opens on its own; it must not take another 5
        # failures to reach the configured threshold a second time.
        with self.assertRaises(CircuitBreakerOpenError):
            breaker.before_call("svc")


class HttpClientBreakerTests(unittest.TestCase):
    """Exercises the breaker through HttpClient.request via a scripted fake
    opener, so no socket is ever opened and no test waits on a real cooldown."""

    def test_connection_errors_trip_breaker_then_skip_the_network(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=30, clock=clock)
        opener = _ScriptedOpener([urllib.error.URLError("refused"), urllib.error.URLError("refused")])
        client = HttpClient(
            "http://example.invalid", service="conn-test", retries=0, backoff=0, opener=opener, breaker=breaker
        )
        with self.assertRaises(ServiceError) as ctx:
            client.request("/x")
        self.assertNotIsInstance(ctx.exception, CircuitBreakerOpenError)
        with self.assertRaises(ServiceError) as ctx:
            client.request("/x")
        self.assertNotIsInstance(ctx.exception, CircuitBreakerOpenError)
        # Breaker just tripped on the 2nd consecutive failure: the 3rd call
        # must fail fast without a 3rd call into the opener.
        with self.assertRaises(CircuitBreakerOpenError):
            client.request("/x")
        self.assertEqual(opener.calls, 2)

    def test_5xx_failures_trip_the_breaker(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=30, clock=clock)
        opener = _ScriptedOpener([_http_error(500), _http_error(503)])
        client = HttpClient(
            "http://example.invalid", service="5xx-test", retries=0, backoff=0, opener=opener, breaker=breaker
        )
        with self.assertRaises(ServiceError):
            client.request("/x")
        with self.assertRaises(ServiceError):
            client.request("/x")
        with self.assertRaises(CircuitBreakerOpenError):
            client.request("/x")
        self.assertEqual(opener.calls, 2)

    def test_404_does_not_trip_the_breaker(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=30, clock=clock)
        # More 404s than the failure_threshold: a bad item id must never
        # disable the provider for every other job behind it in the queue.
        opener = _ScriptedOpener([_http_error(404) for _ in range(5)])
        client = HttpClient(
            "http://example.invalid", service="404-test", retries=0, backoff=0, opener=opener, breaker=breaker
        )
        for _ in range(5):
            with self.assertRaises(ServiceError) as ctx:
                client.request("/x")
            self.assertNotIsInstance(ctx.exception, CircuitBreakerOpenError)
        self.assertEqual(opener.calls, 5)  # every call really reached "the network"

    def test_401_does_not_trip_the_breaker(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=30, clock=clock)
        opener = _ScriptedOpener([_http_error(401), _http_error(401)])
        client = HttpClient(
            "http://example.invalid", service="401-test", retries=0, backoff=0, opener=opener, breaker=breaker
        )
        for _ in range(2):
            with self.assertRaises(ServiceError) as ctx:
                client.request("/x")
            self.assertNotIsInstance(ctx.exception, CircuitBreakerOpenError)
        self.assertEqual(opener.calls, 2)

    def test_429_does_not_trip_the_breaker(self) -> None:
        # 429 is retryable (existing behaviour, unchanged) but is the
        # provider rate-limiting, not the provider being down -- it must not
        # count toward the breaker any more than a 404 would.
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=30, clock=clock)
        opener = _ScriptedOpener([_http_error(429), _http_error(429)])
        client = HttpClient(
            "http://example.invalid", service="429-test", retries=0, backoff=0, opener=opener, breaker=breaker
        )
        for _ in range(2):
            with self.assertRaises(ServiceError) as ctx:
                client.request("/x")
            self.assertNotIsInstance(ctx.exception, CircuitBreakerOpenError)
        self.assertEqual(opener.calls, 2)

    def test_half_open_probe_succeeds_and_closes_circuit(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=10, clock=clock)
        opener = _ScriptedOpener(
            [urllib.error.URLError("down"), _FakeResponse(b'{"ok":true}'), _FakeResponse(b'{"ok":true}')]
        )
        client = HttpClient(
            "http://example.invalid", service="half-open-ok", retries=0, backoff=0, opener=opener, breaker=breaker
        )
        with self.assertRaises(ServiceError):
            client.request("/x")  # trips the breaker
        with self.assertRaises(CircuitBreakerOpenError):
            client.request("/x")  # still within cooldown
        self.assertEqual(opener.calls, 1)
        clock.advance(10)
        self.assertEqual(client.request("/x"), {"ok": True})  # the trial: succeeds
        self.assertEqual(client.request("/x"), {"ok": True})  # closed: no cooldown needed now
        self.assertEqual(opener.calls, 3)

    def test_half_open_probe_failure_reopens_and_keeps_failing_fast(self) -> None:
        clock = _FakeClock()
        breaker = CircuitBreaker(failure_threshold=1, cooldown_seconds=10, clock=clock)
        opener = _ScriptedOpener([urllib.error.URLError("down"), urllib.error.URLError("still down")])
        client = HttpClient(
            "http://example.invalid", service="half-open-fail", retries=0, backoff=0, opener=opener, breaker=breaker
        )
        with self.assertRaises(ServiceError):
            client.request("/x")  # trips the breaker
        clock.advance(10)
        with self.assertRaises(ServiceError) as ctx:
            client.request("/x")  # the trial: fails again
        self.assertNotIsInstance(ctx.exception, CircuitBreakerOpenError)
        # Re-opened immediately on the failed probe: no third network
        # attempt until a fresh cooldown elapses.
        with self.assertRaises(CircuitBreakerOpenError):
            client.request("/x")
        self.assertEqual(opener.calls, 2)


if __name__ == "__main__":
    unittest.main()

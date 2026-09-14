"""Small, retrying clients for the existing media-service APIs.

The clients deliberately return the upstream JSON shape.  Normalization belongs
in the job layer because release and library metadata evolve independently of
the transport protocol.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Any, Callable, Mapping


class ServiceError(RuntimeError):
    """An upstream request failed after retrying."""

    def __init__(self, service: str, message: str, status: int | None = None):
        self.service = service
        self.status = status
        self.message = message
        super().__init__(f"{service}: {message}")


class CircuitBreakerOpenError(ServiceError):
    """Raised in place of a real attempt while a service's breaker is open.

    Subclassing ServiceError means every existing `except ServiceError` call
    site (api.py, discord_bot.py) keeps working unchanged, while code that
    cares can `isinstance()`-check for this specifically to tell "we did not
    even try" apart from "we tried and the provider said no".
    """

    def __init__(self, service: str, retry_after: float):
        self.retry_after = max(0.0, retry_after)
        super().__init__(
            service,
            f"circuit open, provider assumed down; retry after {self.retry_after:.1f}s",
            status=None,
        )


class CircuitBreaker:
    """Consecutive-failure tracker shared by every HttpClient built for one
    upstream service.

    HttpClient instances are constructed fresh per job (see worker.execute),
    so a breaker stored as one of its instance attributes would reset before
    it ever saw a second failure and would never trip.  Instances of this
    class are instead looked up by service name from the module-level
    `_BREAKER_REGISTRY` below, so the same breaker is reused across every
    HttpClient built for "prowlarr" (or any other service) for the life of
    the process -- which is exactly the scope a long-running worker loop or
    API process needs to remember "this provider was just down".

    A `threading.Lock` guards every state read-and-transition so two threads
    (a multi-threaded worker, or a worker plus the API process's own request
    threads sharing this module) racing on the same breaker cannot both slip
    through as the single half-open trial, or both decide they're the one
    that trips it open.
    """

    _CLOSED = "closed"
    _OPEN = "open"
    _HALF_OPEN = "half_open"

    def __init__(
        self,
        *,
        failure_threshold: int,
        cooldown_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._failure_threshold = max(1, int(failure_threshold))
        self._cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._clock = clock
        self._lock = threading.Lock()
        self._state = self._CLOSED
        self._consecutive_failures = 0
        self._opened_at = 0.0
        # True while the one allowed half-open probe hasn't resolved yet, so
        # a second thread arriving mid-probe fails fast instead of piling a
        # second live request onto a provider we just decided is down.
        self._trial_in_flight = False

    def before_call(self, service: str) -> None:
        """Raise CircuitBreakerOpenError if this call should be skipped."""
        with self._lock:
            if self._state == self._CLOSED:
                return
            if self._state == self._OPEN:
                remaining = self._cooldown_seconds - (self._clock() - self._opened_at)
                if remaining > 0:
                    raise CircuitBreakerOpenError(service, remaining)
                # Cooldown elapsed: let exactly this caller through as the
                # probe. Everyone else still fails fast until it resolves.
                # The failure count resets here too, so the probe is judged
                # only on its own outcome in record_failure() below, not on
                # however many failures it took to open the circuit before.
                self._state = self._HALF_OPEN
                self._consecutive_failures = 0
                self._trial_in_flight = True
                return
            # _HALF_OPEN: only the first arrival gets to probe.
            if self._trial_in_flight:
                raise CircuitBreakerOpenError(service, self._cooldown_seconds)
            self._trial_in_flight = True

    def record_success(self) -> None:
        """A call reached the provider and got a real response.

        Any completed HTTP exchange -- even a 4xx -- proves the provider is
        up, so this also closes a breaker that was only half-open.
        """
        with self._lock:
            self._state = self._CLOSED
            self._consecutive_failures = 0
            self._trial_in_flight = False

    def record_failure(self) -> None:
        """A call could not reach the provider or got a 5xx back."""
        with self._lock:
            self._trial_in_flight = False
            self._consecutive_failures += 1
            if self._state == self._HALF_OPEN or self._consecutive_failures >= self._failure_threshold:
                # A failed probe re-opens immediately regardless of the
                # threshold -- half-open only ever gets one try.
                self._state = self._OPEN
                self._opened_at = self._clock()


# Keyed by HttpClient.service (e.g. "prowlarr"), not by base_url: Settings
# only ever configures one URL per service, and keying this way is what lets
# a breaker opened by job N's HttpClient still be open for job N+1's, even
# though job N+1 builds a brand new HttpClient instance.
_BREAKER_REGISTRY: dict[str, CircuitBreaker] = {}
_REGISTRY_LOCK = threading.Lock()


def _shared_breaker(service: str, *, failure_threshold: int, cooldown_seconds: float) -> CircuitBreaker:
    with _REGISTRY_LOCK:
        breaker = _BREAKER_REGISTRY.get(service)
        if breaker is None:
            breaker = CircuitBreaker(failure_threshold=failure_threshold, cooldown_seconds=cooldown_seconds)
            _BREAKER_REGISTRY[service] = breaker
        return breaker


class HttpClient:
    """Dependency-free HTTP transport with bounded retries and cookie support."""

    def __init__(
        self,
        base_url: str,
        *,
        service: str,
        api_key: str | None = None,
        timeout: float = 15.0,
        retries: int = 3,
        backoff: float = 0.25,
        opener: urllib.request.OpenerDirector | None = None,
        breaker_failure_threshold: int = 5,
        breaker_cooldown_seconds: float = 30.0,
        breaker: CircuitBreaker | None = None,
    ):
        base_url = base_url.strip()
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must start with http:// or https://")
        self.base_url = base_url.rstrip("/")
        self.service = service
        self.api_key = api_key
        self.timeout = max(0.1, timeout)
        self.retries = max(0, int(retries))
        self.backoff = max(0.0, backoff)
        self.opener = opener or urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(CookieJar())
        )
        # `breaker` is an escape hatch for tests (inject one with a fake
        # clock, isolated from every other test); production code leaves it
        # unset and gets the process-wide breaker for this service name.
        self.breaker = breaker or _shared_breaker(
            service,
            failure_threshold=breaker_failure_threshold,
            cooldown_seconds=breaker_cooldown_seconds,
        )

    def request(
        self,
        path: str,
        *,
        method: str = "GET",
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
        form: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        if json_body is not None and form is not None:
            raise ValueError("json_body and form are mutually exclusive")
        url = f"{self.base_url}/{path.lstrip('/')}"
        if params:
            query = urllib.parse.urlencode(params, doseq=True)
            url = f"{url}?{query}"
        request_headers = {
            "Accept": "application/json, text/plain;q=0.9, */*;q=0.8",
            "User-Agent": "shelfmark/0.1",
        }
        if self.api_key:
            request_headers["X-Api-Key"] = self.api_key
        if headers:
            request_headers.update(headers)
        data: bytes | None = None
        if json_body is not None:
            data = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form, doseq=True).encode("utf-8")
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = urllib.request.Request(url, data=data, headers=request_headers, method=method.upper())

        # Checked once per logical call, not once per retry attempt: the
        # breaker guards "should we even try this operation", and HttpClient's
        # own retry loop below is what already handles a single operation's
        # transient hiccups. A job that is skipped here never opens a socket
        # or sleeps through a backoff -- that's the whole point when Prowlarr
        # is down and twenty queued jobs would otherwise each pay the full
        # retry budget in turn.
        self.breaker.before_call(self.service)

        last_error: ServiceError | None = None
        # `resolved` tracks whether one of the branches below already told the
        # breaker how this call turned out. It's checked in `finally` because
        # a half-open call that raises something other than HTTPError /
        # URLError / TimeoutError / OSError (a malformed body that fails to
        # decode, a truncated read, anything) would otherwise skip both
        # record_success() and record_failure(). before_call() already
        # flipped that trial's _trial_in_flight to True, and only OPEN's
        # branch re-checks the cooldown -- HALF_OPEN just sees the flag stuck
        # on and fails fast forever. That is a permanently wedged breaker,
        # which is worse than having no breaker at all, so any escape route
        # out of this call must resolve it: an exception we have no specific
        # handling for still means no usable response came back.
        resolved = False
        try:
            for attempt in range(self.retries + 1):
                try:
                    with self.opener.open(req, timeout=self.timeout) as response:
                        raw = response.read()
                        result = self._decode(raw, response.headers.get_content_type())
                    self.breaker.record_success()
                    resolved = True
                    return result
                except urllib.error.HTTPError as exc:
                    raw = exc.read()
                    exc.close()
                    detail = raw.decode("utf-8", errors="replace").strip()
                    last_error = ServiceError(
                        self.service,
                        detail or exc.reason or f"HTTP {exc.code}",
                        status=exc.code,
                    )
                    retryable = exc.code == 429 or exc.code >= 500
                    if not retryable or attempt >= self.retries:
                        # A response at all -- even 4xx/429 -- means the
                        # provider is reachable; only 5xx says the PROVIDER is
                        # unwell. Tripping the breaker on a 401/404 would let
                        # one job with a bad key or a stale item id disable
                        # the provider for every other job behind it in the
                        # queue.
                        if exc.code >= 500:
                            self.breaker.record_failure()
                        else:
                            self.breaker.record_success()
                        resolved = True
                        raise last_error from exc
                    retry_after = exc.headers.get("Retry-After")
                    self._sleep(attempt, retry_after)
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    last_error = ServiceError(self.service, str(exc))
                    if attempt >= self.retries:
                        self.breaker.record_failure()
                        resolved = True
                        raise last_error from exc
                    self._sleep(attempt, None)
            resolved = True
            raise last_error or ServiceError(self.service, "request failed")
        finally:
            if not resolved:
                self.breaker.record_failure()

    def _sleep(self, attempt: int, retry_after: str | None) -> None:
        try:
            delay = max(0.0, float(retry_after)) if retry_after else self.backoff * (2**attempt)
        except ValueError:
            delay = self.backoff * (2**attempt)
        if delay:
            time.sleep(min(delay, 30.0))

    @staticmethod
    def _decode(raw: bytes, content_type: str) -> Any:
        text = raw.decode("utf-8", errors="replace")
        if not text.strip():
            return None
        if content_type == "application/json" or text.lstrip().startswith(("{", "[")):
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
        return text


class AudiobookshelfClient:
    def __init__(self, base_url: str, token: str, **kwargs: Any):
        self.http = HttpClient(base_url, service="audiobookshelf", api_key=None, **kwargs)
        self.http_token = token

    def _request(self, path: str, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {self.http_token}"
        return self.http.request(path, headers=headers, **kwargs)

    def search(self, library_id: str, query: str, limit: int = 12) -> Any:
        return self._request(f"/api/libraries/{urllib.parse.quote(library_id, safe='')}/search", params={"q": query, "limit": limit})

    def list_items(self, library_id: str, *, page: int = 0, limit: int = 100, **filters: Any) -> Any:
        params = {"page": page, "limit": limit, **filters}
        return self._request(f"/api/libraries/{urllib.parse.quote(library_id, safe='')}/items", params=params)

    def get_item(self, item_id: str, *, expanded: bool = True) -> Any:
        return self._request(
            f"/api/items/{urllib.parse.quote(item_id, safe='')}",
            params={"expanded": 1 if expanded else 0},
        )

    def update_media(self, item_id: str, media: Mapping[str, Any]) -> Any:
        return self._request(f"/api/items/{urllib.parse.quote(item_id, safe='')}/media", method="PATCH", json_body=dict(media))

    def match(self, item_id: str, *, title: str | None = None, author: str | None = None, provider: str | None = None, isbn: str | None = None, asin: str | None = None, override_defaults: bool = False) -> Any:
        payload = {"overrideDefaults": override_defaults}
        for key, value in (("title", title), ("author", author), ("provider", provider), ("isbn", isbn), ("asin", asin)):
            if value:
                payload[key] = value
        return self._request(f"/api/items/{urllib.parse.quote(item_id, safe='')}/match", method="POST", json_body=payload)

    def scan(self, library_id: str, *, force: bool = False) -> Any:
        return self._request(f"/api/libraries/{urllib.parse.quote(library_id, safe='')}/scan", method="POST", params={"force": 1 if force else 0})


class ProwlarrClient:
    def __init__(self, base_url: str, api_key: str, **kwargs: Any):
        self.http = HttpClient(base_url, service="prowlarr", api_key=api_key, **kwargs)

    def search(self, query: str, *, search_type: str | None = None, indexer_ids: list[int] | None = None, categories: list[int] | None = None, limit: int | None = None, offset: int | None = None) -> Any:
        params: dict[str, Any] = {"query": query}
        if search_type:
            params["type"] = search_type
        if indexer_ids:
            params["indexerIds"] = indexer_ids
        if categories:
            params["categories"] = categories
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        return self.http.request("/api/v1/search", params=params)

    def grab(self, release: Mapping[str, Any]) -> Any:
        return self.http.request("/api/v1/search", method="POST", json_body=dict(release))


class QBittorrentClient:
    def __init__(self, base_url: str, *, username: str | None = None, password: str | None = None, api_key: str | None = None, **kwargs: Any):
        self.http = HttpClient(base_url, service="qbittorrent", api_key=api_key, **kwargs)
        self.username = username
        self.password = password

    def login(self) -> Any:
        if self.http.api_key:
            return "api-key"
        if self.username is None or self.password is None:
            raise ValueError("qBittorrent username and password are required")
        return self.http.request("/api/v2/auth/login", method="POST", form={"username": self.username, "password": self.password})

    def version(self) -> Any:
        return self.http.request("/api/v2/app/version")

    def torrents(self, **filters: Any) -> Any:
        return self.http.request("/api/v2/torrents/info", params=filters)

    def add_urls(self, urls: list[str], *, category: str, save_path: str | None = None, paused: bool = False) -> Any:
        form: dict[str, Any] = {"urls": "\n".join(urls), "category": category, "paused": "true" if paused else "false"}
        if save_path:
            form["savepath"] = save_path
        return self.http.request("/api/v2/torrents/add", method="POST", form=form)

    def pause(self, hashes: str = "all") -> Any:
        return self.http.request("/api/v2/torrents/pause", method="POST", form={"hashes": hashes})

    def resume(self, hashes: str = "all") -> Any:
        return self.http.request("/api/v2/torrents/resume", method="POST", form={"hashes": hashes})

    def delete(self, hashes: str, *, delete_files: bool = False) -> Any:
        return self.http.request("/api/v2/torrents/delete", method="POST", form={"hashes": hashes, "deleteFiles": "true" if delete_files else "false"})

    def properties(self, torrent_hash: str) -> Any:
        return self.http.request("/api/v2/torrents/properties", params={"hash": torrent_hash})

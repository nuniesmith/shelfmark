"""Small, retrying clients for the existing media-service APIs.

The clients deliberately return the upstream JSON shape.  Normalization belongs
in the job layer because release and library metadata evolve independently of
the transport protocol.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Any, Mapping


class ServiceError(RuntimeError):
    """An upstream request failed after retrying."""

    def __init__(self, service: str, message: str, status: int | None = None):
        self.service = service
        self.status = status
        self.message = message
        super().__init__(f"{service}: {message}")


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

        last_error: ServiceError | None = None
        for attempt in range(self.retries + 1):
            try:
                with self.opener.open(req, timeout=self.timeout) as response:
                    raw = response.read()
                    return self._decode(raw, response.headers.get_content_type())
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
                    raise last_error from exc
                retry_after = exc.headers.get("Retry-After")
                self._sleep(attempt, retry_after)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = ServiceError(self.service, str(exc))
                if attempt >= self.retries:
                    raise last_error from exc
                self._sleep(attempt, None)
        raise last_error or ServiceError(self.service, "request failed")

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

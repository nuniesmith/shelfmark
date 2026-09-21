"""Read a .torrent well enough to identify it.

Only two facts are needed: the infohash, which is the ONLY reliable identity
of a torrent, and the display name, which is what a person recognises.

The infohash matters because qBittorrent deduplicates on it and says nothing
when it does. Adding a release that is already in the client returns the same
`Ok.` as adding a new one, so without computing the hash ourselves there is
no way to tell "downloading" from "you already have this" from "refused" —
and all three used to render as "Queued release <uuid>".

Deliberately not a torrent library: bencode is small, and this parses the
subset a .torrent uses rather than taking a dependency for two fields.
"""

from __future__ import annotations

import hashlib
from typing import Any


class InvalidTorrent(Exception):
    """The bytes are not a .torrent — usually an HTML error page."""


def _decode(data: bytes, index: int) -> tuple[Any, int]:
    kind = data[index : index + 1]
    if kind == b"d":
        index += 1
        out: dict[bytes, Any] = {}
        while data[index : index + 1] != b"e":
            key, index = _decode(data, index)
            value, index = _decode(data, index)
            out[key] = value
        return out, index + 1
    if kind == b"l":
        index += 1
        items = []
        while data[index : index + 1] != b"e":
            value, index = _decode(data, index)
            items.append(value)
        return items, index + 1
    if kind == b"i":
        end = data.index(b"e", index)
        return int(data[index + 1 : end]), end + 1
    colon = data.index(b":", index)
    length = int(data[index:colon])
    start = colon + 1
    return data[start : start + length], start + length


def _encode(value: Any) -> bytes:
    if isinstance(value, dict):
        # Keys MUST be re-emitted in sorted order — that is what the spec
        # requires and what every client hashed when it computed its own id.
        return b"d" + b"".join(_encode(k) + _encode(v) for k, v in sorted(value.items())) + b"e"
    if isinstance(value, list):
        return b"l" + b"".join(_encode(v) for v in value) + b"e"
    if isinstance(value, bool):  # bool before int: bool IS an int in Python
        raise InvalidTorrent("unexpected boolean in torrent metadata")
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    raise InvalidTorrent(f"cannot encode {type(value).__name__}")


def parse(raw: bytes) -> tuple[str, str]:
    """`(infohash, name)` for a .torrent. Raises `InvalidTorrent` otherwise.

    A tracker that has rate-limited us, or an expired link, answers with an
    HTML page and HTTP 200 — so "it downloaded" is not the same as "it is a
    torrent", and that has to fail loudly here rather than become a
    mysterious add that does nothing.
    """
    if not raw:
        raise InvalidTorrent("empty response")
    try:
        meta, _ = _decode(raw, 0)
    except (ValueError, IndexError) as exc:
        raise InvalidTorrent("not bencoded — probably an error page") from exc
    if not isinstance(meta, dict) or b"info" not in meta:
        raise InvalidTorrent("no info dictionary")
    info = meta[b"info"]
    if not isinstance(info, dict):
        raise InvalidTorrent("info is not a dictionary")
    infohash = hashlib.sha1(_encode(info)).hexdigest()
    name = info.get(b"name", b"")
    return infohash, name.decode("utf-8", errors="replace") if isinstance(name, bytes) else ""

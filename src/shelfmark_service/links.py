"""Signed, expiring download links for ebooks too big to attach in Discord.

Discord caps a bot's attachments at 10 MB on an unboosted server, and plenty of
real ebooks -- illustrated epubs, scanned PDFs, comics -- are bigger than that.
For those the bot posts a link instead, which the API serves at /dl/<token>.

The token IS the credential. Whoever holds it can fetch that one book until it
expires, and nothing else: it names a single ebook id and an expiry, signed
with HMAC-SHA256. The key is derived from SHELFMARK_API_TOKEN rather than being
a second secret, so there is nothing new to provision or leak, and rotating the
API token revokes every link still outstanding. With no API token configured
there is no key at all, and links are refused rather than signed with
something guessable.

    v1.<ebook id>.<expiry, unix seconds>.<base64url signature>
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import time

_VERSION = "v1"
# A key made for this one purpose: a token signed with the API token itself
# must not verify, and neither may anything else that token ever signs.
_KEY_CONTEXT = b"shelfmark ebook download links v1"
_EBOOK_ID = re.compile(r"[0-9a-f]{24}")
_EXPIRY = re.compile(r"[0-9]{1,12}")


class LinkError(Exception):
    """The token is malformed, or its signature does not match."""


class LinkExpired(LinkError):
    """A genuine token whose time is up -- worth telling apart from a forged
    one, because the person holding it can simply ask for a fresh link."""


def _key(api_token: str) -> bytes:
    return hmac.new(api_token.encode("utf-8"), _KEY_CONTEXT, hashlib.sha256).digest()


def _signature(key: bytes, message: str) -> str:
    digest = hmac.new(key, message.encode("ascii"), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def sign(api_token: str, ebook_id: str, expires_at: int) -> str:
    """A token granting `ebook_id` until `expires_at` (unix seconds)."""
    if not api_token:
        raise ValueError("download links need SHELFMARK_API_TOKEN to sign with")
    if not _EBOOK_ID.fullmatch(ebook_id):
        raise ValueError(f"not an ebook id: {ebook_id!r}")
    message = f"{_VERSION}.{ebook_id}.{int(expires_at)}"
    return f"{message}.{_signature(_key(api_token), message)}"


def verify(api_token: str, token: str, *, now: float | None = None) -> str:
    """The ebook id `token` grants, or LinkError / LinkExpired.

    The signature is checked before the expiry, so a forged token learns
    nothing -- not even whether the expiry it claims has passed.
    """
    if not api_token:
        raise LinkError("download links are not configured")
    parts = token.split(".")
    if len(parts) != 4:
        raise LinkError("malformed token")
    version, ebook_id, expires, signature = parts
    if version != _VERSION or not _EBOOK_ID.fullmatch(ebook_id) or not _EXPIRY.fullmatch(expires):
        raise LinkError("malformed token")
    expected = _signature(_key(api_token), f"{version}.{ebook_id}.{expires}")
    # Bytes, not str: compare_digest refuses a str holding non-ASCII, and a
    # hostile token is free to contain some.
    if not hmac.compare_digest(signature.encode("utf-8"), expected.encode("ascii")):
        raise LinkError("bad signature")
    if int(expires) <= (time.time() if now is None else now):
        raise LinkExpired("link expired")
    return ebook_id

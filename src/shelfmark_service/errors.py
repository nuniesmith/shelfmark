"""Stable error codes for job failures.

Before this module, `worker.execute()` let every failure escape as a raw
exception and `Worker.run_once()` stored `str(exc)` on the job row. A transfer
failing because Sullivan was unconfigured, because the remote path was
rejected, and because a checksum mismatch was found after copying are three
completely different situations — one needs a secret set, one is a bad
request, one means the data on disk is corrupt — but all three arrived at
`GET /api/v1/jobs/{id}` (and the Discord `/job` command) as the same kind of
indistinguishable sentence. Nothing downstream could branch on WHAT went
wrong, and the sentence changed whenever anyone reworded a message.

Codes are part of the API contract once shipped: a value here must never be
renamed or removed, only added to. Job rows and Discord replies key off the
string value directly, so treat `ErrorCode` members as append-only.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class ErrorCode(str, Enum):
    """One member per failure situation `worker.execute()` can actually produce.

    Every member below is reachable from a real `raise` site in `worker.py` —
    see the comment at each site for which one and why. `INTERNAL` is the
    deliberate exception: it is not tied to one call site, it is the fallback
    for whatever `execute()` did not anticipate, so an unmapped bug reports a
    stable "something unexpected broke" code instead of silently taking on
    whichever real code happens to be highest on the stack.
    """

    # A job needs Audiobookshelf/Prowlarr/Sullivan credentials or a URL that
    # `Settings` does not have. Fixed by setting the missing environment
    # value, not by retrying the job or changing its payload.
    PROVIDER_NOT_CONFIGURED = "provider_not_configured"

    # The job payload itself is unusable: a required field is missing, a path
    # falls outside the root it is required to stay under, or the job kind is
    # one the worker does not implement. Retrying the identical payload will
    # fail identically; the caller has to send a different request.
    INVALID_PAYLOAD = "invalid_payload"

    # The worker looked for content that should already be present — an
    # organizer source directory, or a Sullivan transfer that produced no
    # files, or one that never settled into a stable state — and found
    # nothing usable. Distinct from `provider_not_configured` because the
    # integration itself is reachable; the content just is not there.
    SOURCE_MISSING = "source_missing"

    # `apply_extracts()` in main.py reported one or more archives it could not
    # extract (a corrupt archive, an unsafe member, a missing extractor
    # binary). The source archive is left in place by design — see
    # `apply_extracts` in main.py — so this is recoverable without data loss.
    EXTRACTION_FAILED = "extraction_failed"

    # A transfer's post-copy content comparison (`RsyncTransfer.verify`) found
    # files that differ from Sullivan's copy. This means the data is
    # suspect, not that the network or credentials are bad — see the comment
    # on `RsyncTransfer.verify` for why that check has to be a byte-for-byte
    # pass rather than trusting rsync's exit status.
    VERIFICATION_FAILED = "verification_failed"

    # A remote service Shelfmark depends on (Audiobookshelf, Prowlarr, or the
    # SSH/rsync path to Sullivan) is reachable in configuration but the actual
    # request or transfer failed — a `ServiceError` from clients.py, or a
    # `TransferError` from an rsync pull/verify. Usually transient and worth
    # retrying once the upstream recovers.
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"

    # The job was cancelled through `/api/v1/jobs/{id}/cancel` (or the
    # `JobCancelled` raised mid-apply in `worker.execute()` after cancellation
    # was observed). Not a failure in the usual sense — the job's `status` is
    # already `cancelled` — but it gets a code for the same reason a failure
    # does: so a caller can tell "the user stopped this" apart from "this
    # broke" without parsing a message string.
    CANCELLED = "cancelled"

    # Anything `execute()` raised that was not translated into one of the
    # codes above: an `OSError` from a disk write, an import failure, a bug.
    # `Worker.run_once()` guarantees every exception ends up with SOME code,
    # and this is the one that means "read the log, this was not anticipated"
    # rather than a specific, expected condition.
    INTERNAL = "internal"


class ShelfmarkError(Exception):
    """A job failure carrying a stable code alongside its human message.

    `message` is free text for a person (logs, Discord, the manifest event).
    `code` is what a caller can safely branch on, because — unlike the
    message — it is guaranteed not to change when someone rewords a sentence.

    `details` is optional structured context for logs/manifests. It must
    never hold a secret: `clients.py` handles Audiobookshelf/Prowlarr
    tokens, and a `ServiceError` raised from there carries the upstream
    response body, which api.py's `_upstream_error` already treats as unsafe
    to surface ("can contain release URLs, credentials, or other data").
    Callers converting a `ServiceError` here must follow the same rule and
    pass only the service name and HTTP status, never `exc.message`.
    """

    def __init__(self, code: ErrorCode, message: str, details: dict[str, Any] | None = None):
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)

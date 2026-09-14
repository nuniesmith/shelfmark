from __future__ import annotations

import unittest

from src.shelfmark_service.api import _job_response
from src.shelfmark_service.db import Job


def _job(**overrides: object) -> Job:
    fields: dict[str, object] = dict(
        id="job-1",
        kind="organize_apply",
        payload={},
        status="failed",
        attempts=1,
        created_at="2026-01-01T00:00:00+00:00",
        started_at=None,
        finished_at=None,
        heartbeat_at=None,
        worker_id="worker-a",
        error="source is not a directory: /incoming/x",
        error_code="source_missing",
        result=None,
        cancel_requested=False,
    )
    fields.update(overrides)
    return Job(**fields)  # type: ignore[arg-type]


class JobResponseTests(unittest.TestCase):
    """`GET /api/v1/jobs/{id}` has to carry the code alongside the message —
    the whole point of storing one is that a caller can branch on it instead
    of parsing `error`. `_job_response` is where the DB row becomes the API
    shape, so this is the one place that mapping can silently go missing."""

    def test_response_exposes_the_stable_code_under_a_short_key(self) -> None:
        response = _job_response(_job())
        self.assertEqual(response["code"], "source_missing")
        self.assertEqual(response["error"], "source is not a directory: /incoming/x")

    def test_a_pre_migration_row_exposes_a_null_code_not_a_crash(self) -> None:
        response = _job_response(_job(error_code=None))
        self.assertIsNone(response["code"])


from unittest import mock

from src.shelfmark_service import api as api_module


class FakeProwlarr:
    """Stands in for ProwlarrClient so these tests never touch the network —
    they exist to prove which categories reach the client, not to exercise
    HTTP transport (that's clients.py's job, and clients.py is off limits
    for this change)."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def search(self, query, *, search_type=None, categories=None, limit=None, offset=None):
        self.calls.append({"query": query, "categories": categories})
        return []


class BookOnlyCategoryTests(unittest.TestCase):
    """The one configured indexer advertises 7000/7010/7030/7050, not 7020 —
    `/ebook-request` asks for book_only=true rather than naming 7020 directly,
    and the route is what is supposed to translate that into the configured
    PROWLARR_BOOK_CATEGORIES default. This is the exact gap the task named:
    ProwlarrClient.search() already accepted `categories`, but the route
    never passed anything through.
    """

    def test_book_only_applies_the_configured_book_categories(self) -> None:
        fake = FakeProwlarr()
        with mock.patch.object(api_module, "_prowlarr_client", return_value=fake):
            api_module.release_search(
                q="dune", search_type=None, categories=None, book_only=True,
                limit=50, offset=0, _actor="test",
            )
        self.assertEqual(fake.calls[0]["categories"], list(api_module.settings.prowlarr_book_categories))

    def test_book_only_false_leaves_categories_unset(self) -> None:
        """Must not change /release-search's existing (audiobook-inclusive)
        behavior for the command that doesn't ask for books specifically."""
        fake = FakeProwlarr()
        with mock.patch.object(api_module, "_prowlarr_client", return_value=fake):
            api_module.release_search(
                q="dune", search_type=None, categories=None, book_only=False,
                limit=50, offset=0, _actor="test",
            )
        self.assertIsNone(fake.calls[0]["categories"])

    def test_explicit_categories_override_book_only(self) -> None:
        fake = FakeProwlarr()
        with mock.patch.object(api_module, "_prowlarr_client", return_value=fake):
            api_module.release_search(
                q="dune", search_type=None, categories=[7060], book_only=True,
                limit=50, offset=0, _actor="test",
            )
        self.assertEqual(fake.calls[0]["categories"], [7060])


if __name__ == "__main__":
    unittest.main()

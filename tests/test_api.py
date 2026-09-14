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


if __name__ == "__main__":
    unittest.main()

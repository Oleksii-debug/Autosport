import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from autosport.ingestion_health import SourceHealthStore


class NegativeIngestionEvidenceContinuityTests(unittest.TestCase):
    def _store(self, root: str) -> SourceHealthStore:
        return SourceHealthStore(Path(root) / "source-health.json")

    def test_failed_transition_survives_reopen_and_later_recovery(self):
        failed_at = "2026-09-21T08:00:00+00:00"
        recovered_at = "2026-09-21T08:00:01+00:00"

        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp).record_failure(
                "provider-a",
                now=failed_at,
                error=TimeoutError("upstream unavailable"),
            )

            reopened = self._store(tmp)
            failed = reopened.get("provider-a")
            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.poll_count, 1)
            self.assertEqual(failed.total_failures, 1)
            self.assertEqual(failed.consecutive_failures, 1)
            self.assertEqual(failed.last_error_at, failed_at)
            self.assertEqual(failed.last_error, "TimeoutError: upstream unavailable")

            reopened.record_success(
                "provider-a",
                now=recovered_at,
                received=0,
                accepted=0,
                rejected=0,
                cursor="after-recovery",
                latest_source_ts=None,
                quality_flags=(),
            )

            after_second_reopen = self._store(tmp)
            latest = after_second_reopen.get("provider-a")
            self.assertEqual(latest.status, "healthy")
            self.assertEqual(latest.poll_count, 2)
            self.assertEqual(latest.total_failures, 1)
            self.assertEqual(latest.consecutive_failures, 0)
            self.assertIsNone(latest.last_error)
            self.assertEqual(latest.last_error_at, failed_at)

            historical = after_second_reopen.get_as_of(
                "provider-a",
                as_of=datetime(2026, 9, 21, 8, 0, 0, tzinfo=timezone.utc),
            )
            self.assertEqual(historical.status, "failed")
            self.assertEqual(historical.poll_count, 1)
            self.assertEqual(historical.total_failures, 1)
            self.assertEqual(historical.last_error, "TimeoutError: upstream unavailable")

    def test_rejected_ingestion_evidence_survives_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp).record_success(
                "provider-b",
                now="2026-09-21T08:01:00+00:00",
                received=2,
                accepted=1,
                rejected=1,
                cursor="cursor-rejected",
                latest_source_ts="2026-09-21T08:00:59+00:00",
                quality_flags=("INVALID_QUOTE",),
            )

            reopened = self._store(tmp).get("provider-b")
            self.assertEqual(reopened.status, "degraded")
            self.assertEqual(reopened.poll_count, 1)
            self.assertEqual(reopened.total_received, 2)
            self.assertEqual(reopened.total_accepted, 1)
            self.assertEqual(reopened.total_rejected, 1)
            self.assertEqual(reopened.quality_flags, ("INVALID_QUOTE",))
            self.assertEqual(reopened.last_cursor, "cursor-rejected")

    def test_empty_successful_poll_remains_observable_after_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._store(tmp).record_success(
                "provider-c",
                now="2026-09-21T08:02:00+00:00",
                received=0,
                accepted=0,
                rejected=0,
                cursor="empty-poll-17",
                latest_source_ts=None,
                quality_flags=(),
            )

            reopened = self._store(tmp).get("provider-c")
            self.assertEqual(reopened.status, "healthy")
            self.assertEqual(reopened.poll_count, 1)
            self.assertEqual(reopened.total_received, 0)
            self.assertEqual(reopened.total_accepted, 0)
            self.assertEqual(reopened.total_rejected, 0)
            self.assertEqual(reopened.total_failures, 0)
            self.assertEqual(reopened.last_cursor, "empty-poll-17")
            self.assertEqual(reopened.last_success_at, "2026-09-21T08:02:00+00:00")


if __name__ == "__main__":
    unittest.main()

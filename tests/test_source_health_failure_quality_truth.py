import tempfile
import unittest
from pathlib import Path

from autosport.ingestion_health import SourceHealthStore


class SourceHealthFailureQualityTruthTests(unittest.TestCase):
    def test_failure_clears_previous_batch_quality_flags_but_preserves_success_high_water(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source-health.json"
            store = SourceHealthStore(path)

            degraded = store.record_success(
                "source",
                now="2026-09-14T08:00:00+00:00",
                received=2,
                accepted=1,
                rejected=1,
                cursor="cursor-17",
                latest_source_ts="2026-09-14T07:59:58+00:00",
                quality_flags=("PROVIDER_SEQUENCE_GAP",),
            )
            self.assertEqual(degraded.status, "degraded")
            self.assertEqual(degraded.quality_flags, ("PROVIDER_SEQUENCE_GAP",))

            failed = store.record_failure(
                "source",
                now="2026-09-14T08:01:00+00:00",
                error=RuntimeError("provider unavailable"),
            )

            self.assertEqual(failed.status, "failed")
            self.assertEqual(failed.quality_flags, ())
            self.assertEqual(failed.poll_count, 2)
            self.assertEqual(failed.total_received, 2)
            self.assertEqual(failed.total_accepted, 1)
            self.assertEqual(failed.total_rejected, 1)
            self.assertEqual(failed.total_failures, 1)
            self.assertEqual(failed.consecutive_failures, 1)
            self.assertEqual(failed.last_cursor, "cursor-17")
            self.assertEqual(failed.latest_source_ts, "2026-09-14T07:59:58+00:00")
            self.assertEqual(failed.last_error, "RuntimeError: provider unavailable")

            reopened = SourceHealthStore(path).get("source")
            self.assertEqual(reopened, failed)
            self.assertEqual(reopened.quality_flags, ())

    def test_repeated_failures_do_not_resurrect_previous_batch_quality_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source-health.json"
            store = SourceHealthStore(path)
            store.record_success(
                "source",
                now="2026-09-14T08:00:00+00:00",
                received=1,
                accepted=1,
                rejected=0,
                cursor="cursor-1",
                latest_source_ts="2026-09-14T07:59:59+00:00",
                quality_flags=("STALE_SOURCE",),
            )

            store.record_failure(
                "source",
                now="2026-09-14T08:01:00+00:00",
                error=TimeoutError("first timeout"),
            )
            second = store.record_failure(
                "source",
                now="2026-09-14T08:02:00+00:00",
                error=TimeoutError("second timeout"),
            )

            self.assertEqual(second.status, "failed")
            self.assertEqual(second.quality_flags, ())
            self.assertEqual(second.poll_count, 3)
            self.assertEqual(second.total_failures, 2)
            self.assertEqual(second.consecutive_failures, 2)
            self.assertEqual(second.last_cursor, "cursor-1")
            self.assertEqual(second.latest_source_ts, "2026-09-14T07:59:59+00:00")


if __name__ == "__main__":
    unittest.main()

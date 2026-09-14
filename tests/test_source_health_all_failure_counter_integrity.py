import json
import tempfile
import unittest
from pathlib import Path

from autosport.ingestion_health import SourceHealthStore


class SourceHealthAllFailureCounterIntegrityTests(unittest.TestCase):
    @staticmethod
    def _all_failure_state(*, consecutive_failures: int) -> dict:
        return {
            "source_id": "source",
            "status": "failed",
            "poll_count": 2,
            "total_received": 0,
            "total_accepted": 0,
            "total_rejected": 0,
            "total_failures": 2,
            "consecutive_failures": consecutive_failures,
            "last_success_at": None,
            "last_error_at": "2026-09-14T00:02:00+00:00",
            "last_error": "RuntimeError: provider unavailable",
            "last_cursor": None,
            "latest_source_ts": None,
            "quality_flags": [],
        }

    def test_reopen_rejects_all_failure_counter_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source-health.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "sources": {
                            "source": self._all_failure_state(consecutive_failures=1)
                        },
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "invalid source health state"):
                SourceHealthStore(path)

    def test_two_canonical_failures_reopen_with_full_consecutive_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source-health.json"
            store = SourceHealthStore(path)
            store.record_failure(
                "source",
                now="2026-09-14T00:01:00+00:00",
                error=RuntimeError("provider unavailable"),
            )
            store.record_failure(
                "source",
                now="2026-09-14T00:02:00+00:00",
                error=RuntimeError("provider still unavailable"),
            )

            reopened = SourceHealthStore(path).get("source")
            self.assertEqual(reopened.poll_count, 2)
            self.assertEqual(reopened.total_failures, 2)
            self.assertEqual(reopened.consecutive_failures, 2)
            self.assertEqual(reopened.status, "failed")


if __name__ == "__main__":
    unittest.main()

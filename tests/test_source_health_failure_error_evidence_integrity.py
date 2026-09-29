import json
import tempfile
import unittest
from pathlib import Path

from autosport.ingestion_health import SourceHealthStore


class SourceHealthFailureErrorEvidenceIntegrityTests(unittest.TestCase):
    @staticmethod
    def _first_poll_failed_state() -> dict:
        return {
            "source_id": "source",
            "status": "failed",
            "poll_count": 1,
            "total_received": 0,
            "total_accepted": 0,
            "total_rejected": 0,
            "total_failures": 1,
            "consecutive_failures": 1,
            "last_success_at": None,
            "last_error_at": "2026-09-14T00:01:00+00:00",
            "last_error": "RuntimeError: provider unavailable",
            "last_cursor": None,
            "latest_source_ts": None,
            "quality_flags": [],
        }

    @staticmethod
    def _write_state(path: Path, state: dict) -> None:
        path.write_text(
            json.dumps(
                {"schema_version": 1, "sources": {"source": state}},
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def test_failed_state_rejects_empty_or_whitespace_only_error_evidence(self) -> None:
        for last_error in ("", "   "):
            with self.subTest(last_error=last_error):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "source-health.json"
                    state = self._first_poll_failed_state()
                    state["last_error"] = last_error
                    self._write_state(path, state)

                    with self.assertRaisesRegex(ValueError, "invalid source health state"):
                        SourceHealthStore(path)

    def test_failed_state_accepts_canonical_nonempty_error_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source-health.json"
            state = self._first_poll_failed_state()
            state["last_error"] = "RuntimeError: "
            self._write_state(path, state)

            reopened = SourceHealthStore(path).get("source")

            self.assertEqual(reopened.status, "failed")
            self.assertEqual(reopened.last_error, "RuntimeError: ")


if __name__ == "__main__":
    unittest.main()

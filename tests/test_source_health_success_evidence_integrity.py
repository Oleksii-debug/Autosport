import json
import tempfile
import unittest
from pathlib import Path

from autosport.ingestion_health import SourceHealthStore


class SourceHealthSuccessEvidenceIntegrityTests(unittest.TestCase):
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

    def test_first_failed_poll_cannot_forge_success_only_evidence(self) -> None:
        cases = (
            ("last_cursor", "forged-cursor"),
            ("latest_source_ts", "2026-09-14T00:00:00+00:00"),
        )
        for field_name, value in cases:
            with self.subTest(field_name=field_name):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "source-health.json"
                    state = self._first_poll_failed_state()
                    state[field_name] = value
                    self._write_state(path, state)

                    with self.assertRaisesRegex(ValueError, "invalid source health state"):
                        SourceHealthStore(path)

    def test_canonical_first_failed_poll_remains_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source-health.json"
            self._write_state(path, self._first_poll_failed_state())

            state = SourceHealthStore(path).get("source")

            self.assertEqual(state.status, "failed")
            self.assertEqual(state.poll_count, 1)
            self.assertEqual(state.total_failures, 1)
            self.assertIsNone(state.last_cursor)
            self.assertIsNone(state.latest_source_ts)


if __name__ == "__main__":
    unittest.main()

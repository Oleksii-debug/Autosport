import json
import tempfile
import unittest
from pathlib import Path

from autosport.ingestion_health import SourceHealthStore


class SourceHealthStateIntegrityTests(unittest.TestCase):
    @staticmethod
    def _valid_state() -> dict:
        return {
            "source_id": "source",
            "status": "degraded",
            "poll_count": 2,
            "total_received": 3,
            "total_accepted": 1,
            "total_rejected": 1,
            "total_failures": 1,
            "consecutive_failures": 0,
            "last_success_at": "2026-09-14T00:00:00+00:00",
            "last_error_at": "2026-09-13T23:59:00+00:00",
            "last_error": None,
            "last_cursor": "cursor-2",
            "latest_source_ts": "2026-09-13T23:58:00+00:00",
            "quality_flags": ["PROVIDER_SEQUENCE_GAP"],
        }

    def _write_state(self, path: Path, state: dict) -> None:
        path.write_text(
            json.dumps(
                {"schema_version": 1, "sources": {"source": state}},
                sort_keys=True,
            ),
            encoding="utf-8",
        )

    def test_valid_persisted_state_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source-health.json"
            self._write_state(path, self._valid_state())

            state = SourceHealthStore(path).get("source")

            self.assertEqual(state.source_id, "source")
            self.assertEqual(state.poll_count, 2)
            self.assertEqual(state.quality_flags, ("PROVIDER_SEQUENCE_GAP",))

    def test_existing_corrupt_state_fails_during_store_initialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source-health.json"
            state = self._valid_state()
            state["quality_flags"] = "PROVIDER_SEQUENCE_GAP"
            self._write_state(path, state)

            with self.assertRaisesRegex(ValueError, "invalid source health state"):
                SourceHealthStore(path)

    def test_persisted_source_identity_must_match_map_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source-health.json"
            state = self._valid_state()
            state["source_id"] = "other-source"
            self._write_state(path, state)

            with self.assertRaisesRegex(ValueError, "invalid source health state"):
                SourceHealthStore(path)

    def test_malformed_counters_and_cross_field_totals_fail_closed(self):
        cases = (
            ("poll_count", True),
            ("total_received", -1),
            ("total_accepted", 4),
            ("consecutive_failures", 2),
            ("total_failures", 3),
        )
        for field_name, value in cases:
            with self.subTest(field_name=field_name, value=value):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "source-health.json"
                    state = self._valid_state()
                    state[field_name] = value
                    self._write_state(path, state)

                    with self.assertRaisesRegex(ValueError, "invalid source health state"):
                        SourceHealthStore(path)

    def test_impossible_status_state_machine_combinations_fail_closed(self):
        cases = (
            {"status": "failed", "consecutive_failures": 0, "last_error": "RuntimeError: boom"},
            {"status": "healthy", "consecutive_failures": 1, "quality_flags": []},
            {"status": "healthy", "quality_flags": ["GAP"]},
            {"status": "degraded", "quality_flags": []},
            {"status": "failed", "consecutive_failures": 1, "last_error": None},
            {"status": "failed", "consecutive_failures": 1, "last_error_at": None, "last_error": "RuntimeError: boom"},
            {"status": "unknown", "poll_count": 1, "quality_flags": []},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "source-health.json"
                    state = self._valid_state()
                    state.update(changes)
                    self._write_state(path, state)

                    with self.assertRaisesRegex(ValueError, "invalid source health state"):
                        SourceHealthStore(path)

    def test_poll_history_evidence_must_match_success_and_failure_counts(self):
        cases = (
            {
                "poll_count": 1,
                "total_failures": 1,
                "consecutive_failures": 1,
                "status": "failed",
                "last_success_at": "2026-09-14T00:00:00+00:00",
                "last_error": "RuntimeError: boom",
                "total_received": 0,
                "total_accepted": 0,
                "total_rejected": 0,
                "quality_flags": [],
            },
            {
                "poll_count": 1,
                "total_failures": 1,
                "consecutive_failures": 1,
                "status": "failed",
                "last_success_at": None,
                "last_error": "RuntimeError: boom",
                "total_received": 1,
                "total_accepted": 1,
                "total_rejected": 0,
                "quality_flags": [],
            },
            {
                "poll_count": 1,
                "total_failures": 1,
                "consecutive_failures": 1,
                "status": "failed",
                "last_success_at": None,
                "last_error": "RuntimeError: boom",
                "total_received": 0,
                "total_accepted": 0,
                "total_rejected": 0,
                "quality_flags": ["GAP"],
            },
            {
                "poll_count": 1,
                "total_failures": 0,
                "consecutive_failures": 0,
                "status": "healthy",
                "last_error_at": "2026-09-13T23:59:00+00:00",
                "quality_flags": [],
            },
            {"last_success_at": None},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "source-health.json"
                    state = self._valid_state()
                    state.update(changes)
                    self._write_state(path, state)

                    with self.assertRaisesRegex(ValueError, "invalid source health state"):
                        SourceHealthStore(path)

    def test_canonical_failed_states_are_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source-health.json"
            after_success = self._valid_state()
            after_success.update(
                {
                    "status": "failed",
                    "poll_count": 3,
                    "total_failures": 2,
                    "consecutive_failures": 1,
                    "last_error_at": "2026-09-14T00:01:00+00:00",
                    "last_error": "RuntimeError: provider unavailable",
                }
            )
            self._write_state(path, after_success)
            reopened = SourceHealthStore(path).get("source")
            self.assertEqual(reopened.status, "failed")
            self.assertEqual(reopened.consecutive_failures, 1)
            self.assertEqual(reopened.total_failures, 2)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source-health.json"
            first_poll_failed = self._valid_state()
            first_poll_failed.update(
                {
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
            )
            self._write_state(path, first_poll_failed)
            reopened = SourceHealthStore(path).get("source")
            self.assertEqual(reopened.status, "failed")
            self.assertEqual(reopened.poll_count, 1)
            self.assertEqual(reopened.total_failures, 1)

    def test_invalid_persisted_status_and_timestamps_fail_closed(self):
        cases = (
            ("status", "maybe"),
            ("last_success_at", "not-a-timestamp"),
            ("last_error_at", 123),
            ("latest_source_ts", "2026-09-14T00:00:00"),
        )
        for field_name, value in cases:
            with self.subTest(field_name=field_name, value=value):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "source-health.json"
                    state = self._valid_state()
                    state[field_name] = value
                    self._write_state(path, state)

                    with self.assertRaisesRegex(ValueError, "invalid source health state"):
                        SourceHealthStore(path)

    def test_schema_drift_in_persisted_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source-health.json"
            missing = self._valid_state()
            missing.pop("last_cursor")
            self._write_state(path, missing)
            with self.assertRaisesRegex(ValueError, "invalid source health state"):
                SourceHealthStore(path)

            extra = self._valid_state()
            extra["unexpected"] = "value"
            self._write_state(path, extra)
            with self.assertRaisesRegex(ValueError, "invalid source health state"):
                SourceHealthStore(path)

    def test_schema_version_requires_exact_non_boolean_integer(self):
        for version in (True, 1.0, "1"):
            with self.subTest(version=version):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "source-health.json"
                    path.write_text(
                        json.dumps({"schema_version": version, "sources": {}}),
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(ValueError, "invalid source health store"):
                        SourceHealthStore(path)

    def test_duplicate_and_nonfinite_json_evidence_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source-health.json"
            path.write_text(
                '{"schema_version":1,"schema_version":1,"sources":{}}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "invalid source health store"):
                SourceHealthStore(path)

            state = self._valid_state()
            state["total_received"] = float("nan")
            self._write_state(path, state)
            with self.assertRaisesRegex(ValueError, "invalid source health store"):
                SourceHealthStore(path)

    def test_runtime_record_success_rejects_ambiguous_inputs_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source-health.json"
            store = SourceHealthStore(path)
            baseline = path.read_bytes()

            with self.assertRaisesRegex(ValueError, "non-negative integer"):
                store.record_success(
                    "source",
                    now="2026-09-14T00:00:00+00:00",
                    received=1,
                    accepted=True,
                    rejected=0,
                    cursor=None,
                    latest_source_ts=None,
                    quality_flags=(),
                )
            self.assertEqual(path.read_bytes(), baseline)

            with self.assertRaisesRegex(ValueError, "duplicates"):
                store.record_success(
                    "source",
                    now="2026-09-14T00:00:00+00:00",
                    received=1,
                    accepted=1,
                    rejected=0,
                    cursor=None,
                    latest_source_ts=None,
                    quality_flags=("GAP", "GAP"),
                )
            self.assertEqual(path.read_bytes(), baseline)


if __name__ == "__main__":
    unittest.main()

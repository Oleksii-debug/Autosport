import json
import tempfile
import unittest
from pathlib import Path

from autosport.ingestion_negative_evidence import project_ingestion_negative_evidence


def _status(**overrides):
    payload = {
        "schema_version": 1,
        "kind": "autosport_continuous_local_observation",
        "run_id": "run-1",
        "source_id": "provider-a",
        "state": "stopped",
        "attempted_cycles": 2,
        "successful_cycles": 2,
        "last_error_kind": None,
        "stop_reason": "max_cycles",
    }
    payload.update(overrides)
    return payload


class IngestionNegativeEvidenceTests(unittest.TestCase):
    def _write(self, root: Path, payload) -> Path:
        path = root / "continuous_observation_status.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_recovered_provider_failure_remains_in_denominator(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                Path(tmp),
                _status(attempted_cycles=2, successful_cycles=1, last_error_kind=None),
            )
            evidence = project_ingestion_negative_evidence(path)

        self.assertEqual(evidence.evidence_state, "observed")
        self.assertEqual(evidence.failed_cycles, 1)
        self.assertEqual(evidence.denominator_cycles, 2)
        self.assertEqual(evidence.successful_fraction, 0.5)
        self.assertTrue(evidence.has_negative_evidence)
        self.assertIn("failed_attempts_present", evidence.reason_codes)

    def test_missing_status_is_explicit_negative_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuous_observation_status.json"
            evidence = project_ingestion_negative_evidence(path, expected_cycles=4)

        self.assertEqual(evidence.evidence_state, "missing")
        self.assertEqual(evidence.unobserved_expected_cycles, 4)
        self.assertEqual(evidence.denominator_cycles, 4)
        self.assertEqual(evidence.successful_fraction, 0.0)
        self.assertTrue(evidence.has_negative_evidence)
        self.assertEqual(evidence.reason_codes, ("status_missing",))

    def test_expected_but_unattempted_cycles_are_not_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), _status(attempted_cycles=2, successful_cycles=2))
            evidence = project_ingestion_negative_evidence(path, expected_cycles=5)

        self.assertEqual(evidence.failed_cycles, 0)
        self.assertEqual(evidence.unobserved_expected_cycles, 3)
        self.assertEqual(evidence.denominator_cycles, 5)
        self.assertEqual(evidence.successful_fraction, 0.4)
        self.assertTrue(evidence.has_negative_evidence)
        self.assertIn("expected_cycles_unobserved", evidence.reason_codes)

    def test_all_successful_observed_cycles_have_no_negative_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), _status())
            evidence = project_ingestion_negative_evidence(path, expected_cycles=2)

        self.assertFalse(evidence.has_negative_evidence)
        self.assertEqual(evidence.reason_codes, ())
        self.assertEqual(evidence.successful_fraction, 1.0)

    def test_success_above_attempts_is_explicit_invalid_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                Path(tmp),
                _status(attempted_cycles=1, successful_cycles=2),
            )
            evidence = project_ingestion_negative_evidence(path, expected_cycles=3)

        self.assertEqual(evidence.evidence_state, "invalid")
        self.assertEqual(evidence.denominator_cycles, 3)
        self.assertTrue(evidence.has_negative_evidence)
        self.assertEqual(evidence.reason_codes, ("status_success_exceeds_attempts",))

    def test_invalid_json_is_not_silently_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuous_observation_status.json"
            path.write_text("{not-json", encoding="utf-8")
            evidence = project_ingestion_negative_evidence(path)

        self.assertEqual(evidence.evidence_state, "invalid")
        self.assertTrue(evidence.has_negative_evidence)
        self.assertEqual(evidence.reason_codes, ("status_invalid_json",))

    def test_bool_counter_is_rejected_even_though_bool_is_int_subclass(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), _status(attempted_cycles=True))
            evidence = project_ingestion_negative_evidence(path)

        self.assertEqual(evidence.evidence_state, "invalid")
        self.assertEqual(evidence.reason_codes, ("status_invalid_cycle_counter",))

    def test_attempts_beyond_expectation_remain_in_denominator(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(
                Path(tmp),
                _status(attempted_cycles=4, successful_cycles=3),
            )
            evidence = project_ingestion_negative_evidence(path, expected_cycles=2)

        self.assertEqual(evidence.denominator_cycles, 4)
        self.assertEqual(evidence.failed_cycles, 1)
        self.assertEqual(evidence.unobserved_expected_cycles, 0)
        self.assertEqual(evidence.successful_fraction, 0.75)

    def test_invalid_expected_cycles_is_caller_error(self):
        with self.assertRaises(ValueError):
            project_ingestion_negative_evidence("unused.json", expected_cycles=-1)
        with self.assertRaises(ValueError):
            project_ingestion_negative_evidence("unused.json", expected_cycles=True)


if __name__ == "__main__":
    unittest.main()

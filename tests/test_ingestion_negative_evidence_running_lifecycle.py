import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from autosport.ingestion_negative_evidence import (
    main,
    project_ingestion_negative_evidence,
)


def _running_status() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "autosport_continuous_local_observation",
        "run_id": "run-running",
        "source_id": "provider-a",
        "state": "running",
        "attempted_cycles": 2,
        "successful_cycles": 2,
        "last_error_kind": None,
        "stop_reason": None,
    }


class RunningLifecycleNegativeEvidenceTests(unittest.TestCase):
    def _write_running_status(self, root: Path) -> Path:
        path = root / "continuous_observation_status.json"
        path.write_text(json.dumps(_running_status()), encoding="utf-8")
        return path

    def test_running_lifecycle_is_incomplete_even_when_all_attempts_succeeded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_running_status(Path(tmp))
            evidence = project_ingestion_negative_evidence(path, expected_cycles=2)

        self.assertEqual(evidence.evidence_state, "observed")
        self.assertEqual(evidence.lifecycle_state, "running")
        self.assertEqual(evidence.failed_cycles, 0)
        self.assertEqual(evidence.unobserved_expected_cycles, 0)
        self.assertEqual(evidence.successful_fraction, 1.0)
        self.assertTrue(evidence.has_negative_evidence)
        self.assertEqual(evidence.reason_codes, ("lifecycle_incomplete",))

    def test_running_lifecycle_cli_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write_running_status(Path(tmp))
            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = main([str(path), "--expected-cycles", "2"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 2)
        self.assertTrue(payload["has_negative_evidence"])
        self.assertEqual(payload["reason_codes"], ["lifecycle_incomplete"])


if __name__ == "__main__":
    unittest.main()

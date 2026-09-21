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


class IngestionNegativeEvidenceRunningStateTests(unittest.TestCase):
    def test_running_lifecycle_remains_explicit_incomplete_evidence(self):
        status = {
            "schema_version": 1,
            "kind": "autosport_continuous_local_observation",
            "run_id": "run-active",
            "source_id": "provider-a",
            "state": "running",
            "attempted_cycles": 2,
            "successful_cycles": 2,
            "last_error_kind": None,
            "stop_reason": None,
        }

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "continuous_observation_status.json"
            path.write_text(json.dumps(status), encoding="utf-8")

            evidence = project_ingestion_negative_evidence(path, expected_cycles=2)

            stdout = StringIO()
            with redirect_stdout(stdout):
                exit_code = main([str(path), "--expected-cycles", "2"])
            cli_payload = json.loads(stdout.getvalue())

        # A RUNNING acquisition is nonterminal. Equal attempted/successful counters
        # cannot turn an in-progress observation window into clean/complete evidence.
        self.assertTrue(evidence.has_negative_evidence)
        self.assertIn("lifecycle_incomplete", evidence.reason_codes)
        self.assertEqual(exit_code, 2)
        self.assertTrue(cli_payload["has_negative_evidence"])
        self.assertIn("lifecycle_incomplete", cli_payload["reason_codes"])


if __name__ == "__main__":
    unittest.main()

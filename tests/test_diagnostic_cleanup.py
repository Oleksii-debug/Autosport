from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autosport.diagnostic as diagnostic


class _TrackingStore:
    last_instance: "_TrackingStore | None" = None

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.closed = False
        _TrackingStore.last_instance = self

    def append(self, event) -> bool:
        return True

    def close(self) -> None:
        self.closed = True


class _FailingReplayEngine:
    def __init__(self, events, firewall) -> None:
        del events, firewall

    def run(self, sink, *, run_id: str):
        del sink, run_id
        raise RuntimeError("diagnostic replay failed")


class DiagnosticCleanupTests(unittest.TestCase):
    def test_failure_closes_temporary_market_store_before_reporting(self) -> None:
        _TrackingStore.last_instance = None
        with tempfile.TemporaryDirectory() as temporary:
            output_path = Path(temporary) / "diagnostic.json"
            with (
                patch.object(diagnostic, "SQLiteMarketStore", _TrackingStore),
                patch.object(diagnostic, "ReplayEngine", _FailingReplayEngine),
            ):
                exit_code = diagnostic.run_machine_diagnostic(output_path)

            self.assertEqual(exit_code, 1)
            self.assertIsNotNone(_TrackingStore.last_instance)
            assert _TrackingStore.last_instance is not None
            self.assertTrue(_TrackingStore.last_instance.closed)
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "FAIL")
            self.assertEqual(payload["error"], "RuntimeError: diagnostic replay failed")
            self.assertFalse(payload["real_money_execution"])
            self.assertFalse(payload["human_tested"])
            self.assertFalse(payload["nvda_verified"])


if __name__ == "__main__":
    unittest.main()

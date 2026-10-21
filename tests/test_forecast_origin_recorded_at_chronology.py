from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from autosport.decision_ledger import DecisionLedgerIntegrityError
from autosport.evaluation_bundle import WalkForwardBundle, evaluate_walk_forward_bundle
from test_forecast_origin_binding import _bundle_raw, _write_bundle, _write_dataset, _write_origin


class ForecastOriginRecordedAtChronologyTests(unittest.TestCase):
    def test_recorded_at_before_canonical_decision_time_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = _write_dataset(root / "dataset")
            raw = _bundle_raw(dataset)
            _write_origin(
                root,
                dataset,
                raw,
                second_recorded_at="2026-03-01T11:59:59+00:00",
            )

            # The verified durable ledger must reject impossible chronology
            # before forecast-origin projection can consume the malformed row.
            with self.assertRaisesRegex(
                ValueError,
                "canonical decision ledger failed semantic integrity validation",
            ) as rejected:
                evaluate_walk_forward_bundle(
                    WalkForwardBundle.from_path(_write_bundle(root, raw))
                )
            self.assertIsInstance(rejected.exception.__cause__, DecisionLedgerIntegrityError)
            self.assertIn(
                "Decision Ledger observed_ts is after recorded_at",
                str(rejected.exception.__cause__),
            )


if __name__ == "__main__":
    unittest.main()

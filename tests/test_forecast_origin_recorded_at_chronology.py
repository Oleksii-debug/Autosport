from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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

            with self.assertRaisesRegex(
                ValueError,
                "recorded_at is before its canonical decision time",
            ):
                evaluate_walk_forward_bundle(
                    WalkForwardBundle.from_path(_write_bundle(root, raw))
                )


if __name__ == "__main__":
    unittest.main()

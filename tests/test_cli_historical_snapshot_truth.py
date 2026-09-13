from __future__ import annotations

import io
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autosport import cli


class HistoricalSnapshotCliTruthTests(unittest.TestCase):
    def test_nonempty_snapshot_never_promotes_historical_market_coverage(self) -> None:
        report = SimpleNamespace(
            has_data=True,
            quote_count=2,
            snapshot_at="2026-09-12T10:00:00Z",
            snapshot_timestamp_fallback_count=2,
            output_path="market.jsonl",
            evidence_path="market.jsonl.evidence.json",
        )

        def provider_factory(*args, **kwargs):
            return object()

        stdout = io.StringIO()
        with patch.dict(os.environ, {"AUTOSPORT_PARLAYAPI_KEY": "runtime-only-secret"}, clear=True):
            with patch("autosport.cli.capture_historical_snapshot", return_value=report):
                with patch("sys.stdout", stdout):
                    code = cli.run_historical_snapshot(
                        requested_at="2026-09-12T10:03:00Z",
                        output=Path("market.jsonl"),
                        evidence=None,
                        regions="us",
                        markets="h2h",
                        provider_factory=provider_factory,
                    )

        output = stdout.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("historical_snapshot=DATA_AVAILABLE", output)
        self.assertIn("point_in_time_snapshot_contains_odds=true", output)
        self.assertIn("point_in_time_odds_market_coverage_verified=false", output)
        self.assertIn("historical_window_market_coverage_verified=false", output)
        self.assertIn("sealed_outcomes_present=false", output)
        self.assertIn("replay_corpus_ready=false", output)
        self.assertNotIn("point_in_time_odds_market_coverage_verified=true", output)
        self.assertNotIn("runtime-only-secret", output)


if __name__ == "__main__":
    unittest.main()

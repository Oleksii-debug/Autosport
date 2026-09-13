from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from autosport.historical_corpus import _snapshot
from autosport.parlayapi_provider import ParlayApiTableTennisProvider


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class HistoricalCorpusSnapshotCausalityTests(unittest.TestCase):
    def test_capture_before_requested_historical_time_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            market = root / "market.jsonl"
            evidence = root / "evidence.json"
            event = {
                "event_id": "tt-causal",
                "market_id": "winner",
                "selection_id": "alice",
                "decimal_odds": "1.80",
                "observed_ts": "2026-01-01T23:00:00+00:00",
                "source_id": ParlayApiTableTennisProvider.source_id,
                "sequence": 1,
                "market_type": "winner",
                "source_ts": "2026-01-01T22:59:59+00:00",
                "ingest_ts": "2026-01-01T23:30:00+00:00",
                "metadata": {},
            }
            market.write_text(json.dumps(event, sort_keys=True) + "\n", encoding="utf-8")
            evidence.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "parlayapi_point_in_time_historical_snapshot",
                        "provider": "parlayapi",
                        "sport_key": "table_tennis",
                        "requested_at": "2026-01-02T00:00:00+00:00",
                        "snapshot_at": "2026-01-01T23:00:00+00:00",
                        "captured_at": "2026-01-01T23:30:00+00:00",
                        "market_sha256": _sha256(market),
                        "quote_count": 1,
                        "has_data": True,
                        "point_in_time_snapshot_contains_odds": True,
                        "point_in_time_odds_market_coverage_verified": False,
                        "historical_window_market_coverage_verified": False,
                        "sealed_outcomes_present": False,
                        "replay_corpus_ready": False,
                        "terms_reference": "https://example.test/terms",
                        "real_money_execution": False,
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "captured_at is before requested_at"):
                _snapshot(
                    market,
                    evidence,
                    expected_terms_reference="https://example.test/terms",
                )


if __name__ == "__main__":
    unittest.main()

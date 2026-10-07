import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from autosport.historical_corpus import _snapshot
from autosport.parlayapi_provider import ParlayApiTableTennisProvider


class HistoricalSnapshotProviderBindingTests(unittest.TestCase):
    def _write_pair(
        self,
        root: Path,
        *,
        source_id: str,
        provider: str = "parlayapi",
        sport_key: str = "table_tennis",
    ) -> tuple[Path, Path]:
        market_path = root / "snapshot.jsonl"
        evidence_path = root / "snapshot.evidence.json"

        event = {
            "event_id": "tt-provider-binding",
            "market_id": "winner",
            "selection_id": "alice",
            "decimal_odds": "1.80",
            "observed_ts": "2026-01-01T10:00:01+00:00",
            "source_id": source_id,
            "sequence": 1,
            "market_type": "winner",
            "source_ts": "2026-01-01T10:00:00+00:00",
            "ingest_ts": "2026-01-02T00:00:00+00:00",
            "metadata": {
                "bookmaker_key": "book-a",
                "source_time_semantics": "provider_quote_last_update",
            },
        }
        market_path.write_text(json.dumps(event, sort_keys=True) + "\n", encoding="utf-8")
        market_sha = hashlib.sha256(market_path.read_bytes()).hexdigest()

        evidence = {
            "schema_version": 1,
            "kind": "parlayapi_point_in_time_historical_snapshot",
            "provider": provider,
            "sport_key": sport_key,
            "requested_at": "2026-01-01T10:00:30+00:00",
            "snapshot_at": "2026-01-01T10:00:20+00:00",
            "captured_at": "2026-01-02T00:00:00+00:00",
            "response_sha256": "1" * 64,
            "market_sha256": market_sha,
            "quote_count": 1,
            "has_data": True,
            "market_types": ["winner"],
            "bookmaker_keys": ["book-a"],
            "snapshot_timestamp_fallback_count": 0,
            "point_in_time_snapshot_contains_odds": True,
            "point_in_time_odds_market_coverage_verified": False,
            "historical_window_market_coverage_verified": False,
            "sealed_outcomes_present": False,
            "replay_corpus_ready": False,
            "terms_reference": "https://parlay-api.com/terms",
            "licensing_or_retention_verified": False,
            "redistribution_verified": False,
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
        evidence_path.write_text(json.dumps(evidence, sort_keys=True), encoding="utf-8")
        return market_path, evidence_path

    def test_producer_shaped_provider_and_sport_bind_to_canonical_source_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_path, evidence_path = self._write_pair(
                Path(tmp),
                source_id=ParlayApiTableTennisProvider.source_id,
            )
            rows, evidence = _snapshot(
                market_path,
                evidence_path,
                expected_terms_reference="https://parlay-api.com/terms",
            )

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0][0].source_id, ParlayApiTableTennisProvider.source_id)
            self.assertEqual(evidence["provider"], "parlayapi")
            self.assertEqual(evidence["sport_key"], ParlayApiTableTennisProvider.sport_key)

    def test_snapshot_evidence_provider_must_reject_forged_market_source_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_path, evidence_path = self._write_pair(
                Path(tmp),
                source_id="forged-provider",
            )

            with self.assertRaisesRegex(ValueError, "provider.*source_id"):
                _snapshot(
                    market_path,
                    evidence_path,
                    expected_terms_reference="https://parlay-api.com/terms",
                )

    def test_parlayapi_snapshot_kind_rejects_renamed_provider_even_with_matching_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            market_path, evidence_path = self._write_pair(
                Path(tmp),
                provider="evil",
                source_id="evil:table_tennis",
            )

            with self.assertRaisesRegex(ValueError, "provider must be parlayapi"):
                _snapshot(
                    market_path,
                    evidence_path,
                    expected_terms_reference="https://parlay-api.com/terms",
                )


if __name__ == "__main__":
    unittest.main()

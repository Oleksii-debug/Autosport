from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from autosport.historical_snapshot import capture_historical_snapshot
from autosport.parlayapi_provider import HttpJsonResponse, ParlayApiTableTennisProvider


def _payload() -> dict[str, object]:
    return {
        "timestamp": "2026-09-12T10:00:00Z",
        "previous_timestamp": "2026-09-12T09:55:00Z",
        "next_timestamp": "2026-09-12T10:05:00Z",
        "data": [
            {
                "id": "tt-1",
                "sport_key": "table_tennis",
                "commence_time": "2026-09-12T11:00:00Z",
                "home_team": "Player A",
                "away_team": "Player B",
                "bookmakers": [
                    {
                        "key": "book-a",
                        "title": "Book A",
                        "markets": [
                            {
                                "key": "h2h",
                                "last_update": "2026-09-12T09:59:30Z",
                                "outcomes": [
                                    {"name": "Player A", "price": 1.8},
                                    {"name": "Player B", "price": 2.1},
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }


class _Transport:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> HttpJsonResponse:
        return HttpJsonResponse(self.payload, 200, {"x-api-version": "test"})


def _provider(payload: object) -> ParlayApiTableTennisProvider:
    return ParlayApiTableTennisProvider(
        "unit-test-key",
        transport=_Transport(payload),
        clock=lambda: "2026-09-13T03:00:00+00:00",
        sleeper=lambda _: None,
    )


class HistoricalObservedCoverageTests(unittest.TestCase):
    def test_snapshot_evidence_counts_observed_fixtures_and_fixture_markets_only(self) -> None:
        payload = _payload()
        first = payload["data"][0]
        first["bookmakers"][0]["markets"].append(
            {
                "key": "totals",
                "last_update": "2026-09-12T09:59:30Z",
                "outcomes": [
                    {"name": "Over", "price": 1.9, "point": 3.5},
                    {"name": "Under", "price": 1.9, "point": 3.5},
                ],
            }
        )
        second = copy.deepcopy(first)
        second["id"] = "tt-2"
        second["home_team"] = "Player C"
        second["away_team"] = "Player D"
        payload["data"].append(second)

        with tempfile.TemporaryDirectory() as temp:
            evidence_path = Path(temp) / "evidence.json"
            report = capture_historical_snapshot(
                _provider(payload),
                requested_at="2026-09-12T10:03:00Z",
                output_path=Path(temp) / "market.jsonl",
                evidence_path=evidence_path,
            )
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

        self.assertEqual(report.quote_count, 8)
        self.assertEqual(evidence["market_types"], ["total", "winner"])
        self.assertEqual(
            evidence["observed_coverage"],
            {
                "scope": "captured_snapshot_rows_only",
                "fixture_identity": "canonical_event_id",
                "fixture_market_identity": "canonical_event_id_plus_market_id",
                "fixture_count": 2,
                "fixture_market_count": 4,
                "quote_count": 8,
                "quote_count_by_market_type": {"total": 4, "winner": 4},
                "completeness_semantics": "observed_rows_only_not_provider_universe",
            },
        )
        self.assertFalse(evidence["point_in_time_odds_market_coverage_verified"])
        self.assertFalse(evidence["historical_window_market_coverage_verified"])

    def test_empty_snapshot_has_zero_observed_coverage_without_promoting_completeness(self) -> None:
        payload = _payload()
        payload["data"] = []

        with tempfile.TemporaryDirectory() as temp:
            evidence_path = Path(temp) / "evidence.json"
            report = capture_historical_snapshot(
                _provider(payload),
                requested_at="2026-09-12T10:03:00Z",
                output_path=Path(temp) / "market.jsonl",
                evidence_path=evidence_path,
            )
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

        self.assertFalse(report.has_data)
        self.assertEqual(
            evidence["observed_coverage"],
            {
                "scope": "captured_snapshot_rows_only",
                "fixture_identity": "canonical_event_id",
                "fixture_market_identity": "canonical_event_id_plus_market_id",
                "fixture_count": 0,
                "fixture_market_count": 0,
                "quote_count": 0,
                "quote_count_by_market_type": {},
                "completeness_semantics": "observed_rows_only_not_provider_universe",
            },
        )
        self.assertFalse(evidence["point_in_time_snapshot_contains_odds"])
        self.assertFalse(evidence["point_in_time_odds_market_coverage_verified"])
        self.assertFalse(evidence["historical_window_market_coverage_verified"])


if __name__ == "__main__":
    unittest.main()

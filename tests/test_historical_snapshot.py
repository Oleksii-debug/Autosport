from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from autosport.historical_snapshot import capture_historical_snapshot
from autosport.parlayapi_provider import (
    HttpJsonResponse,
    ParlayApiTableTennisProvider,
    ProviderPayloadError,
)


class _Transport:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> HttpJsonResponse:
        self.urls.append(url)
        self.headers.append(dict(headers))
        return HttpJsonResponse(self.payload, 200, {"x-api-version": "test"})


def _payload(*, timestamp: str = "2026-09-12T10:00:00Z", last_update: str | None = None) -> dict[str, object]:
    market: dict[str, object] = {
        "key": "h2h",
        "outcomes": [
            {"name": "Player A", "price": 1.8},
            {"name": "Player B", "price": 2.1},
        ],
    }
    if last_update is not None:
        market["last_update"] = last_update
    return {
        "timestamp": timestamp,
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
                        "key": "bovada",
                        "title": "Bovada",
                        "markets": [market],
                    }
                ],
            }
        ],
    }


class HistoricalSnapshotTests(unittest.TestCase):
    def _provider(self, transport: _Transport) -> ParlayApiTableTennisProvider:
        return ParlayApiTableTennisProvider(
            "secret-key-must-not-leak",
            transport=transport,
            clock=lambda: "2026-09-13T02:00:00+00:00",
            sleeper=lambda _: None,
        )

    def test_capture_uses_provider_snapshot_time_when_quote_last_update_is_missing(self) -> None:
        transport = _Transport(_payload())
        provider = self._provider(transport)
        with tempfile.TemporaryDirectory() as temp:
            market_path = Path(temp) / "market.jsonl"
            evidence_path = Path(temp) / "evidence.json"
            report = capture_historical_snapshot(
                provider,
                requested_at="2026-09-12T10:03:00Z",
                output_path=market_path,
                evidence_path=evidence_path,
            )
            rows = [json.loads(line) for line in market_path.read_text(encoding="utf-8").splitlines()]
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

        self.assertEqual(report.quote_count, 2)
        self.assertEqual(report.snapshot_timestamp_fallback_count, 2)
        self.assertEqual(report.bookmaker_keys, ("bovada",))
        self.assertEqual(report.provider_market_keys, ("h2h",))
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["source_ts"], "2026-09-12T10:00:00Z")
            self.assertEqual(row["observed_ts"], "2026-09-12T10:00:00Z")
            self.assertEqual(row["ingest_ts"], "2026-09-13T02:00:00+00:00")
            self.assertEqual(row["metadata"]["source_time_semantics"], "provider_historical_snapshot_timestamp")
            self.assertFalse(row["metadata"]["provider_quote_last_update_present"])
        self.assertTrue(evidence["point_in_time_snapshot_data_observed"])
        self.assertEqual(evidence["requested_markets"], ["h2h", "spreads", "totals"])
        self.assertEqual(evidence["observed_provider_market_keys"], ["h2h"])
        self.assertFalse(evidence["requested_market_set_complete_verified"])
        self.assertFalse(evidence["historical_window_coverage_verified"])
        self.assertNotIn("point_in_time_odds_market_coverage_verified", evidence)
        self.assertFalse(evidence["sealed_outcomes_present"])
        self.assertFalse(evidence["replay_corpus_ready"])
        self.assertFalse(evidence["licensing_or_retention_verified"])
        self.assertFalse(evidence["redistribution_verified"])
        self.assertFalse(evidence["real_money_execution"])
        serialized = json.dumps(evidence) + json.dumps(rows)
        self.assertNotIn("secret-key-must-not-leak", serialized)
        self.assertEqual(transport.headers[0]["X-API-Key"], "secret-key-must-not-leak")
        query = parse_qs(urlparse(transport.urls[0]).query)
        self.assertEqual(query["date"], ["2026-09-12T10:03:00Z"])
        self.assertEqual(query["markets"], ["h2h,spreads,totals"])
        self.assertEqual(query["oddsFormat"], ["decimal"])

    def test_real_quote_last_update_is_preserved(self) -> None:
        transport = _Transport(_payload(last_update="2026-09-12T09:59:30Z"))
        provider = self._provider(transport)
        with tempfile.TemporaryDirectory() as temp:
            market_path = Path(temp) / "market.jsonl"
            report = capture_historical_snapshot(
                provider,
                requested_at="2026-09-12T10:03:00Z",
                output_path=market_path,
            )
            rows = [json.loads(line) for line in market_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(report.snapshot_timestamp_fallback_count, 0)
        self.assertEqual({row["source_ts"] for row in rows}, {"2026-09-12T09:59:30Z"})
        self.assertEqual({row["metadata"]["source_time_semantics"] for row in rows}, {"provider_quote_last_update"})

    def test_future_quote_update_fails_closed(self) -> None:
        transport = _Transport(_payload(last_update="2026-09-12T10:00:01Z"))
        provider = self._provider(transport)
        with tempfile.TemporaryDirectory() as temp:
            market_path = Path(temp) / "market.jsonl"
            with self.assertRaisesRegex(ProviderPayloadError, "after historical snapshot"):
                capture_historical_snapshot(
                    provider,
                    requested_at="2026-09-12T10:03:00Z",
                    output_path=market_path,
                )
            self.assertFalse(market_path.exists())

    def test_snapshot_after_requested_time_fails_closed(self) -> None:
        transport = _Transport(_payload(timestamp="2026-09-12T10:04:00Z"))
        provider = self._provider(transport)
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ProviderPayloadError, "after requested_at"):
                capture_historical_snapshot(
                    provider,
                    requested_at="2026-09-12T10:03:00Z",
                    output_path=Path(temp) / "market.jsonl",
                )

    def test_missing_response_timestamp_fails_closed(self) -> None:
        payload = _payload()
        payload.pop("timestamp")
        transport = _Transport(payload)
        provider = self._provider(transport)
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ProviderPayloadError, "requires non-empty timestamp"):
                capture_historical_snapshot(
                    provider,
                    requested_at="2026-09-12T10:03:00Z",
                    output_path=Path(temp) / "market.jsonl",
                )

    def test_empty_snapshot_is_machine_visible_but_not_completeness_verified(self) -> None:
        payload = _payload()
        payload["data"] = []
        transport = _Transport(payload)
        provider = self._provider(transport)
        with tempfile.TemporaryDirectory() as temp:
            evidence_path = Path(temp) / "evidence.json"
            report = capture_historical_snapshot(
                provider,
                requested_at="2026-09-12T10:03:00Z",
                output_path=Path(temp) / "market.jsonl",
                evidence_path=evidence_path,
            )
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

        self.assertFalse(report.has_data)
        self.assertFalse(evidence["has_data"])
        self.assertFalse(evidence["point_in_time_snapshot_data_observed"])
        self.assertFalse(evidence["requested_market_set_complete_verified"])
        self.assertFalse(evidence["historical_window_coverage_verified"])
        self.assertFalse(evidence["replay_corpus_ready"])


if __name__ == "__main__":
    unittest.main()

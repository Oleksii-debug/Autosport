from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from autosport.historical_matches import capture_historical_matches
from autosport.parlayapi_provider import HttpJsonResponse, ParlayApiTableTennisProvider, ProviderPayloadError


class _Transport:
    def __init__(self, payload: object, headers: dict[str, str] | None = None) -> None:
        self.payload = payload
        self.response_headers = headers if headers is not None else {
            "x-historical-window-hours": "168",
            "x-historical-window-from": "2026-09-06T00:00:00Z",
            "x-api-version": "test",
            "x-coverage-hint": "source=test-source",
        }
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> HttpJsonResponse:
        self.urls.append(url)
        self.headers.append(dict(headers))
        return HttpJsonResponse(self.payload, 200, self.response_headers)


class HistoricalMatchCaptureTests(unittest.TestCase):
    def _provider(self, transport: _Transport) -> ParlayApiTableTennisProvider:
        return ParlayApiTableTennisProvider(
            "unit-test-key",
            transport=transport,
            clock=lambda: "2026-09-13T03:00:00+00:00",
            sleeper=lambda _: None,
        )

    def test_capture_is_opaque_and_keeps_truth_boundaries(self) -> None:
        payload = [{"provider_defined_id": "match-1", "has_odds": False, "opaque": {"value": 1}}]
        transport = _Transport(payload)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "matches.json"
            evidence_path = Path(temp) / "matches.evidence.json"
            report = capture_historical_matches(
                self._provider(transport),
                date_from="2026-09-10",
                date_to="2026-09-12",
                output_path=output,
                evidence_path=evidence_path,
                sources=("source-b", "source-a", "source-a"),
                limit=500,
            )
            capture = json.loads(output.read_text(encoding="utf-8"))
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

        self.assertEqual(capture["payload"], payload)
        self.assertEqual(capture["request"]["sources"], ["source-a", "source-b"])
        self.assertEqual(evidence["capture_sha256"], report.capture_sha256)
        self.assertEqual(evidence["coverage_hint"], "source=test-source")
        self.assertFalse(evidence["provider_result_schema_parsed"])
        self.assertFalse(evidence["sealed_quote_outcomes_derived"])
        self.assertFalse(evidence["point_in_time_odds_market_coverage_verified"])
        self.assertFalse(evidence["historical_window_market_coverage_verified"])
        self.assertFalse(evidence["replay_corpus_ready"])
        self.assertFalse(evidence["licensing_or_retention_verified"])
        self.assertNotIn("unit-test-key", json.dumps(capture) + json.dumps(evidence))
        self.assertEqual(transport.headers[0]["X-API-Key"], "unit-test-key")
        parsed = urlparse(transport.urls[0])
        self.assertEqual(parsed.path, "/v1/historical/sports/table_tennis/matches")
        query = parse_qs(parsed.query)
        self.assertEqual(query["dateFrom"], ["2026-09-10"])
        self.assertEqual(query["dateTo"], ["2026-09-12"])
        self.assertEqual(query["pricedOnly"], ["false"])
        self.assertEqual(query["includeRaw"], ["false"])
        self.assertEqual(query["sources"], ["source-a,source-b"])
        self.assertEqual(query["limit"], ["500"])

    def test_missing_entitlement_headers_fail_closed_before_output(self) -> None:
        transport = _Transport([], headers={"x-api-version": "test"})
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "matches.json"
            with self.assertRaisesRegex(ProviderPayloadError, "missing entitlement-window headers"):
                capture_historical_matches(
                    self._provider(transport),
                    date_from="2026-09-10",
                    date_to="2026-09-12",
                    output_path=output,
                )
            self.assertFalse(output.exists())

    def test_out_of_entitlement_range_fails_closed(self) -> None:
        transport = _Transport([], headers={
            "x-historical-window-hours": "48",
            "x-historical-window-from": "2026-09-11T00:00:00Z",
        })
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "matches.json"
            with self.assertRaisesRegex(ProviderPayloadError, "contradicts its entitlement-window header"):
                capture_historical_matches(
                    self._provider(transport),
                    date_from="2026-09-10",
                    date_to="2026-09-12",
                    output_path=output,
                )
            self.assertFalse(output.exists())

    def test_invalid_range_fails_before_network(self) -> None:
        transport = _Transport([])
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "date_to must not precede date_from"):
                capture_historical_matches(
                    self._provider(transport),
                    date_from="2026-09-12",
                    date_to="2026-09-10",
                    output_path=Path(temp) / "matches.json",
                )
        self.assertEqual(transport.urls, [])

    def test_scalar_provider_response_fails_closed(self) -> None:
        transport = _Transport("not-a-result-archive")
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "matches.json"
            with self.assertRaisesRegex(ProviderPayloadError, "JSON object or array"):
                capture_historical_matches(
                    self._provider(transport),
                    date_from="2026-09-10",
                    date_to="2026-09-12",
                    output_path=output,
                )
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()

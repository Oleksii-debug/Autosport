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

    def test_capture_uses_only_documented_query_and_keeps_truth_boundaries(self) -> None:
        payload = [{"provider_defined_id": "match-1", "has_odds": False, "opaque": {"value": 1}}]
        transport = _Transport(payload)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "matches.json"
            evidence_path = Path(temp) / "matches.evidence.json"
            report = capture_historical_matches(
                self._provider(transport),
                requested_date="2026-09-10",
                output_path=output,
                evidence_path=evidence_path,
            )
            capture = json.loads(output.read_text(encoding="utf-8"))
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

        self.assertEqual(capture["payload"], payload)
        self.assertEqual(capture["request"], {"date": "2026-09-10", "priced_only": False})
        self.assertEqual(evidence["requested_date"], "2026-09-10")
        self.assertFalse(evidence["priced_only"])
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
        self.assertEqual(set(query), {"date", "pricedOnly"})
        self.assertEqual(query["date"], ["2026-09-10"])
        self.assertEqual(query["pricedOnly"], ["false"])
        self.assertNotIn("unit-test-key", transport.urls[0])

    def test_priced_only_maps_to_documented_boolean_parameter(self) -> None:
        transport = _Transport([])
        with tempfile.TemporaryDirectory() as temp:
            capture_historical_matches(
                self._provider(transport),
                requested_date="2026-09-10",
                priced_only=True,
                output_path=Path(temp) / "matches.json",
            )
        query = parse_qs(urlparse(transport.urls[0]).query)
        self.assertEqual(query, {"date": ["2026-09-10"], "pricedOnly": ["true"]})

    def test_capture_and_evidence_paths_must_not_alias(self) -> None:
        transport = _Transport([])
        with tempfile.TemporaryDirectory() as temp:
            artifact = Path(temp) / "matches.json"
            with self.assertRaisesRegex(ValueError, "must refer to different files"):
                capture_historical_matches(
                    self._provider(transport),
                    requested_date="2026-09-10",
                    output_path=artifact,
                    evidence_path=artifact,
                )
            self.assertFalse(artifact.exists())
        self.assertEqual(transport.urls, [])

    def test_noncanonical_date_forms_fail_before_network(self) -> None:
        for requested_date in ("20260910", "2026-W37-4"):
            with self.subTest(requested_date=requested_date):
                transport = _Transport([])
                with tempfile.TemporaryDirectory() as temp:
                    with self.assertRaisesRegex(ValueError, "requested_date must be YYYY-MM-DD"):
                        capture_historical_matches(
                            self._provider(transport),
                            requested_date=requested_date,
                            output_path=Path(temp) / "matches.json",
                        )
                self.assertEqual(transport.urls, [])

    def test_nonfinite_provider_response_fails_closed_before_output(self) -> None:
        transport = _Transport([{"provider_defined_id": "match-1", "score": float("nan")}])
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "matches.json"
            evidence = Path(temp) / "matches.evidence.json"
            with self.assertRaisesRegex(ProviderPayloadError, "strict UTF-8 JSON"):
                capture_historical_matches(
                    self._provider(transport),
                    requested_date="2026-09-10",
                    output_path=output,
                    evidence_path=evidence,
                )
            self.assertFalse(output.exists())
            self.assertFalse(evidence.exists())

    def test_missing_entitlement_headers_fail_closed_before_output(self) -> None:
        transport = _Transport([], headers={"x-api-version": "test"})
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "matches.json"
            with self.assertRaisesRegex(ProviderPayloadError, "missing entitlement-window headers"):
                capture_historical_matches(
                    self._provider(transport),
                    requested_date="2026-09-10",
                    output_path=output,
                )
            self.assertFalse(output.exists())

    def test_out_of_entitlement_date_fails_closed(self) -> None:
        transport = _Transport([], headers={
            "x-historical-window-hours": "48",
            "x-historical-window-from": "2026-09-11T00:00:00Z",
        })
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "matches.json"
            with self.assertRaisesRegex(ProviderPayloadError, "contradicts its entitlement-window header"):
                capture_historical_matches(
                    self._provider(transport),
                    requested_date="2026-09-10",
                    output_path=output,
                )
            self.assertFalse(output.exists())

    def test_invalid_date_fails_before_network(self) -> None:
        transport = _Transport([])
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "requested_date must be YYYY-MM-DD"):
                capture_historical_matches(
                    self._provider(transport),
                    requested_date="2026-09-40",
                    output_path=Path(temp) / "matches.json",
                )
        self.assertEqual(transport.urls, [])

    def test_non_boolean_priced_only_fails_before_network(self) -> None:
        transport = _Transport([])
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "priced_only must be boolean"):
                capture_historical_matches(
                    self._provider(transport),
                    requested_date="2026-09-10",
                    priced_only=1,  # type: ignore[arg-type]
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
                    requested_date="2026-09-10",
                    output_path=output,
                )
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()

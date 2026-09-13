from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from autosport.historical_acquisition import capture_historical_acquisition_bundle
from autosport.parlayapi_provider import HttpJsonResponse, ParlayApiTableTennisProvider, ProviderPayloadError


def _snapshot_payload() -> dict[str, object]:
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
    def __init__(self, *, fail_matches: bool = False) -> None:
        self.fail_matches = fail_matches
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> HttpJsonResponse:
        self.urls.append(url)
        self.headers.append(dict(headers))
        path = urlparse(url).path
        if path.endswith("/odds"):
            return HttpJsonResponse(_snapshot_payload(), 200, {"x-api-version": "test"})
        if path.endswith("/matches"):
            response_headers = {"x-api-version": "test"}
            if not self.fail_matches:
                response_headers.update(
                    {
                        "x-historical-window-hours": "168",
                        "x-historical-window-from": "2026-09-06T00:00:00Z",
                        "x-coverage-hint": "source=test-source",
                    }
                )
            return HttpJsonResponse(
                [{"match_id": "opaque-1", "result": {"provider_defined": True}}],
                200,
                response_headers,
            )
        raise AssertionError(f"unexpected URL: {url}")


class HistoricalAcquisitionBundleTests(unittest.TestCase):
    def _provider(self, transport: _Transport) -> ParlayApiTableTennisProvider:
        return ParlayApiTableTennisProvider(
            "unit-test-key",
            transport=transport,
            clock=lambda: "2026-09-13T03:00:00+00:00",
            sleeper=lambda _: None,
        )

    def test_bundle_binds_exact_files_without_promoting_truth(self) -> None:
        transport = _Transport()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "acquisition"
            report = capture_historical_acquisition_bundle(
                self._provider(transport),
                requested_at=("2026-09-12T10:03:00Z", "2026-09-12T10:08:00Z"),
                results_date_from="2026-09-10",
                results_date_to="2026-09-12",
                output_dir=root,
                result_sources=("source-b", "source-a", "source-a"),
                result_limit=500,
            )
            bundle_path = root / "bundle.json"
            bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
            serialized = bundle_path.read_text(encoding="utf-8")

            self.assertEqual(report.snapshot_count, 2)
            self.assertEqual(report.snapshots_with_odds, 2)
            self.assertEqual(bundle["request_identity"], report.request_identity)
            self.assertEqual(bundle["evidence_identity"], report.evidence_identity)
            self.assertEqual(
                report.bundle_sha256,
                hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(bundle["request_scope"]["match_results"]["sources"], ["source-a", "source-b"])
            self.assertEqual(bundle["request_scope"]["match_results"]["limit"], 500)
            self.assertFalse(bundle["provider_result_schema_parsed"])
            self.assertFalse(bundle["sealed_quote_outcomes_derived"])
            self.assertFalse(bundle["point_in_time_odds_market_coverage_verified"])
            self.assertFalse(bundle["historical_window_market_coverage_verified"])
            self.assertFalse(bundle["licensing_or_retention_verified"])
            self.assertFalse(bundle["redistribution_verified"])
            self.assertFalse(bundle["replay_corpus_ready"])
            self.assertFalse(bundle["real_money_execution"])
            self.assertFalse(bundle["human_tested"])
            self.assertFalse(bundle["nvda_verified"])
            self.assertNotIn("unit-test-key", serialized)

            for entry in bundle["snapshots"]:
                market = root / entry["market_file"]
                evidence = root / entry["evidence_file"]
                self.assertEqual(hashlib.sha256(market.read_bytes()).hexdigest(), entry["market_sha256"])
                self.assertEqual(hashlib.sha256(evidence.read_bytes()).hexdigest(), entry["evidence_sha256"])
            result_entry = bundle["match_results"]
            result_capture = root / result_entry["capture_file"]
            result_evidence = root / result_entry["evidence_file"]
            self.assertEqual(hashlib.sha256(result_capture.read_bytes()).hexdigest(), result_entry["capture_sha256"])
            self.assertEqual(hashlib.sha256(result_evidence.read_bytes()).hexdigest(), result_entry["evidence_sha256"])

        self.assertEqual(len(transport.urls), 3)
        self.assertTrue(all(headers["X-API-Key"] == "unit-test-key" for headers in transport.headers))
        match_url = next(url for url in transport.urls if urlparse(url).path.endswith("/matches"))
        match_query = parse_qs(urlparse(match_url).query)
        self.assertEqual(match_query["sources"], ["source-a,source-b"])
        self.assertEqual(match_query["limit"], ["500"])

    def test_equivalent_duplicate_snapshot_instants_fail_before_network(self) -> None:
        transport = _Transport()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "unique instants"):
                capture_historical_acquisition_bundle(
                    self._provider(transport),
                    requested_at=("2026-09-12T10:03:00Z", "2026-09-12T12:03:00+02:00"),
                    results_date_from="2026-09-10",
                    results_date_to="2026-09-12",
                    output_dir=Path(tmp) / "acquisition",
                )
        self.assertEqual(transport.urls, [])

    def test_match_evidence_failure_leaves_no_partial_final_bundle(self) -> None:
        transport = _Transport(fail_matches=True)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "acquisition"
            with self.assertRaisesRegex(ProviderPayloadError, "missing entitlement-window headers"):
                capture_historical_acquisition_bundle(
                    self._provider(transport),
                    requested_at=("2026-09-12T10:03:00Z",),
                    results_date_from="2026-09-10",
                    results_date_to="2026-09-12",
                    output_dir=root,
                )
            self.assertFalse(root.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_existing_output_is_never_overwritten(self) -> None:
        transport = _Transport()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "acquisition"
            root.mkdir()
            marker = root / "keep.txt"
            marker.write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "never overwrite"):
                capture_historical_acquisition_bundle(
                    self._provider(transport),
                    requested_at=("2026-09-12T10:03:00Z",),
                    results_date_from="2026-09-10",
                    results_date_to="2026-09-12",
                    output_dir=root,
                )
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertEqual(transport.urls, [])


if __name__ == "__main__":
    unittest.main()

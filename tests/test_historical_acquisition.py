from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from autosport import historical_acquisition
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
    def __init__(self, *, fail_matches: bool = False, empty_coverage: bool = False) -> None:
        self.fail_matches = fail_matches
        self.empty_coverage = empty_coverage
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []

    def __call__(self, url: str, headers: dict[str, str], timeout: float) -> HttpJsonResponse:
        self.urls.append(url)
        self.headers.append(dict(headers))
        parsed = urlparse(url)
        path = parsed.path
        if path.endswith("/coverage"):
            query = parse_qs(parsed.query)
            date_from = query["dateFrom"][0]
            date_to = query["dateTo"][0]
            by_source = {}
            if not self.empty_coverage:
                by_source = {
                    "test-source": {
                        "rows": 3,
                        "first_date": date_from,
                        "last_date": date_to,
                        "priced_rows": 2,
                    }
                }
            return HttpJsonResponse(
                {
                    "sport_key": "table_tennis",
                    "window": {"date_from": date_from, "date_to": date_to},
                    "by_source": by_source,
                },
                200,
                {
                    "x-api-version": "test",
                    "x-historical-window-hours": "168",
                    "x-historical-window-from": "2026-09-06T00:00:00Z",
                },
            )
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

    def test_bundle_preflights_and_binds_exact_files_without_promoting_truth(self) -> None:
        transport = _Transport()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "acquisition"
            report = capture_historical_acquisition_bundle(
                self._provider(transport),
                requested_at=("2026-09-12T10:03:00Z", "2026-09-12T10:08:00Z"),
                results_date="2026-09-10",
                output_dir=root,
                results_priced_only=True,
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
            self.assertEqual(
                bundle["request_scope"]["coverage_preflight"],
                {"date_from": "2026-09-10", "date_to": "2026-09-12"},
            )
            expected_match_url = (
                "https://parlay-api.com/v1/historical/sports/table_tennis/matches"
                "?date=2026-09-10&pricedOnly=true"
            )
            self.assertEqual(
                bundle["request_scope"]["match_results"],
                {
                    "url": expected_match_url,
                    "date": "2026-09-10",
                    "priced_only": True,
                },
            )
            self.assertEqual(bundle["match_results"]["requested_date"], "2026-09-10")
            self.assertTrue(bundle["match_results"]["priced_only"])
            self.assertEqual(bundle["match_results"]["request_url"], expected_match_url)
            self.assertFalse(bundle["match_results"]["product_owned_request_path_verified"])
            self.assertFalse(bundle["match_results"]["product_owned_acquisition_clock_verified"])
            self.assertFalse(bundle["match_results"]["provider_response_origin_verified"])
            self.assertFalse(bundle["match_results"]["trusted_outcome_source_admissible"])
            self.assertFalse(bundle["match_result_product_owned_request_path_verified"])
            self.assertFalse(bundle["match_result_product_owned_acquisition_clock_verified"])
            self.assertFalse(bundle["match_result_provider_response_origin_verified"])
            self.assertFalse(bundle["trusted_outcome_source_admissible"])
            coverage = bundle["match_results"]["coverage_preflight"]
            self.assertEqual(coverage["date_from"], "2026-09-10")
            self.assertEqual(coverage["date_to"], "2026-09-12")
            self.assertEqual(coverage["source_count"], 1)
            self.assertEqual(coverage["total_rows"], 3)
            self.assertEqual(coverage["total_priced_rows"], 2)
            self.assertFalse(coverage["historical_window_market_coverage_verified"])
            self.assertFalse(coverage["licensing_or_retention_verified"])
            self.assertFalse(coverage["redistribution_verified"])
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

        self.assertEqual(len(transport.urls), 4)
        self.assertTrue(all(headers["X-API-Key"] == "unit-test-key" for headers in transport.headers))
        self.assertTrue(urlparse(transport.urls[0]).path.endswith("/coverage"))
        coverage_query = parse_qs(urlparse(transport.urls[0]).query)
        self.assertEqual(coverage_query["dateFrom"], ["2026-09-10"])
        self.assertEqual(coverage_query["dateTo"], ["2026-09-12"])
        match_url = next(url for url in transport.urls if urlparse(url).path.endswith("/matches"))
        match_query = parse_qs(urlparse(match_url).query)
        self.assertEqual(set(match_query), {"date", "pricedOnly"})
        self.assertEqual(match_query["date"], ["2026-09-10"])
        self.assertEqual(match_query["pricedOnly"], ["true"])
        self.assertNotIn("unit-test-key", match_url)

    def test_custom_match_origins_change_bundle_request_and_evidence_identity(self) -> None:
        reports: list[tuple[str, str, str]] = []
        with tempfile.TemporaryDirectory() as tmp:
            for index, base_url in enumerate(
                ("https://origin-a.example", "https://origin-b.example"),
                start=1,
            ):
                transport = _Transport()
                provider = ParlayApiTableTennisProvider(
                    "unit-test-key",
                    base_url=base_url,
                    transport=transport,
                    clock=lambda: "2026-09-13T03:00:00+00:00",
                    sleeper=lambda _: None,
                )
                root = Path(tmp) / f"acquisition-{index}"
                report = capture_historical_acquisition_bundle(
                    provider,
                    requested_at=("2026-09-12T10:03:00Z",),
                    results_date="2026-09-10",
                    output_dir=root,
                )
                bundle = json.loads((root / "bundle.json").read_text(encoding="utf-8"))
                request_url = bundle["request_scope"]["match_results"]["url"]
                self.assertEqual(request_url, bundle["match_results"]["request_url"])
                self.assertFalse(bundle["match_result_product_owned_request_path_verified"])
                self.assertFalse(bundle["match_result_product_owned_acquisition_clock_verified"])
                reports.append((request_url, report.request_identity, report.evidence_identity))

        self.assertNotEqual(reports[0][0], reports[1][0])
        self.assertNotEqual(reports[0][1], reports[1][1])
        self.assertNotEqual(reports[0][2], reports[1][2])

    def test_replaced_match_capture_fails_before_bundle_publication(self) -> None:
        transport = _Transport()
        real_capture = historical_acquisition.capture_historical_matches

        def replace_result_capture(*args, **kwargs):
            report = real_capture(*args, **kwargs)
            Path(kwargs["output_path"]).write_text('{"tampered":true}\n', encoding="utf-8")
            return report

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "acquisition"
            with patch.object(
                historical_acquisition,
                "capture_historical_matches",
                side_effect=replace_result_capture,
            ):
                with self.assertRaisesRegex(
                    ProviderPayloadError,
                    "match_results.evidence capture_sha256 does not bind staged capture bytes",
                ):
                    capture_historical_acquisition_bundle(
                        self._provider(transport),
                        requested_at=("2026-09-12T10:03:00Z",),
                        results_date="2026-09-10",
                        output_dir=root,
                    )
            self.assertFalse(root.exists())

    def test_returned_match_report_cannot_override_hashed_evidence_semantics(self) -> None:
        transport = _Transport()
        real_capture = historical_acquisition.capture_historical_matches

        def forge_returned_report(*args, **kwargs):
            report = real_capture(*args, **kwargs)
            return replace(
                report,
                requested_date="2026-09-09",
                request_url="https://attacker.invalid/forged",
                provider_response_origin_verified=True,
                trusted_outcome_source_admissible=True,
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "acquisition"
            with patch.object(
                historical_acquisition,
                "capture_historical_matches",
                side_effect=forge_returned_report,
            ):
                with self.assertRaisesRegex(
                    ProviderPayloadError,
                    "returned child report does not match staged evidence",
                ):
                    capture_historical_acquisition_bundle(
                        self._provider(transport),
                        requested_at=("2026-09-12T10:03:00Z",),
                        results_date="2026-09-10",
                        output_dir=root,
                    )
            self.assertFalse(root.exists())

    def test_mutated_match_evidence_ordinary_semantics_fail_closed(self) -> None:
        transport = _Transport()
        real_capture = historical_acquisition.capture_historical_matches

        def mutate_evidence_after_child(*args, **kwargs):
            report = real_capture(*args, **kwargs)
            evidence_path = Path(kwargs["evidence_path"])
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["canonical_response_sha256"] = "0" * 64
            evidence["captured_at"] = "2026-09-13T03:01:00+00:00"
            evidence_path.write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return report

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "acquisition"
            with patch.object(
                historical_acquisition,
                "capture_historical_matches",
                side_effect=mutate_evidence_after_child,
            ):
                with self.assertRaisesRegex(
                    ProviderPayloadError,
                    "capture/evidence semantic mismatch|returned child report does not match staged evidence",
                ):
                    capture_historical_acquisition_bundle(
                        self._provider(transport),
                        requested_at=("2026-09-12T10:03:00Z",),
                        results_date="2026-09-10",
                        output_dir=root,
                    )
            self.assertFalse(root.exists())

    def test_mutated_match_evidence_cannot_promote_bundle_trust(self) -> None:
        transport = _Transport()
        real_capture = historical_acquisition.capture_historical_matches

        def mutate_evidence_after_child(*args, **kwargs):
            report = real_capture(*args, **kwargs)
            evidence_path = Path(kwargs["evidence_path"])
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["provider_response_origin_verified"] = True
            evidence["trusted_outcome_source_admissible"] = True
            evidence_path.write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return report

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "acquisition"
            with patch.object(
                historical_acquisition,
                "capture_historical_matches",
                side_effect=mutate_evidence_after_child,
            ):
                with self.assertRaisesRegex(
                    ProviderPayloadError,
                    "provider_response_origin_verified must remain false",
                ):
                    capture_historical_acquisition_bundle(
                        self._provider(transport),
                        requested_at=("2026-09-12T10:03:00Z",),
                        results_date="2026-09-10",
                        output_dir=root,
                    )
            self.assertFalse(root.exists())

    def test_mutated_child_evidence_cannot_claim_parsed_result_authority(self) -> None:
        transport = _Transport()
        real_capture = historical_acquisition.capture_historical_matches

        def mutate_evidence_after_child(*args, **kwargs):
            report = real_capture(*args, **kwargs)
            evidence_path = Path(kwargs["evidence_path"])
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["provider_result_schema_parsed"] = True
            evidence_path.write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return report

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "acquisition"
            with patch.object(
                historical_acquisition,
                "capture_historical_matches",
                side_effect=mutate_evidence_after_child,
            ):
                with self.assertRaisesRegex(
                    ProviderPayloadError,
                    "provider_result_schema_parsed must remain false",
                ):
                    capture_historical_acquisition_bundle(
                        self._provider(transport),
                        requested_at=("2026-09-12T10:03:00Z",),
                        results_date="2026-09-10",
                        output_dir=root,
                    )
            self.assertFalse(root.exists())

    def test_returned_snapshot_report_cannot_override_hashed_evidence_semantics(self) -> None:
        transport = _Transport()
        real_capture = historical_acquisition.capture_historical_snapshot

        def forge_returned_report(*args, **kwargs):
            report = real_capture(*args, **kwargs)
            return replace(
                report,
                requested_at="2026-09-11T00:00:00Z",
                snapshot_at="2026-09-11T00:00:00Z",
                quote_count=0,
                market_sha256="0" * 64,
            )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "acquisition"
            with patch.object(
                historical_acquisition,
                "capture_historical_snapshot",
                side_effect=forge_returned_report,
            ):
                with self.assertRaisesRegex(
                    ProviderPayloadError,
                    "returned child report does not match staged evidence",
                ):
                    capture_historical_acquisition_bundle(
                        self._provider(transport),
                        requested_at=("2026-09-12T10:03:00Z",),
                        results_date="2026-09-10",
                        output_dir=root,
                    )
            self.assertFalse(root.exists())

    def test_mutated_snapshot_evidence_quote_count_fails_closed(self) -> None:
        transport = _Transport()
        real_capture = historical_acquisition.capture_historical_snapshot

        def mutate_evidence_after_child(*args, **kwargs):
            report = real_capture(*args, **kwargs)
            evidence_path = Path(kwargs["evidence_path"])
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["quote_count"] = 1
            evidence["has_data"] = True
            evidence["point_in_time_snapshot_contains_odds"] = True
            evidence_path.write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return report

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "acquisition"
            with patch.object(
                historical_acquisition,
                "capture_historical_snapshot",
                side_effect=mutate_evidence_after_child,
            ):
                with self.assertRaisesRegex(
                    ProviderPayloadError,
                    "returned child report does not match staged evidence|row count does not match staged child evidence",
                ):
                    capture_historical_acquisition_bundle(
                        self._provider(transport),
                        requested_at=("2026-09-12T10:03:00Z",),
                        results_date="2026-09-10",
                        output_dir=root,
                    )
            self.assertFalse(root.exists())

    def test_mutated_snapshot_evidence_cannot_promote_coverage_truth(self) -> None:
        transport = _Transport()
        real_capture = historical_acquisition.capture_historical_snapshot

        def mutate_evidence_after_child(*args, **kwargs):
            report = real_capture(*args, **kwargs)
            evidence_path = Path(kwargs["evidence_path"])
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["point_in_time_odds_market_coverage_verified"] = True
            evidence_path.write_text(
                json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            return report

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "acquisition"
            with patch.object(
                historical_acquisition,
                "capture_historical_snapshot",
                side_effect=mutate_evidence_after_child,
            ):
                with self.assertRaisesRegex(
                    ProviderPayloadError,
                    "point_in_time_odds_market_coverage_verified must remain false",
                ):
                    capture_historical_acquisition_bundle(
                        self._provider(transport),
                        requested_at=("2026-09-12T10:03:00Z",),
                        results_date="2026-09-10",
                        output_dir=root,
                    )
            self.assertFalse(root.exists())

    def test_replaced_snapshot_market_fails_before_bundle_publication(self) -> None:
        transport = _Transport()
        real_capture = historical_acquisition.capture_historical_snapshot

        def replace_snapshot_market(*args, **kwargs):
            report = real_capture(*args, **kwargs)
            Path(kwargs["output_path"]).write_text('{"tampered":true}\n', encoding="utf-8")
            return report

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "acquisition"
            with patch.object(
                historical_acquisition,
                "capture_historical_snapshot",
                side_effect=replace_snapshot_market,
            ):
                with self.assertRaisesRegex(
                    ProviderPayloadError,
                    r"snapshot\[1\]\.market bytes changed after child capture",
                ):
                    capture_historical_acquisition_bundle(
                        self._provider(transport),
                        requested_at=("2026-09-12T10:03:00Z",),
                        results_date="2026-09-10",
                        output_dir=root,
                    )
            self.assertFalse(root.exists())

    def test_equivalent_duplicate_snapshot_instants_fail_before_network(self) -> None:
        transport = _Transport()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "unique instants"):
                capture_historical_acquisition_bundle(
                    self._provider(transport),
                    requested_at=("2026-09-12T10:03:00Z", "2026-09-12T12:03:00+02:00"),
                    results_date="2026-09-10",
                    output_dir=Path(tmp) / "acquisition",
                )
        self.assertEqual(transport.urls, [])

    def test_empty_coverage_fails_before_snapshot_calls_and_leaves_no_bundle(self) -> None:
        transport = _Transport(empty_coverage=True)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "acquisition"
            with self.assertRaisesRegex(ProviderPayloadError, "coverage preflight returned no source rows"):
                capture_historical_acquisition_bundle(
                    self._provider(transport),
                    requested_at=("2026-09-12T10:03:00Z",),
                    results_date="2026-09-10",
                    output_dir=root,
                )
            self.assertFalse(root.exists())
        self.assertEqual(len(transport.urls), 1)
        self.assertTrue(urlparse(transport.urls[0]).path.endswith("/coverage"))

    def test_match_evidence_failure_leaves_no_partial_final_bundle(self) -> None:
        transport = _Transport(fail_matches=True)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "acquisition"
            with self.assertRaisesRegex(ProviderPayloadError, "missing entitlement-window headers"):
                capture_historical_acquisition_bundle(
                    self._provider(transport),
                    requested_at=("2026-09-12T10:03:00Z",),
                    results_date="2026-09-10",
                    output_dir=root,
                )
            self.assertFalse(root.exists())
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_output_created_during_publication_is_not_overwritten(self) -> None:
        transport = _Transport()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "acquisition"
            real_atomic_write_json = historical_acquisition.atomic_write_json

            def create_destination_after_bundle_staging(path, payload):
                result = real_atomic_write_json(path, payload)
                if Path(path).name == "bundle.json":
                    root.mkdir()
                return result

            with patch.object(
                historical_acquisition,
                "atomic_write_json",
                side_effect=create_destination_after_bundle_staging,
            ):
                with self.assertRaisesRegex(ValueError, "appeared during acquisition"):
                    capture_historical_acquisition_bundle(
                        self._provider(transport),
                        requested_at=("2026-09-12T10:03:00Z",),
                        results_date="2026-09-10",
                        output_dir=root,
                    )

            self.assertTrue(root.is_dir())
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(list(Path(tmp).iterdir()), [root])

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
                    results_date="2026-09-10",
                    output_dir=root,
                )
            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
        self.assertEqual(transport.urls, [])


if __name__ == "__main__":
    unittest.main()

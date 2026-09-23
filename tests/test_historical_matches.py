from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from autosport import historical_matches
from autosport.historical_matches import capture_historical_matches
from autosport.parlayapi_provider import (
    HttpJsonResponse,
    ParlayApiTableTennisProvider,
    ProviderPayloadError,
    ProviderTransportError,
)


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
            capture_bytes = output.read_bytes()
            capture = json.loads(capture_bytes.decode("utf-8"))
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

        self.assertEqual(capture["payload"], payload)
        expected_url = (
            "https://parlay-api.com/v1/historical/sports/table_tennis/matches"
            "?date=2026-09-10&pricedOnly=false"
        )
        self.assertEqual(
            capture["request"],
            {"url": expected_url, "date": "2026-09-10", "priced_only": False},
        )
        self.assertEqual(evidence["requested_date"], "2026-09-10")
        self.assertFalse(evidence["priced_only"])
        self.assertEqual(evidence["request_url"], expected_url)
        self.assertEqual(report.request_url, expected_url)
        self.assertEqual(report.capture_sha256, hashlib.sha256(capture_bytes).hexdigest())
        self.assertEqual(evidence["capture_sha256"], report.capture_sha256)
        self.assertEqual(evidence["coverage_hint"], "source=test-source")
        self.assertFalse(capture["trust"]["product_owned_request_path_verified"])
        self.assertFalse(capture["trust"]["product_owned_acquisition_clock_verified"])
        self.assertFalse(capture["trust"]["provider_response_origin_verified"])
        self.assertFalse(capture["trust"]["trusted_outcome_source_admissible"])
        self.assertEqual(
            capture["trust"]["response_origin_limitation"],
            "provider_response_envelope_omits_final_url_and_exact_wire_bytes",
        )
        self.assertFalse(evidence["product_owned_request_path_verified"])
        self.assertFalse(evidence["product_owned_acquisition_clock_verified"])
        self.assertFalse(evidence["provider_response_origin_verified"])
        self.assertFalse(evidence["trusted_outcome_source_admissible"])
        self.assertFalse(report.product_owned_request_path_verified)
        self.assertFalse(report.product_owned_acquisition_clock_verified)
        self.assertFalse(report.provider_response_origin_verified)
        self.assertFalse(report.trusted_outcome_source_admissible)
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

    def test_mutable_default_provider_cannot_mint_positive_transport_or_clock_authority(self) -> None:
        provider = ParlayApiTableTennisProvider("unit-test-key")
        response = HttpJsonResponse(
            [],
            200,
            {
                "x-historical-window-hours": "168",
                "x-historical-window-from": "2026-09-06T00:00:00Z",
            },
        )
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(provider, "_request", return_value=response):
                report = capture_historical_matches(
                    provider,
                    requested_date="2026-09-10",
                    output_path=Path(temp) / "matches.json",
                )
        self.assertFalse(report.product_owned_request_path_verified)
        self.assertFalse(report.product_owned_acquisition_clock_verified)
        self.assertFalse(report.provider_response_origin_verified)
        self.assertFalse(report.trusted_outcome_source_admissible)

    def test_sport_scope_mutation_during_request_fails_before_publication(self) -> None:
        provider = ParlayApiTableTennisProvider(
            "unit-test-key",
            clock=lambda: "2026-09-13T03:00:00+00:00",
        )
        response = HttpJsonResponse(
            [],
            200,
            {
                "x-historical-window-hours": "168",
                "x-historical-window-from": "2026-09-06T00:00:00Z",
            },
        )

        def mutate_sport(_url: str) -> HttpJsonResponse:
            provider.sport_key = "football"
            return response

        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "matches.json"
            evidence = Path(temp) / "matches.evidence.json"
            with patch.object(provider, "_request", side_effect=mutate_sport):
                with self.assertRaisesRegex(ProviderPayloadError, "sport_key changed"):
                    capture_historical_matches(
                        provider,
                        requested_date="2026-09-10",
                        output_path=output,
                        evidence_path=evidence,
                    )
            self.assertFalse(output.exists())
            self.assertFalse(evidence.exists())

    def test_noncanonical_initial_sport_scope_fails_before_network(self) -> None:
        transport = _Transport([])
        provider = self._provider(transport)
        provider.sport_key = "football"
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ProviderPayloadError, "canonical table-tennis sport scope"):
                capture_historical_matches(
                    provider,
                    requested_date="2026-09-10",
                    output_path=Path(temp) / "matches.json",
                )
        self.assertEqual(transport.urls, [])

    def test_retry_transport_and_clock_mutation_cannot_mint_positive_trust(self) -> None:
        payload = [{"provider_defined_id": "retry-match"}]
        second_transport = _Transport(payload)
        attempts: list[str] = []

        def first_transport(url: str, headers: dict[str, str], timeout: float) -> HttpJsonResponse:
            attempts.append(url)
            raise ProviderTransportError("retryable", status_code=500)

        provider = ParlayApiTableTennisProvider(
            "unit-test-key",
            transport=first_transport,
            clock=lambda: "2026-09-13T02:59:59+00:00",
            max_attempts=2,
            sleeper=lambda _: None,
        )

        def mutate_before_retry(_: float) -> None:
            provider.transport = second_transport
            provider.clock = lambda: "2026-09-13T03:00:00+00:00"

        provider.sleeper = mutate_before_retry
        with tempfile.TemporaryDirectory() as temp:
            report = capture_historical_matches(
                provider,
                requested_date="2026-09-10",
                output_path=Path(temp) / "matches.json",
            )

        expected_url = (
            "https://parlay-api.com/v1/historical/sports/table_tennis/matches"
            "?date=2026-09-10&pricedOnly=false"
        )
        self.assertEqual(attempts, [expected_url])
        self.assertEqual(second_transport.urls, [expected_url])
        self.assertEqual(report.request_url, expected_url)
        self.assertEqual(report.captured_at, "2026-09-13T03:00:00+00:00")
        self.assertFalse(report.product_owned_request_path_verified)
        self.assertFalse(report.product_owned_acquisition_clock_verified)
        self.assertFalse(report.provider_response_origin_verified)
        self.assertFalse(report.trusted_outcome_source_admissible)

    def test_custom_origins_with_identical_response_do_not_alias_provenance(self) -> None:
        payload = [{"provider_defined_id": "same-match", "opaque": {"value": 1}}]
        captures: list[tuple[str, str, str, dict[str, object]]] = []
        with tempfile.TemporaryDirectory() as temp:
            for index, base_url in enumerate(
                ("https://origin-a.example", "https://origin-b.example"),
                start=1,
            ):
                transport = _Transport(payload)
                provider = ParlayApiTableTennisProvider(
                    "unit-test-key",
                    base_url=base_url,
                    transport=transport,
                    clock=lambda: "2026-09-13T03:00:00+00:00",
                    sleeper=lambda _: None,
                )
                output = Path(temp) / f"matches-{index}.json"
                evidence_path = Path(temp) / f"matches-{index}.evidence.json"
                report = capture_historical_matches(
                    provider,
                    requested_date="2026-09-10",
                    output_path=output,
                    evidence_path=evidence_path,
                )
                evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
                captures.append(
                    (
                        report.request_url,
                        report.capture_sha256,
                        report.canonical_response_sha256,
                        evidence,
                    )
                )
                self.assertFalse(report.product_owned_request_path_verified)
                self.assertFalse(report.product_owned_acquisition_clock_verified)
                self.assertFalse(report.provider_response_origin_verified)
                self.assertFalse(report.trusted_outcome_source_admissible)

        self.assertNotEqual(captures[0][0], captures[1][0])
        self.assertNotEqual(captures[0][1], captures[1][1])
        self.assertEqual(captures[0][2], captures[1][2])
        self.assertNotEqual(captures[0][3]["request_url"], captures[1][3]["request_url"])

    def test_request_provenance_rejects_secret_bearing_base_url_before_network(self) -> None:
        transport = _Transport([])
        provider = ParlayApiTableTennisProvider(
            "unit-test-key",
            base_url="https://user:secret@example.invalid",
            transport=transport,
            clock=lambda: "2026-09-13T03:00:00+00:00",
            sleeper=lambda _: None,
        )
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "must not contain credentials"):
                capture_historical_matches(
                    provider,
                    requested_date="2026-09-10",
                    output_path=Path(temp) / "matches.json",
                )
        self.assertEqual(transport.urls, [])

    def test_custom_base_paths_are_rejected_without_persisting_path_secrets(self) -> None:
        for sentinel in ("TENANT-SECRET-A", "TENANT-SECRET-B"):
            with self.subTest(sentinel=sentinel):
                transport = _Transport([])
                provider = ParlayApiTableTennisProvider(
                    "unit-test-key",
                    base_url=f"https://proxy.example/tenant/{sentinel}",
                    transport=transport,
                    clock=lambda: "2026-09-13T03:00:00+00:00",
                    sleeper=lambda _: None,
                )
                with tempfile.TemporaryDirectory() as temp:
                    output = Path(temp) / "matches.json"
                    evidence = Path(temp) / "matches.evidence.json"
                    with self.assertRaisesRegex(ValueError, "origin-only provider base_url"):
                        capture_historical_matches(
                            provider,
                            requested_date="2026-09-10",
                            output_path=output,
                            evidence_path=evidence,
                        )
                    self.assertFalse(output.exists())
                    self.assertFalse(evidence.exists())
                    self.assertNotIn(sentinel, "\n".join(path.name for path in Path(temp).iterdir()))
                self.assertEqual(transport.urls, [])

    def test_capture_replacement_during_pair_publication_fails_closed(self) -> None:
        payload = [{"provider_defined_id": "match-1", "opaque": {"value": 1}}]
        transport = _Transport(payload)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "matches.json"
            evidence_path = Path(temp) / "matches.evidence.json"
            real_atomic_write_json = historical_matches.atomic_write_json

            def replace_output_before_evidence(path, evidence_payload):
                if Path(path) == evidence_path:
                    output.write_bytes(b'{"foreign":"replacement"}\n')
                return real_atomic_write_json(path, evidence_payload)

            with patch.object(
                historical_matches,
                "atomic_write_json",
                side_effect=replace_output_before_evidence,
            ):
                with self.assertRaisesRegex(
                    ProviderPayloadError,
                    "capture bytes changed during pair publication",
                ):
                    capture_historical_matches(
                        self._provider(transport),
                        requested_date="2026-09-10",
                        output_path=output,
                        evidence_path=evidence_path,
                    )

            self.assertEqual(output.read_bytes(), b'{"foreign":"replacement"}\n')
            self.assertFalse(evidence_path.exists())

    def test_evidence_replacement_during_pair_publication_fails_closed(self) -> None:
        payload = [{"provider_defined_id": "match-1", "opaque": {"value": 1}}]
        transport = _Transport(payload)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "matches.json"
            evidence_path = Path(temp) / "matches.evidence.json"
            real_atomic_write_json = historical_matches.atomic_write_json

            def replace_evidence_after_write(path, evidence_payload):
                result = real_atomic_write_json(path, evidence_payload)
                if Path(path) == evidence_path:
                    evidence_path.write_bytes(b'{"foreign":"replacement"}\n')
                return result

            with patch.object(
                historical_matches,
                "atomic_write_json",
                side_effect=replace_evidence_after_write,
            ):
                with self.assertRaisesRegex(
                    ProviderPayloadError,
                    "evidence bytes changed during pair publication",
                ):
                    capture_historical_matches(
                        self._provider(transport),
                        requested_date="2026-09-10",
                        output_path=output,
                        evidence_path=evidence_path,
                    )

            self.assertFalse(output.exists())
            self.assertEqual(evidence_path.read_bytes(), b'{"foreign":"replacement"}\n')

    def test_evidence_write_failure_removes_owned_uncommitted_capture(self) -> None:
        payload = [{"provider_defined_id": "match-1", "opaque": {"value": 1}}]
        transport = _Transport(payload)
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "matches.json"
            evidence_path = Path(temp) / "matches.evidence.json"

            with patch.object(
                historical_matches,
                "atomic_write_json",
                side_effect=OSError("evidence publication failed"),
            ):
                with self.assertRaisesRegex(OSError, "evidence publication failed"):
                    capture_historical_matches(
                        self._provider(transport),
                        requested_date="2026-09-10",
                        output_path=output,
                        evidence_path=evidence_path,
                    )

            self.assertFalse(output.exists())
            self.assertFalse(evidence_path.exists())

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

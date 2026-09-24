from __future__ import annotations

from contextlib import contextmanager
import hashlib
import http.client as http_client
import json
import urllib.request as urllib_request
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import HTTPSHandler, OpenerDirector

import autosport.historical_snapshot as historical_snapshot
from autosport.historical_snapshot import (
    _atomic_write_jsonl,
    assert_historical_snapshot_provider_origin,
    capture_historical_snapshot,
    capture_product_owned_historical_snapshot,
)
from autosport.parlayapi_provider import (
    HttpJsonResponse,
    ParlayApiTableTennisProvider,
    ProviderPayloadError,
)


class _Transport:
    def __init__(
        self,
        payload: object,
        *,
        response_headers: dict[str, str] | None = None,
        status_code: int = 200,
    ) -> None:
        self.payload = payload
        self.response_headers = (
            {"x-api-version": "test"}
            if response_headers is None
            else dict(response_headers)
        )
        self.status_code = status_code
        self.urls: list[str] = []
        self.headers: list[dict[str, str]] = []

    def __call__(
        self,
        url: str,
        headers: dict[str, str],
        timeout: float,
    ) -> HttpJsonResponse:
        self.urls.append(url)
        self.headers.append(dict(headers))
        return HttpJsonResponse(
            self.payload,
            self.status_code,
            dict(self.response_headers),
        )


def _payload(
    *,
    timestamp: str = "2026-09-12T10:00:00Z",
    last_update: str | None = None,
    event_id: str = "tt-1",
) -> dict[str, object]:
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
                "id": event_id,
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

    def test_injected_transport_capture_cannot_issue_provider_origin_authority(self) -> None:
        transport = _Transport(_payload())
        provider = self._provider(transport)
        with tempfile.TemporaryDirectory() as temp:
            report = capture_historical_snapshot(
                provider,
                requested_at="2026-09-12T10:03:00Z",
                output_path=Path(temp) / "market.jsonl",
                evidence_path=Path(temp) / "evidence.json",
            )

        with self.assertRaisesRegex(
            ProviderPayloadError,
            "not issued by canonical product-owned Parlay acquisition",
        ):
            assert_historical_snapshot_provider_origin(report)


    def test_product_owned_capture_rejects_precall_opener_open_rebind(self) -> None:
        forged_calls: list[str] = []

        def forged_open(opener, request, timeout=None):
            del opener, timeout
            forged_calls.append(request.full_url)
            raise AssertionError("forged opener dispatch must not run")

        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            OpenerDirector,
            "open",
            forged_open,
        ):
            output_path = Path(temp) / "market.jsonl"
            evidence_path = Path(temp) / "evidence.json"
            with self.assertRaisesRegex(
                ProviderPayloadError,
                "network dispatch changed before construction",
            ):
                capture_product_owned_historical_snapshot(
                    api_key="secret-key-must-not-leak",
                    requested_at="2026-09-12T10:03:00Z",
                    output_path=output_path,
                    evidence_path=evidence_path,
                )
            self.assertFalse(output_path.exists())
            self.assertFalse(evidence_path.exists())

        self.assertEqual(forged_calls, [])

    def test_product_owned_capture_rejects_precall_opener_internal_rebind(self) -> None:
        forged_calls: list[str] = []

        def forged_internal_open(opener, request, data=None):
            del opener, data
            forged_calls.append(request.full_url)
            raise AssertionError("forged internal opener dispatch must not run")

        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            OpenerDirector,
            "_open",
            forged_internal_open,
        ):
            output_path = Path(temp) / "market.jsonl"
            evidence_path = Path(temp) / "evidence.json"
            with self.assertRaisesRegex(
                ProviderPayloadError,
                "network dispatch changed before construction",
            ):
                capture_product_owned_historical_snapshot(
                    api_key="secret-key-must-not-leak",
                    requested_at="2026-09-12T10:03:00Z",
                    output_path=output_path,
                    evidence_path=evidence_path,
                )
            self.assertFalse(output_path.exists())
            self.assertFalse(evidence_path.exists())

        self.assertEqual(forged_calls, [])

    def test_product_owned_capture_rejects_precall_https_open_rebind(self) -> None:
        forged_calls: list[str] = []

        def forged_https_open(handler, request):
            del handler
            forged_calls.append(request.full_url)
            raise AssertionError("forged HTTPS dispatch must not run")

        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            HTTPSHandler,
            "https_open",
            forged_https_open,
        ):
            output_path = Path(temp) / "market.jsonl"
            evidence_path = Path(temp) / "evidence.json"
            with self.assertRaisesRegex(
                ProviderPayloadError,
                "network dispatch changed before construction",
            ):
                capture_product_owned_historical_snapshot(
                    api_key="secret-key-must-not-leak",
                    requested_at="2026-09-12T10:03:00Z",
                    output_path=output_path,
                    evidence_path=evidence_path,
                )
            self.assertFalse(output_path.exists())
            self.assertFalse(evidence_path.exists())

        self.assertEqual(forged_calls, [])

    def test_product_owned_capture_rejects_precall_https_connection_rebind(self) -> None:
        forged_calls: list[str] = []

        class ForgedHTTPSConnection:
            def __init__(self, *args, **kwargs) -> None:
                del args, kwargs
                forged_calls.append("init")

        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            http_client,
            "HTTPSConnection",
            ForgedHTTPSConnection,
        ):
            output_path = Path(temp) / "market.jsonl"
            evidence_path = Path(temp) / "evidence.json"
            with self.assertRaisesRegex(
                ProviderPayloadError,
                "network dispatch changed before construction",
            ):
                capture_product_owned_historical_snapshot(
                    api_key="secret-key-must-not-leak",
                    requested_at="2026-09-12T10:03:00Z",
                    output_path=output_path,
                    evidence_path=evidence_path,
                )
            self.assertFalse(output_path.exists())
            self.assertFalse(evidence_path.exists())

        self.assertEqual(forged_calls, [])

    def test_product_owned_capture_rejects_request_init_transient_connection_rebind(self) -> None:
        original_request_init = urllib_request.Request.__init__
        original_https_connection = http_client.HTTPSConnection
        request_init_calls: list[str] = []
        forged_connection_calls: list[str] = []

        class ForgedHeaders:
            def items(self):
                return [("Content-Type", "application/json")]

            def get(self, _name, default=None):
                return default

        class ForgedResponse:
            def __init__(self) -> None:
                self.status = 200
                self.reason = "OK"
                self.headers = ForgedHeaders()
                self.url = None
                self.msg = "OK"
                self._body = json.dumps(_payload()).encode("utf-8")
                self._read = False

            def read(self) -> bytes:
                if self._read:
                    return b""
                self._read = True
                return self._body

            def geturl(self):
                return self.url

            def close(self) -> None:
                return None

            def __enter__(self):
                return self

            def __exit__(self, *_args) -> None:
                self.close()

        class ForgedHTTPSConnection:
            def __init__(self, *args, **kwargs) -> None:
                del args, kwargs
                forged_connection_calls.append("init")
                self.sock = None
                # This models the transient bypass: the genuine HTTPSHandler has
                # already selected this class, so restoring the checked module
                # global here makes the later authority check look canonical.
                http_client.HTTPSConnection = original_https_connection

            def set_debuglevel(self, _level) -> None:
                return None

            def request(self, *_args, **_kwargs) -> None:
                return None

            def getresponse(self):
                return ForgedResponse()

            def close(self) -> None:
                return None

        def forged_request_init(request, *args, **kwargs):
            original_request_init(request, *args, **kwargs)
            request_init_calls.append("request-init")
            http_client.HTTPSConnection = ForgedHTTPSConnection

        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            urllib_request.Request,
            "__init__",
            forged_request_init,
        ):
            output_path = Path(temp) / "market.jsonl"
            evidence_path = Path(temp) / "evidence.json"
            with self.assertRaisesRegex(
                ProviderPayloadError,
                "network dispatch changed before construction",
            ):
                capture_product_owned_historical_snapshot(
                    api_key="secret-key-must-not-leak",
                    requested_at="2026-09-12T10:03:00Z",
                    output_path=output_path,
                    evidence_path=evidence_path,
                )
            self.assertFalse(output_path.exists())
            self.assertFalse(evidence_path.exists())

        # The Request executable graph is now part of the import-time trust root,
        # so the mutating wrapper must be rejected before it can install the
        # transient forged HTTPS connection.
        self.assertEqual(request_init_calls, [])
        self.assertEqual(forged_connection_calls, [])
        self.assertIs(http_client.HTTPSConnection, original_https_connection)

    def test_product_owned_capture_rejects_https_connection_method_drift(self) -> None:
        forged_calls: list[str] = []

        for method_name in ("request", "getresponse", "connect"):
            with self.subTest(method_name=method_name):
                def forged_method(*args, _method_name=method_name, **kwargs):
                    del args, kwargs
                    forged_calls.append(_method_name)
                    raise AssertionError("forged HTTPS connection dispatch must not run")

                with tempfile.TemporaryDirectory() as temp, mock.patch.object(
                    http_client.HTTPSConnection,
                    method_name,
                    forged_method,
                ):
                    output_path = Path(temp) / "market.jsonl"
                    evidence_path = Path(temp) / "evidence.json"
                    with self.assertRaisesRegex(
                        ProviderPayloadError,
                        "network dispatch changed before construction",
                    ):
                        capture_product_owned_historical_snapshot(
                            api_key="secret-key-must-not-leak",
                            requested_at="2026-09-12T10:03:00Z",
                            output_path=output_path,
                            evidence_path=evidence_path,
                        )
                    self.assertFalse(output_path.exists())
                    self.assertFalse(evidence_path.exists())

        self.assertEqual(forged_calls, [])

    def test_product_owned_capture_rejects_urllib_http_global_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            urllib_request,
            "http",
            object(),
        ):
            output_path = Path(temp) / "market.jsonl"
            evidence_path = Path(temp) / "evidence.json"
            with self.assertRaisesRegex(
                ProviderPayloadError,
                "network dispatch changed before construction",
            ):
                capture_product_owned_historical_snapshot(
                    api_key="secret-key-must-not-leak",
                    requested_at="2026-09-12T10:03:00Z",
                    output_path=output_path,
                    evidence_path=evidence_path,
                )
            self.assertFalse(output_path.exists())
            self.assertFalse(evidence_path.exists())

    def test_product_owned_capture_rejects_http_response_class_rebind(self) -> None:
        class ForgedResponse:
            pass

        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            http_client.HTTPConnection,
            "response_class",
            ForgedResponse,
        ):
            output_path = Path(temp) / "market.jsonl"
            evidence_path = Path(temp) / "evidence.json"
            with self.assertRaisesRegex(
                ProviderPayloadError,
                "network dispatch changed before construction",
            ):
                capture_product_owned_historical_snapshot(
                    api_key="secret-key-must-not-leak",
                    requested_at="2026-09-12T10:03:00Z",
                    output_path=output_path,
                    evidence_path=evidence_path,
                )
            self.assertFalse(output_path.exists())
            self.assertFalse(evidence_path.exists())

    def test_product_owned_capture_rejects_live_constructor_rebind_before_io(self) -> None:
        calls: list[str] = []

        def forged_init(provider, *args, **kwargs):
            calls.append("forged-init")

        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            ParlayApiTableTennisProvider,
            "__init__",
            forged_init,
        ):
            with self.assertRaisesRegex(
                ProviderPayloadError,
                "authority changed before acquisition",
            ):
                capture_product_owned_historical_snapshot(
                    api_key="secret-key-must-not-leak",
                    requested_at="2026-09-12T10:03:00Z",
                    output_path=Path(temp) / "market.jsonl",
                    evidence_path=Path(temp) / "evidence.json",
                )

        self.assertEqual(calls, [])


    def test_product_owned_capture_rejects_provider_dispatch_drift_before_io(self) -> None:
        calls: list[str] = []

        def forged_request(provider, url):
            calls.append(url)
            return HttpJsonResponse(_payload(), 200, {"X-API-Version": "forged"})

        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            ParlayApiTableTennisProvider,
            "_request",
            forged_request,
        ):
            with self.assertRaisesRegex(
                ProviderPayloadError,
                "authority changed before acquisition",
            ):
                capture_product_owned_historical_snapshot(
                    api_key="secret-key-must-not-leak",
                    requested_at="2026-09-12T10:03:00Z",
                    output_path=Path(temp) / "market.jsonl",
                    evidence_path=Path(temp) / "evidence.json",
                )

        self.assertEqual(calls, [])


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
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertEqual(row["source_ts"], "2026-09-12T10:00:00Z")
            self.assertEqual(row["observed_ts"], "2026-09-12T10:00:00Z")
            self.assertEqual(row["ingest_ts"], "2026-09-13T02:00:00+00:00")
            self.assertEqual(row["metadata"]["source_time_semantics"], "provider_historical_snapshot_timestamp")
            self.assertFalse(row["metadata"]["provider_quote_last_update_present"])
        self.assertTrue(evidence["point_in_time_snapshot_contains_odds"])
        self.assertFalse(evidence["point_in_time_odds_market_coverage_verified"])
        self.assertFalse(evidence["historical_window_market_coverage_verified"])
        self.assertFalse(evidence["sealed_outcomes_present"])
        self.assertFalse(evidence["replay_corpus_ready"])
        self.assertFalse(evidence["licensing_or_retention_verified"])
        self.assertFalse(evidence["real_money_execution"])
        acquisition = evidence["acquisition_provenance"]
        self.assertEqual(acquisition["schema_version"], 1)
        self.assertEqual(
            acquisition["kind"],
            "parlayapi_point_in_time_historical_acquisition",
        )
        self.assertEqual(acquisition["product_kind"], "POINT_IN_TIME_ODDS")
        self.assertEqual(acquisition["http_status"], 200)
        self.assertTrue(acquisition["canonical_response_payload_bound"])
        self.assertFalse(acquisition["raw_response_bytes_bound"])
        self.assertFalse(acquisition["provider_origin_authority_persisted"])
        self.assertTrue(acquisition["provider_origin_requires_live_product_capture"])
        self.assertEqual(
            acquisition["response_payload_sha256"],
            evidence["response_sha256"],
        )
        request = acquisition["request"]
        self.assertEqual(request["method"], "GET")
        self.assertEqual(request["origin"], "https://parlay-api.com")
        self.assertEqual(
            request["endpoint_path"],
            "/v1/historical/sports/table_tennis/odds",
        )
        self.assertEqual(request["query"]["date"], "2026-09-12T10:03:00Z")
        self.assertEqual(request["query"]["markets"], "h2h,spreads,totals")
        self.assertFalse(request["request_url_persisted"])
        self.assertFalse(request["request_credentials_persisted"])
        self.assertEqual(
            request["request_url_sha256"],
            hashlib.sha256(transport.urls[0].encode("utf-8")).hexdigest(),
        )
        self.assertEqual(acquisition["response_headers"]["x-api-version"], "test")
        self.assertIsNone(
            acquisition["response_headers"]["x-api-release-date"]
        )
        self.assertEqual(
            evidence["acquisition_sha256"],
            historical_snapshot._canonical_sha256(acquisition),
        )
        serialized = json.dumps(evidence) + json.dumps(rows)
        self.assertNotIn("secret-key-must-not-leak", serialized)
        self.assertEqual(transport.headers[0]["X-API-Key"], "secret-key-must-not-leak")
        query = parse_qs(urlparse(transport.urls[0]).query)
        self.assertEqual(query["date"], ["2026-09-12T10:03:00Z"])
        self.assertEqual(query["markets"], ["h2h,spreads,totals"])
        self.assertEqual(query["oddsFormat"], ["decimal"])

    def test_acquisition_identity_binds_response_version_headers_without_secrets(self) -> None:
        payload = _payload()
        headers_a = {
            "X-API-Version": "3.2.0",
            "X-API-Release-Date": "2026-09-01",
            "Deprecation": "false",
            "X-Historical-Window-Hours": "720",
            "X-Markets-Unservable": "outrights",
            "Cache-Control": "private, max-age=60",
            "Authorization": "response-secret-must-not-persist",
        }
        headers_b = dict(headers_a)
        headers_b["X-API-Version"] = "3.2.1"
        provider_a = self._provider(
            _Transport(payload, response_headers=headers_a)
        )
        provider_b = self._provider(
            _Transport(payload, response_headers=headers_b)
        )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            evidence_a_path = root / "a.evidence.json"
            evidence_b_path = root / "b.evidence.json"
            capture_historical_snapshot(
                provider_a,
                requested_at="2026-09-12T10:03:00Z",
                output_path=root / "a.jsonl",
                evidence_path=evidence_a_path,
            )
            capture_historical_snapshot(
                provider_b,
                requested_at="2026-09-12T10:03:00Z",
                output_path=root / "b.jsonl",
                evidence_path=evidence_b_path,
            )
            evidence_a = json.loads(evidence_a_path.read_text(encoding="utf-8"))
            evidence_b = json.loads(evidence_b_path.read_text(encoding="utf-8"))

        self.assertEqual(evidence_a["response_sha256"], evidence_b["response_sha256"])
        self.assertNotEqual(
            evidence_a["acquisition_sha256"],
            evidence_b["acquisition_sha256"],
        )
        tracked = evidence_a["acquisition_provenance"]["response_headers"]
        self.assertEqual(tracked["x-api-version"], "3.2.0")
        self.assertEqual(tracked["x-api-release-date"], "2026-09-01")
        self.assertEqual(tracked["deprecation"], "false")
        self.assertEqual(tracked["x-historical-window-hours"], "720")
        self.assertEqual(tracked["x-markets-unservable"], "outrights")
        self.assertEqual(tracked["cache-control"], "private, max-age=60")
        self.assertNotIn("authorization", tracked)
        self.assertNotIn(
            "response-secret-must-not-persist",
            json.dumps(evidence_a),
        )

    def test_acquisition_identity_binds_exact_request_scope(self) -> None:
        payload = _payload()
        transport_a = _Transport(payload)
        transport_b = _Transport(payload)
        provider_a = self._provider(transport_a)
        provider_b = ParlayApiTableTennisProvider(
            "secret-key-must-not-leak",
            regions=("us",),
            markets=("h2h",),
            transport=transport_b,
            clock=lambda: "2026-09-13T02:00:00+00:00",
            sleeper=lambda _: None,
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            a_path = root / "a.evidence.json"
            b_path = root / "b.evidence.json"
            capture_historical_snapshot(
                provider_a,
                requested_at="2026-09-12T10:03:00Z",
                output_path=root / "a.jsonl",
                evidence_path=a_path,
            )
            capture_historical_snapshot(
                provider_b,
                requested_at="2026-09-12T10:03:00Z",
                output_path=root / "b.jsonl",
                evidence_path=b_path,
            )
            a = json.loads(a_path.read_text(encoding="utf-8"))
            b = json.loads(b_path.read_text(encoding="utf-8"))

        self.assertEqual(a["response_sha256"], b["response_sha256"])
        self.assertNotEqual(a["acquisition_sha256"], b["acquisition_sha256"])
        self.assertEqual(
            a["acquisition_provenance"]["request"]["query"]["markets"],
            "h2h,spreads,totals",
        )
        self.assertEqual(
            b["acquisition_provenance"]["request"]["query"]["markets"],
            "h2h",
        )

    def test_duplicate_tracked_response_header_fails_closed(self) -> None:
        transport = _Transport(
            _payload(),
            response_headers={
                "X-API-Version": "3.2.0",
                "x-api-version": "3.2.1",
            },
        )
        provider = self._provider(transport)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(
                ProviderPayloadError,
                "duplicate tracked response header",
            ):
                capture_historical_snapshot(
                    provider,
                    requested_at="2026-09-12T10:03:00Z",
                    output_path=root / "market.jsonl",
                    evidence_path=root / "evidence.json",
                )
            self.assertFalse((root / "market.jsonl").exists())
            self.assertFalse((root / "evidence.json").exists())

    def test_non_200_transport_result_cannot_publish_success_evidence(self) -> None:
        transport = _Transport(_payload(), status_code=206)
        provider = self._provider(transport)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(
                ProviderPayloadError,
                "requires HTTP 200",
            ):
                capture_historical_snapshot(
                    provider,
                    requested_at="2026-09-12T10:03:00Z",
                    output_path=root / "market.jsonl",
                    evidence_path=root / "evidence.json",
                )
            self.assertFalse((root / "market.jsonl").exists())
            self.assertFalse((root / "evidence.json").exists())

    def test_request_provenance_rejects_credential_bearing_base_url(self) -> None:
        transport = _Transport(_payload())
        provider = ParlayApiTableTennisProvider(
            "secret-key-must-not-leak",
            base_url="https://user:embedded-secret@parlay-api.com",
            transport=transport,
            clock=lambda: "2026-09-13T02:00:00+00:00",
            sleeper=lambda _: None,
        )
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(ValueError, "secret-free HTTPS"):
                capture_historical_snapshot(
                    provider,
                    requested_at="2026-09-12T10:03:00Z",
                    output_path=Path(temp) / "market.jsonl",
                )
        self.assertEqual(transport.urls, [])

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

    def test_capture_rejects_output_evidence_alias_before_provider_io(self) -> None:
        transport = _Transport(_payload())
        provider = self._provider(transport)
        with tempfile.TemporaryDirectory() as temp:
            shared_path = Path(temp) / "shared.json"
            with self.assertRaisesRegex(ValueError, "output and evidence paths must be distinct"):
                capture_historical_snapshot(
                    provider,
                    requested_at="2026-09-12T10:03:00Z",
                    output_path=shared_path,
                    evidence_path=shared_path,
                )

        self.assertEqual(transport.urls, [])

    def test_capture_serializes_market_and_evidence_pair_publication(self) -> None:
        provider_a = self._provider(_Transport(_payload(event_id="tt-a")))
        provider_b = self._provider(_Transport(_payload(event_id="tt-b")))
        a_market_published = threading.Event()
        release_a = threading.Event()
        b_lock_attempted = threading.Event()
        b_writer_entered = threading.Event()
        errors: list[BaseException] = []
        errors_lock = threading.Lock()
        reports: dict[str, object] = {}
        original_writer = historical_snapshot._atomic_write_jsonl
        original_lock = historical_snapshot.durable_path_lock

        def controlled_writer(path: Path, rows) -> None:
            original_writer(path, rows)
            if threading.current_thread().name == "capture-a":
                a_market_published.set()
                if not release_a.wait(timeout=5):
                    raise AssertionError("timed out waiting to release capture-a publication")
            elif threading.current_thread().name == "capture-b":
                b_writer_entered.set()

        @contextmanager
        def observed_lock(path: Path):
            if threading.current_thread().name == "capture-b":
                b_lock_attempted.set()
            with original_lock(path):
                yield

        with tempfile.TemporaryDirectory() as temp:
            market_path = Path(temp) / "market.jsonl"
            evidence_path = Path(temp) / "evidence.json"

            def capture(label: str, provider: ParlayApiTableTennisProvider) -> None:
                try:
                    reports[label] = capture_historical_snapshot(
                        provider,
                        requested_at="2026-09-12T10:03:00Z",
                        output_path=market_path,
                        evidence_path=evidence_path,
                    )
                except BaseException as exc:
                    with errors_lock:
                        errors.append(exc)

            with mock.patch.object(
                historical_snapshot,
                "_atomic_write_jsonl",
                side_effect=controlled_writer,
            ), mock.patch.object(
                historical_snapshot,
                "durable_path_lock",
                side_effect=observed_lock,
            ):
                thread_a = threading.Thread(
                    target=capture,
                    args=("a", provider_a),
                    name="capture-a",
                    daemon=True,
                )
                thread_b = threading.Thread(
                    target=capture,
                    args=("b", provider_b),
                    name="capture-b",
                    daemon=True,
                )
                thread_a.start()
                self.assertTrue(a_market_published.wait(timeout=5))
                thread_b.start()
                self.assertTrue(b_lock_attempted.wait(timeout=5))
                self.assertFalse(b_writer_entered.is_set())
                release_a.set()
                thread_a.join(timeout=5)
                thread_b.join(timeout=5)

            self.assertFalse(thread_a.is_alive())
            self.assertFalse(thread_b.is_alive())
            self.assertEqual(errors, [])
            report_a = reports["a"]
            report_b = reports["b"]
            self.assertNotEqual(report_a.response_sha256, report_b.response_sha256)
            final_evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            self.assertEqual(
                historical_snapshot._sha256(market_path),
                report_b.market_sha256,
            )
            self.assertEqual(final_evidence["market_sha256"], report_b.market_sha256)
            self.assertEqual(final_evidence["response_sha256"], report_b.response_sha256)

    def test_atomic_writer_uses_isolated_temp_files_for_concurrent_publication(self) -> None:
        rows_a = [
            {"writer": "a", "index": 1},
            {"writer": "a", "index": 2},
        ]
        rows_b = [
            {"writer": "b", "index": 1},
            {"writer": "b", "index": 2},
        ]
        barrier = threading.Barrier(2)
        errors: list[BaseException] = []
        errors_lock = threading.Lock()

        def synchronized_rows(rows: list[dict[str, object]]):
            barrier.wait(timeout=5)
            yield from rows

        def publish(path: Path, rows: list[dict[str, object]]) -> None:
            try:
                _atomic_write_jsonl(path, synchronized_rows(rows))
            except BaseException as exc:
                with errors_lock:
                    errors.append(exc)

        with tempfile.TemporaryDirectory() as temp:
            market_path = Path(temp) / "market.jsonl"
            threads = [
                threading.Thread(target=publish, args=(market_path, rows_a), daemon=True),
                threading.Thread(target=publish, args=(market_path, rows_b), daemon=True),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            published = [
                json.loads(line)
                for line in market_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn(published, (rows_a, rows_b))
            self.assertEqual(
                list(Path(temp).glob(f".{market_path.name}.*.tmp")),
                [],
            )
            self.assertFalse(market_path.with_name(market_path.name + ".tmp").exists())

    def test_empty_snapshot_is_machine_visible_but_not_coverage_verified(self) -> None:
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
        self.assertFalse(evidence["point_in_time_snapshot_contains_odds"])
        self.assertFalse(evidence["point_in_time_odds_market_coverage_verified"])
        self.assertFalse(evidence["historical_window_market_coverage_verified"])
        self.assertFalse(evidence["replay_corpus_ready"])


if __name__ == "__main__":
    unittest.main()

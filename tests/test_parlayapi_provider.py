import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from autosport import parlayapi_provider as parlayapi_provider_module
from autosport.domain import MarketType
from autosport.ingestion import IngestionEngine
from autosport.market_bus import MarketEventBus
from autosport.parlayapi_provider import (
    HttpJsonResponse,
    ParlayApiTableTennisProvider,
    ProviderPayloadError,
    ProviderTransportError,
)
from autosport.providers import ProviderUnavailableError
from autosport.storage import SQLiteMarketStore


SAMPLE_EVENT = {
    "id": "tt-100",
    "sport_key": "table_tennis",
    "sport_title": "Table Tennis",
    "commence_time": "2026-09-12T21:00:00Z",
    "home_team": "Player A",
    "away_team": "Player B",
    "bookmakers": [
        {
            "key": "book-a",
            "title": "Book A",
            "last_update": "2026-09-12T20:00:00Z",
            "markets": [
                {
                    "key": "h2h",
                    "last_update": "2026-09-12T20:00:01Z",
                    "outcomes": [
                        {"name": "Player A", "price": 1.80},
                        {"name": "Player B", "price": 2.05},
                    ],
                },
                {
                    "key": "spreads",
                    "last_update": "2026-09-12T20:00:02Z",
                    "outcomes": [
                        {"name": "Player A", "price": 1.91, "point": -1.5},
                        {"name": "Player B", "price": 1.91, "point": 1.5},
                    ],
                },
                {
                    "key": "totals",
                    "last_update": "2026-09-12T20:00:03Z",
                    "outcomes": [
                        {"name": "Over", "price": 1.87, "point": 74.5},
                        {"name": "Under", "price": 1.95, "point": 74.5},
                    ],
                },
            ],
        }
    ],
}


COVERAGE_PAYLOAD = {
    "sport_key": "table_tennis",
    "window": {"date_from": "2026-09-01", "date_to": "2026-09-12"},
    "by_source": {
        "bovada": {
            "rows": 100,
            "first_date": "2026-09-01",
            "last_date": "2026-09-12",
            "priced_rows": 94,
        },
        "pinnacle": {
            "rows": 25,
            "first_date": "2026-09-05",
            "last_date": "2026-09-11",
            "priced_rows": 25,
        },
    },
    "_note": "coverage evidence only",
}


COVERAGE_HEADERS = {
    "X-Historical-Window-Hours": "720",
    "x-historical-window-from": "2026-08-14T00:00:00Z",
    "X-API-Version": "3.2.0",
}


class ParlayApiProviderTests(unittest.TestCase):
    def test_transport_error_is_typed_live_provider_unavailability(self):
        error = ProviderTransportError("provider transport unavailable")
        self.assertIsInstance(error, ProviderUnavailableError)
        self.assertIsInstance(error, RuntimeError)

    def test_default_transport_rejects_redirects_before_api_key_forwarding(self):
        for redirect_code in (301, 302, 303, 307, 308):
            with self.subTest(redirect_code=redirect_code):
                sink_api_keys: list[str | None] = []
                source_api_keys: list[str | None] = []

                class SinkHandler(BaseHTTPRequestHandler):
                    def do_GET(self):
                        sink_api_keys.append(self.headers.get("X-API-Key"))
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b"[]")

                    def log_message(self, format, *args):  # noqa: A002
                        del format, args

                sink = ThreadingHTTPServer(("127.0.0.1", 0), SinkHandler)
                sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
                sink_thread.start()
                sink_url = f"http://127.0.0.1:{sink.server_port}/credential-sink"

                class SourceHandler(BaseHTTPRequestHandler):
                    def do_GET(self):
                        source_api_keys.append(self.headers.get("X-API-Key"))
                        self.send_response(redirect_code)
                        self.send_header("Location", sink_url)
                        self.end_headers()

                    def log_message(self, format, *args):  # noqa: A002
                        del format, args

                source = ThreadingHTTPServer(("127.0.0.1", 0), SourceHandler)
                source_thread = threading.Thread(target=source.serve_forever, daemon=True)
                source_thread.start()
                try:
                    with self.assertRaises(ProviderTransportError) as raised:
                        parlayapi_provider_module._default_transport(
                            f"http://127.0.0.1:{source.server_port}/start",
                            {"X-API-Key": "dummy-secret"},
                            1.0,
                        )
                    self.assertEqual(raised.exception.status_code, redirect_code)
                    self.assertEqual(source_api_keys, ["dummy-secret"])
                    self.assertEqual(sink_api_keys, [])
                finally:
                    source.shutdown()
                    sink.shutdown()
                    source.server_close()
                    sink.server_close()
                    source_thread.join(timeout=2)
                    sink_thread.join(timeout=2)

    def test_default_transport_preserves_direct_json_success(self):
        class DirectHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"[]")

            def log_message(self, format, *args):  # noqa: A002
                del format, args

        server = ThreadingHTTPServer(("127.0.0.1", 0), DirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            response = parlayapi_provider_module._default_transport(
                f"http://127.0.0.1:{server.server_port}/direct",
                {"X-API-Key": "dummy-secret"},
                1.0,
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.payload, [])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_provider_rejects_caller_selected_origin_before_request(self):
        invalid_origins = (
            "http://parlay-api.com",
            "https://example.invalid",
            "https://parlay-api.com.evil.invalid",
            "https://parlay-api.com/v1",
            "https://parlay-api.com:444",
        )
        for base_url in invalid_origins:
            with self.subTest(base_url=base_url):
                with self.assertRaisesRegex(ValueError, "canonical ParlayAPI origin"):
                    ParlayApiTableTennisProvider("dummy-secret", base_url=base_url)

        provider = ParlayApiTableTennisProvider(
            "dummy-secret",
            base_url="https://parlay-api.com/",
        )
        self.assertEqual(provider.base_url, "https://parlay-api.com")

    def test_authenticated_snapshot_maps_to_typed_provider_quotes_without_key_in_url(self):
        calls = []

        def transport(url, headers, timeout):
            calls.append((url, dict(headers), timeout))
            return HttpJsonResponse([SAMPLE_EVENT], 200, {})

        provider = ParlayApiTableTennisProvider(
            "secret-key",
            transport=transport,
            clock=lambda: "2026-09-12T20:00:10+00:00",
        )
        batch = provider.read_batch()
        self.assertEqual(batch.source_id, "parlayapi:table_tennis")
        self.assertEqual(len(batch.quotes), 6)
        self.assertNotIn("secret-key", calls[0][0])
        self.assertEqual(calls[0][1]["X-API-Key"], "secret-key")
        self.assertIn("oddsFormat=decimal", calls[0][0])
        self.assertEqual(batch.quotes[0].market_type, MarketType.WINNER)
        self.assertEqual(batch.quotes[2].market_type, MarketType.HANDICAP)
        self.assertEqual(batch.quotes[4].market_type, MarketType.TOTAL)
        self.assertEqual(batch.quotes[2].provider_market_id, "book-a:spreads:1.5")
        self.assertEqual(batch.quotes[3].provider_market_id, "book-a:spreads:1.5")
        self.assertEqual(batch.quotes[0].source_ts, "2026-09-12T20:00:01Z")
        self.assertEqual(batch.quotes[0].metadata["bookmaker_key"], "book-a")
        self.assertFalse(batch.quotes[0].metadata["public_preview"])

    def test_public_preview_wrapper_uses_same_parser_without_api_key(self):
        seen = {}

        def transport(url, headers, timeout):
            seen["url"] = url
            seen["headers"] = dict(headers)
            return HttpJsonResponse({"events_returned": 1, "events": [SAMPLE_EVENT]}, 200, {})

        provider = ParlayApiTableTennisProvider(
            public_preview=True,
            transport=transport,
            clock=lambda: "2026-09-12T20:00:10+00:00",
        )
        batch = provider.read_batch(max_items=2)
        self.assertEqual(len(batch.quotes), 2)
        self.assertIn("/v1/try/table_tennis/odds", seen["url"])
        self.assertNotIn("X-API-Key", seen["headers"])
        self.assertTrue(batch.quotes[0].metadata["public_preview"])

    def test_unchanged_source_snapshot_is_idempotent_across_poll_receive_times(self):
        fetch_times = iter(["2026-09-12T20:00:10+00:00", "2026-09-12T20:00:20+00:00"])

        def transport(url, headers, timeout):
            return HttpJsonResponse([SAMPLE_EVENT], 200, {})

        provider = ParlayApiTableTennisProvider("key", transport=transport, clock=lambda: next(fetch_times))
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMarketStore(Path(tmp) / "market.db")
            engine = IngestionEngine(MarketEventBus(store))
            first = engine.poll_once(provider)
            second = engine.poll_once(provider)
            self.assertEqual(first.accepted, 6)
            self.assertEqual(second.accepted, 0)
            self.assertEqual(len(store.events()), 6)
            store.close()

    def test_rate_limit_retry_is_bounded_and_honors_capped_retry_after(self):
        attempts = []
        sleeps = []

        def transport(url, headers, timeout):
            attempts.append(url)
            if len(attempts) == 1:
                raise ProviderTransportError("rate limited", 429, 30.0)
            return HttpJsonResponse([SAMPLE_EVENT], 200, {})

        provider = ParlayApiTableTennisProvider(
            "key",
            transport=transport,
            sleeper=sleeps.append,
            max_attempts=2,
            max_backoff_seconds=0.5,
            clock=lambda: "2026-09-12T20:00:10+00:00",
        )
        provider.read_batch(max_items=1)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sleeps, [0.5])

    def test_statusless_transport_failure_retries_once_and_recovers(self):
        attempts = []
        sleeps = []

        def transport(url, headers, timeout):
            attempts.append(url)
            if len(attempts) == 1:
                raise ProviderTransportError("provider transport timeout")
            return HttpJsonResponse([SAMPLE_EVENT], 200, {})

        provider = ParlayApiTableTennisProvider(
            "key",
            transport=transport,
            sleeper=sleeps.append,
            max_attempts=2,
            max_backoff_seconds=1.0,
            clock=lambda: "2026-09-12T20:00:10+00:00",
        )
        provider.read_batch(max_items=1)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sleeps, [0.25])

    def test_statusless_transport_failure_stops_at_max_attempts(self):
        attempts = []
        sleeps = []

        def transport(url, headers, timeout):
            attempts.append(url)
            raise ProviderTransportError("provider transport timeout")

        provider = ParlayApiTableTennisProvider(
            "key",
            transport=transport,
            sleeper=sleeps.append,
            max_attempts=3,
            max_backoff_seconds=1.0,
            clock=lambda: "2026-09-12T20:00:10+00:00",
        )
        with self.assertRaisesRegex(ProviderTransportError, "transport timeout"):
            provider.read_batch(max_items=1)
        self.assertEqual(len(attempts), 3)
        self.assertEqual(sleeps, [0.25, 0.5])

    def test_non_retryable_client_error_still_fails_fast(self):
        attempts = []
        sleeps = []

        def transport(url, headers, timeout):
            attempts.append(url)
            raise ProviderTransportError("unauthorized", 401)

        provider = ParlayApiTableTennisProvider(
            "key",
            transport=transport,
            sleeper=sleeps.append,
            max_attempts=5,
            max_backoff_seconds=1.0,
            clock=lambda: "2026-09-12T20:00:10+00:00",
        )
        with self.assertRaisesRegex(ProviderTransportError, "unauthorized"):
            provider.read_batch(max_items=1)
        self.assertEqual(len(attempts), 1)
        self.assertEqual(sleeps, [])

    def test_malformed_payload_fails_closed(self):
        provider = ParlayApiTableTennisProvider(
            "key",
            transport=lambda *_: HttpJsonResponse({"unexpected": []}, 200, {}),
            clock=lambda: "2026-09-12T20:00:10+00:00",
        )
        with self.assertRaises(ProviderPayloadError):
            provider.read_batch()

    def test_historical_coverage_verifies_exact_window_and_never_puts_key_in_url(self):
        calls = []

        def transport(url, headers, timeout):
            calls.append((url, dict(headers), timeout))
            return HttpJsonResponse(COVERAGE_PAYLOAD, 200, COVERAGE_HEADERS)

        provider = ParlayApiTableTennisProvider(
            "historical-secret",
            transport=transport,
            clock=lambda: "2026-09-13T01:00:00+00:00",
        )
        report = provider.historical_coverage("2026-09-01", "2026-09-12")
        self.assertIn("/v1/historical/sports/table_tennis/coverage?", calls[0][0])
        self.assertIn("dateFrom=2026-09-01", calls[0][0])
        self.assertIn("dateTo=2026-09-12", calls[0][0])
        self.assertNotIn("historical-secret", calls[0][0])
        self.assertEqual(calls[0][1]["X-API-Key"], "historical-secret")
        self.assertEqual(report.historical_window_hours, 720)
        self.assertEqual(report.historical_window_from, "2026-08-14T00:00:00Z")
        self.assertEqual(report.api_version, "3.2.0")
        self.assertEqual(report.total_rows, 125)
        self.assertEqual(report.total_priced_rows, 119)
        self.assertTrue(report.has_data)
        self.assertEqual([item.source for item in report.sources], ["bovada", "pinnacle"])
        self.assertEqual(len(report.response_sha256), 64)

    def test_historical_coverage_empty_by_source_is_truthful_no_data_not_successful_corpus(self):
        payload = {
            "sport_key": "table_tennis",
            "window": {"date_from": "2026-09-01", "date_to": "2026-09-12"},
            "by_source": {},
        }
        provider = ParlayApiTableTennisProvider(
            "key",
            transport=lambda *_: HttpJsonResponse(payload, 200, COVERAGE_HEADERS),
            clock=lambda: "2026-09-13T01:00:00+00:00",
        )
        report = provider.historical_coverage("2026-09-01", "2026-09-12")
        self.assertFalse(report.has_data)
        self.assertEqual(report.total_rows, 0)
        self.assertEqual(report.total_priced_rows, 0)

    def test_historical_coverage_requires_entitlement_headers(self):
        provider = ParlayApiTableTennisProvider(
            "key",
            transport=lambda *_: HttpJsonResponse(COVERAGE_PAYLOAD, 200, {}),
            clock=lambda: "2026-09-13T01:00:00+00:00",
        )
        with self.assertRaisesRegex(ProviderPayloadError, "entitlement-window headers"):
            provider.historical_coverage("2026-09-01", "2026-09-12")

    def test_historical_coverage_rejects_provider_window_mismatch(self):
        payload = dict(COVERAGE_PAYLOAD)
        payload["window"] = {"date_from": "2026-09-02", "date_to": "2026-09-12"}
        provider = ParlayApiTableTennisProvider(
            "key",
            transport=lambda *_: HttpJsonResponse(payload, 200, COVERAGE_HEADERS),
            clock=lambda: "2026-09-13T01:00:00+00:00",
        )
        with self.assertRaisesRegex(ProviderPayloadError, "window mismatch"):
            provider.historical_coverage("2026-09-01", "2026-09-12")

    def test_historical_coverage_rejects_request_older_than_entitlement_header(self):
        headers = dict(COVERAGE_HEADERS)
        headers["x-historical-window-from"] = "2026-09-05T00:00:00Z"
        provider = ParlayApiTableTennisProvider(
            "key",
            transport=lambda *_: HttpJsonResponse(COVERAGE_PAYLOAD, 200, headers),
            clock=lambda: "2026-09-13T01:00:00+00:00",
        )
        with self.assertRaisesRegex(ProviderPayloadError, "contradicts"):
            provider.historical_coverage("2026-09-01", "2026-09-12")

    def test_historical_coverage_rejects_impossible_priced_row_count(self):
        payload = dict(COVERAGE_PAYLOAD)
        payload["by_source"] = {
            "bovada": {
                "rows": 10,
                "first_date": "2026-09-01",
                "last_date": "2026-09-12",
                "priced_rows": 11,
            }
        }
        provider = ParlayApiTableTennisProvider(
            "key",
            transport=lambda *_: HttpJsonResponse(payload, 200, COVERAGE_HEADERS),
            clock=lambda: "2026-09-13T01:00:00+00:00",
        )
        with self.assertRaisesRegex(ProviderPayloadError, "priced_rows"):
            provider.historical_coverage("2026-09-01", "2026-09-12")

    def test_historical_coverage_rejects_public_preview_and_bad_date_order(self):
        preview = ParlayApiTableTennisProvider(public_preview=True, transport=lambda *_: None)
        with self.assertRaisesRegex(ValueError, "authenticated API key"):
            preview.historical_coverage("2026-09-01", "2026-09-12")

        provider = ParlayApiTableTennisProvider("key", transport=lambda *_: None)
        with self.assertRaisesRegex(ValueError, "must not precede"):
            provider.historical_coverage("2026-09-12", "2026-09-01")


if __name__ == "__main__":
    unittest.main()

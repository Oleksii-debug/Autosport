from __future__ import annotations

import hashlib
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from autosport.parlay_historical_match_acquisition import (
    HistoricalMatchAcquisitionError,
    HistoricalMatchTransportError,
    RawHistoricalMatchResponse,
    acquire_parlay_historical_matches,
)


HEADERS = {
    "X-Historical-Window-Hours": "720",
    "x-historical-window-from": "2026-08-22T00:00:00Z",
    "X-API-Version": "3.2.0",
    "X-Coverage-Hint": "source-filtered archive coverage is sparse",
}


def _body(rows=None) -> bytes:
    if rows is None:
        rows = [
            {
                "source": "pinnacle",
                "home_team": "Alice",
                "away_team": "Bob",
                "home_score": 3,
                "away_score": 1,
                "has_odds": True,
            },
            {
                "source": "pinnacle",
                "home_team": "Carol",
                "away_team": "Dana",
                "home_score": 2,
                "away_score": 3,
                "has_odds": False,
            },
        ]
    return json.dumps(rows, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _transport_for(
    body: bytes | None = None,
    *,
    status: int = 200,
    headers=None,
    final_url_transform=None,
    calls=None,
):
    payload = _body() if body is None else body
    response_headers = HEADERS if headers is None else headers

    def transport(url, request_headers, timeout, max_bytes):
        if calls is not None:
            calls.append((url, dict(request_headers), timeout, max_bytes))
        final_url = url if final_url_transform is None else final_url_transform(url)
        return RawHistoricalMatchResponse(
            body=payload,
            status_code=status,
            headers=response_headers,
            final_url=final_url,
        )

    return transport


class ParlayHistoricalMatchAcquisitionTests(unittest.TestCase):
    def test_authenticated_fixed_scope_preserves_exact_raw_bytes_without_key_in_url(self):
        calls = []
        payload = b' [ {"source":"pinnacle","has_odds":true} ] \n'
        acquisition = acquire_parlay_historical_matches(
            api_key="secret-key",
            date_from="2026-09-01",
            date_to="2026-09-12",
            sources=("pinnacle",),
            clock=lambda: "2026-09-13T01:00:00+00:00",
            transport=_transport_for(payload, calls=calls),
        )

        self.assertEqual(len(calls), 1)
        url, headers, timeout, max_bytes = calls[0]
        parsed = urlsplit(url)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.hostname, "parlay-api.com")
        self.assertEqual(
            parsed.path,
            "/v1/historical/sports/table_tennis/matches",
        )
        query = parse_qs(parsed.query)
        self.assertEqual(query["dateFrom"], ["2026-09-01"])
        self.assertEqual(query["dateTo"], ["2026-09-12"])
        self.assertEqual(query["sources"], ["pinnacle"])
        self.assertEqual(query["pricedOnly"], ["false"])
        self.assertEqual(query["includeRaw"], ["false"])
        self.assertEqual(query["limit"], ["5000"])
        self.assertNotIn("secret-key", url)
        self.assertEqual(headers["X-API-Key"], "secret-key")
        self.assertEqual(timeout, 10.0)
        self.assertGreater(max_bytes, len(payload))

        self.assertEqual(acquisition.response_bytes, payload)
        self.assertEqual(
            acquisition.response_sha256,
            hashlib.sha256(payload).hexdigest(),
        )
        self.assertEqual(acquisition.row_count, 1)
        self.assertEqual(acquisition.observed_sources, ("pinnacle",))
        self.assertEqual(acquisition.historical_window_hours, 720)
        self.assertEqual(
            acquisition.historical_window_from,
            "2026-08-22T00:00:00Z",
        )
        self.assertEqual(acquisition.api_version, "3.2.0")
        self.assertEqual(
            acquisition.coverage_hint,
            "source-filtered archive coverage is sparse",
        )
        self.assertFalse(acquisition.possibly_truncated)
        self.assertFalse(acquisition.provider_origin_verified)
        self.assertFalse(acquisition.acquisition_time_verified)

    def test_injected_transport_cannot_mint_provider_origin_even_with_canonical_final_url(self):
        acquisition = acquire_parlay_historical_matches(
            api_key="key",
            date_from="2026-09-01",
            date_to="2026-09-12",
            clock=lambda: "2026-09-13T01:00:00+00:00",
            transport=_transport_for(),
        )
        self.assertFalse(acquisition.provider_origin_verified)
        self.assertFalse(acquisition.acquisition_time_verified)

    def test_response_digest_is_over_exact_bytes_not_reserialized_json(self):
        first = b'[{"source":"pinnacle","has_odds":true}]'
        second = b'[ { "source": "pinnacle", "has_odds": true } ]'
        kwargs = dict(
            api_key="key",
            date_from="2026-09-01",
            date_to="2026-09-12",
            clock=lambda: "2026-09-13T01:00:00+00:00",
        )
        a = acquire_parlay_historical_matches(
            **kwargs,
            transport=_transport_for(first),
        )
        b = acquire_parlay_historical_matches(
            **kwargs,
            transport=_transport_for(second),
        )
        self.assertNotEqual(a.response_sha256, b.response_sha256)
        self.assertEqual(a.row_count, b.row_count)

    def test_wrong_final_origin_or_path_fails_closed(self):
        transforms = (
            lambda url: url.replace("parlay-api.com", "evil.example"),
            lambda url: url.replace("/matches", "/odds"),
            lambda url: url.replace("dateTo=2026-09-12", "dateTo=2026-09-11"),
        )
        for transform in transforms:
            with self.subTest(transform=transform):
                with self.assertRaisesRegex(
                    HistoricalMatchAcquisitionError,
                    "fixed ParlayAPI origin/path|changed the requested evidence scope",
                ):
                    acquire_parlay_historical_matches(
                        api_key="key",
                        date_from="2026-09-01",
                        date_to="2026-09-12",
                        clock=lambda: "2026-09-13T01:00:00+00:00",
                        transport=_transport_for(final_url_transform=transform),
                    )

    def test_missing_or_invalid_entitlement_headers_fail_closed(self):
        cases = (
            {},
            {
                "x-historical-window-hours": "not-an-int",
                "x-historical-window-from": "2026-08-22",
            },
            {
                "x-historical-window-hours": "0",
                "x-historical-window-from": "2026-08-22",
            },
            {
                "x-historical-window-hours": "720",
                "x-historical-window-from": "not-a-date",
            },
        )
        for headers in cases:
            with self.subTest(headers=headers):
                with self.assertRaises(HistoricalMatchAcquisitionError):
                    acquire_parlay_historical_matches(
                        api_key="key",
                        date_from="2026-09-01",
                        date_to="2026-09-12",
                        clock=lambda: "2026-09-13T01:00:00+00:00",
                        transport=_transport_for(headers=headers),
                    )

    def test_request_before_entitlement_window_fails_closed(self):
        headers = {
            "x-historical-window-hours": "168",
            "x-historical-window-from": "2026-09-05T00:00:00Z",
        }
        with self.assertRaisesRegex(
            HistoricalMatchAcquisitionError,
            "contradicts its entitlement window",
        ):
            acquire_parlay_historical_matches(
                api_key="key",
                date_from="2026-09-01",
                date_to="2026-09-12",
                clock=lambda: "2026-09-13T01:00:00+00:00",
                transport=_transport_for(headers=headers),
            )

    def test_duplicate_json_keys_and_nonstandard_constants_fail_closed(self):
        payloads = (
            b'[{"source":"pinnacle","source":"other","has_odds":true}]',
            b'[{"source":"pinnacle","has_odds":true,"x":NaN}]',
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(HistoricalMatchAcquisitionError):
                    acquire_parlay_historical_matches(
                        api_key="key",
                        date_from="2026-09-01",
                        date_to="2026-09-12",
                        clock=lambda: "2026-09-13T01:00:00+00:00",
                        transport=_transport_for(payload),
                    )

    def test_non_array_non_object_rows_or_missing_has_odds_fail_closed(self):
        payloads = (
            b'{"matches":[]}',
            b'[1]',
            b'[{"source":"pinnacle"}]',
            b'[{"source":"pinnacle","has_odds":1}]',
            b'[{"source":"","has_odds":false}]',
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(HistoricalMatchAcquisitionError):
                    acquire_parlay_historical_matches(
                        api_key="key",
                        date_from="2026-09-01",
                        date_to="2026-09-12",
                        clock=lambda: "2026-09-13T01:00:00+00:00",
                        transport=_transport_for(payload),
                    )

    def test_row_source_cannot_escape_requested_filter(self):
        with self.assertRaisesRegex(
            HistoricalMatchAcquisitionError,
            "escaped requested sources",
        ):
            acquire_parlay_historical_matches(
                api_key="key",
                date_from="2026-09-01",
                date_to="2026-09-12",
                sources=("pinnacle",),
                clock=lambda: "2026-09-13T01:00:00+00:00",
                transport=_transport_for(
                    b'[{"source":"hltv","has_odds":false}]'
                ),
            )

    def test_http_non_200_is_typed_transport_failure(self):
        with self.assertRaises(HistoricalMatchTransportError) as caught:
            acquire_parlay_historical_matches(
                api_key="key",
                date_from="2026-09-01",
                date_to="2026-09-12",
                clock=lambda: "2026-09-13T01:00:00+00:00",
                transport=_transport_for(status=429),
            )
        self.assertEqual(caught.exception.status_code, 429)

    def test_oversized_injected_body_is_rejected_even_if_transport_ignores_bound(self):
        oversized = b"[" + (b" " * (64 * 1024 * 1024)) + b"]"
        with self.assertRaisesRegex(
            HistoricalMatchAcquisitionError,
            "fixed size bound",
        ):
            acquire_parlay_historical_matches(
                api_key="key",
                date_from="2026-09-01",
                date_to="2026-09-12",
                clock=lambda: "2026-09-13T01:00:00+00:00",
                transport=_transport_for(oversized),
            )

    def test_invalid_request_scope_fails_before_transport(self):
        calls = []
        transport = _transport_for(calls=calls)
        cases = (
            dict(api_key="", date_from="2026-09-01", date_to="2026-09-12"),
            dict(api_key="key", date_from="2026-09-1", date_to="2026-09-12"),
            dict(api_key="key", date_from="2026-09-12", date_to="2026-09-01"),
            dict(
                api_key="key",
                date_from="2026-09-01",
                date_to="2026-09-12",
                sources=("Pinnacle",),
            ),
            dict(
                api_key="key",
                date_from="2026-09-01",
                date_to="2026-09-12",
                sources=("pinnacle", "pinnacle"),
            ),
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(HistoricalMatchAcquisitionError):
                    acquire_parlay_historical_matches(
                        **kwargs,
                        clock=lambda: "2026-09-13T01:00:00+00:00",
                        transport=transport,
                    )
        self.assertEqual(calls, [])

    def test_bad_clock_fails_after_response_without_mutating_origin_claim(self):
        with self.assertRaisesRegex(
            HistoricalMatchAcquisitionError,
            "clock",
        ):
            acquire_parlay_historical_matches(
                api_key="key",
                date_from="2026-09-01",
                date_to="2026-09-12",
                clock=lambda: "2026-09-13",
                transport=_transport_for(),
            )

    def test_exact_raw_response_type_is_required(self):
        class RawSubclass(RawHistoricalMatchResponse):
            pass

        def transport(url, headers, timeout, max_bytes):
            return RawSubclass(_body(), 200, HEADERS, url)

        with self.assertRaisesRegex(
            HistoricalMatchAcquisitionError,
            "exact RawHistoricalMatchResponse",
        ):
            acquire_parlay_historical_matches(
                api_key="key",
                date_from="2026-09-01",
                date_to="2026-09-12",
                clock=lambda: "2026-09-13T01:00:00+00:00",
                transport=transport,
            )


if __name__ == "__main__":
    unittest.main()

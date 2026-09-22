import hashlib
import unittest
from decimal import Decimal
from urllib.error import HTTPError
from unittest.mock import patch

import autosport.the_odds_api_provider as odds_api_module
from autosport.domain import MarketType
from autosport.providers import CanonicalNormalizer, ProviderUnavailableError
from autosport.the_odds_api_provider import (
    HttpJsonResponse,
    TheOddsApiPayloadError,
    TheOddsApiProvider,
    TheOddsApiTransportError,
)


def event(
    *,
    sport="soccer_epl",
    event_id="0123456789abcdef0123456789abcdef",
    market_key="h2h",
    market_last_update="2026-09-22T13:50:00Z",
    price=Decimal("2.10"),
    point=None,
    bookmaker_sid="event-77",
    market_sid="market-88",
    outcome_sid="outcome-99",
    description=None,
):
    outcome = {"name": "Alpha", "price": price, "sid": outcome_sid}
    if point is not None:
        outcome["point"] = point
    if description is not None:
        outcome["description"] = description
    return {
        "id": event_id,
        "sport_key": sport,
        "sport_title": sport,
        "commence_time": "2026-09-22T15:00:00Z",
        "home_team": "Alpha",
        "away_team": "Beta",
        "bookmakers": [
            {
                "key": "book-a",
                "title": "Book A",
                "last_update": "2026-09-22T13:59:59Z",
                "sid": bookmaker_sid,
                "markets": [
                    {
                        "key": market_key,
                        "last_update": market_last_update,
                        "sid": market_sid,
                        "outcomes": [outcome],
                    }
                ],
            }
        ],
    }


class TheOddsApiProviderTests(unittest.TestCase):
    def test_current_read_uses_market_timestamp_exact_decimal_quota_and_terms_evidence(self):
        calls = []

        def transport(url, timeout):
            calls.append((url, timeout))
            return HttpJsonResponse(
                [event()],
                200,
                {
                    "X-Requests-Remaining": "499",
                    "x-requests-used": "12",
                    "x-requests-last": "1",
                },
            )

        provider = TheOddsApiProvider(
            "secret",
            sport="soccer_epl",
            regions=("eu",),
            markets=("h2h",),
            transport=transport,
            clock=lambda: "2026-09-22T14:00:00+00:00",
        )
        batch = provider.read_batch()

        self.assertEqual(batch.source_id, "the-odds-api:soccer_epl")
        self.assertEqual(
            batch.quality_flags,
            (
                "DYNAMIC_COVERAGE",
                "UNVERIFIED_PROVIDER_ORIGIN",
                "UNVERIFIED_RECEIPT_CLOCK",
            ),
        )
        self.assertEqual(len(batch.quotes), 1)
        quote = batch.quotes[0]
        self.assertEqual(quote.decimal_odds, Decimal("2.10"))
        self.assertEqual(quote.source_ts, "2026-09-22T13:50:00Z")
        self.assertEqual(quote.market_type, MarketType.WINNER)
        self.assertEqual(quote.sport, "soccer_epl")
        self.assertEqual(quote.status, "observed")
        self.assertEqual(quote.metadata["bookmaker_event_sid"], "event-77")
        self.assertEqual(quote.metadata["market_sid"], "market-88")
        self.assertEqual(quote.metadata["outcome_sid"], "outcome-99")
        self.assertFalse(quote.metadata["coverage_complete"])
        self.assertEqual(quote.metadata["request"]["quota"]["remaining"], 499)
        self.assertFalse(
            quote.metadata["terms"]["standalone_raw_redistribution_permitted"]
        )
        self.assertFalse(quote.metadata["terms"]["wagering_operator"])
        self.assertIn("apiKey=secret", calls[0][0])
        self.assertNotIn("secret", repr(quote.metadata))

    def test_current_observed_at_is_sampled_after_response_returns(self):
        response_returned = {"value": False}

        def transport(url, timeout):
            response_returned["value"] = True
            return HttpJsonResponse([event()], 200, {})

        def clock():
            self.assertTrue(response_returned["value"])
            return "2026-09-22T14:00:01+00:00"

        provider = TheOddsApiProvider(
            "k",
            sport="soccer_epl",
            transport=transport,
            clock=clock,
        )
        quote = provider.read_batch().quotes[0]

        self.assertEqual(quote.observed_ts, "2026-09-22T14:00:01+00:00")
        self.assertEqual(
            quote.metadata["request"]["observed_at"],
            "2026-09-22T14:00:01+00:00",
        )

    def test_bookmakers_take_request_precedence_and_can_omit_regions(self):
        seen = {}

        def transport(url, timeout):
            seen["url"] = url
            return HttpJsonResponse([event()], 200, {})

        provider = TheOddsApiProvider(
            "k",
            sport="soccer_epl",
            regions=(),
            bookmakers=("book-a",),
            markets=("h2h",),
            transport=transport,
            clock=lambda: "2026-09-22T14:00:00+00:00",
        )
        quote = provider.read_batch().quotes[0]
        self.assertIn("bookmakers=book-a", seen["url"])
        self.assertNotIn("regions=", seen["url"])
        request = quote.metadata["request"]
        self.assertEqual(request["effective_bookmaker_scope"], "bookmakers")
        self.assertEqual(request["regions"], [])
        self.assertEqual(request["bookmakers"], ["book-a"])

    def test_missing_sids_remain_explicit_missing_and_are_not_fabricated(self):
        payload = event(bookmaker_sid=None, market_sid=None, outcome_sid=None)
        quote = TheOddsApiProvider(
            "k",
            sport="soccer_epl",
            transport=lambda *_: HttpJsonResponse([payload], 200, {}),
            clock=lambda: "2026-09-22T14:00:00+00:00",
        ).read_batch().quotes[0]
        self.assertIsNone(quote.metadata["bookmaker_event_sid"])
        self.assertIsNone(quote.metadata["market_sid"])
        self.assertIsNone(quote.metadata["outcome_sid"])
        self.assertNotEqual(quote.provider_selection_id, quote.provider_event_id)

    def test_provider_native_sids_round_trip_losslessly_even_with_identity_delimiters(self):
        payload = event(
            bookmaker_sid="event|native",
            market_sid="market|native",
            outcome_sid="outcome|native",
        )
        quote = TheOddsApiProvider(
            "k",
            sport="soccer_epl",
            transport=lambda *_: HttpJsonResponse([payload], 200, {}),
            clock=lambda: "2026-09-22T14:00:00+00:00",
        ).read_batch().quotes[0]
        self.assertEqual(quote.metadata["bookmaker_event_sid"], "event|native")
        self.assertEqual(quote.metadata["market_sid"], "market|native")
        self.assertEqual(quote.metadata["outcome_sid"], "outcome|native")
        self.assertNotIn("|", quote.provider_market_id)
        self.assertNotIn("|", quote.provider_selection_id)

    def test_same_display_event_across_sports_has_distinct_canonical_identity(self):
        football = event(sport="soccer_epl")
        basketball = event(sport="basketball_nba")
        provider = TheOddsApiProvider(
            "k",
            sport="upcoming",
            transport=lambda *_: HttpJsonResponse([football, basketball], 200, {}),
            clock=lambda: "2026-09-22T14:00:00+00:00",
        )
        quotes = provider.read_batch().quotes
        self.assertEqual(
            [quote.sport for quote in quotes],
            ["soccer_epl", "basketball_nba"],
        )
        normalizer = CanonicalNormalizer()
        first = normalizer.normalize(provider.source_id, quotes[0])
        second = normalizer.normalize(provider.source_id, quotes[1])
        self.assertNotEqual(first.quote_key, second.quote_key)

    def test_lay_market_cannot_alias_back_like_h2h(self):
        back = event(market_key="h2h", market_sid="m1", outcome_sid="o1")
        lay = event(
            market_key="h2h_lay",
            market_sid="m1",
            outcome_sid="o1",
        )
        provider = TheOddsApiProvider(
            "k",
            sport="soccer_epl",
            transport=lambda *_: HttpJsonResponse([back, lay], 200, {}),
            clock=lambda: "2026-09-22T14:00:00+00:00",
        )
        first, second = provider.read_batch().quotes
        self.assertNotEqual(first.provider_market_id, second.provider_market_id)
        self.assertNotEqual(first.provider_selection_id, second.provider_selection_id)
        self.assertIsNone(first.metadata["exchange_side"])
        self.assertEqual(second.metadata["exchange_side"], "lay")

    def test_spread_point_is_exact_and_changes_evidence_identity(self):
        positive = event(market_key="spreads", point=Decimal("1.5"))
        negative = event(
            market_key="spreads",
            point=Decimal("-1.5"),
            outcome_sid="other",
        )
        provider = TheOddsApiProvider(
            "k",
            sport="soccer_epl",
            transport=lambda *_: HttpJsonResponse([positive, negative], 200, {}),
            clock=lambda: "2026-09-22T14:00:00+00:00",
        )
        first, second = provider.read_batch().quotes
        self.assertEqual(first.market_type, MarketType.HANDICAP)
        self.assertEqual(first.metadata["point"], "1.5")
        self.assertEqual(second.metadata["point"], "-1.5")
        self.assertNotEqual(first.provider_selection_id, second.provider_selection_id)

    def test_stale_current_market_timestamp_is_not_rejuvenated_by_receive_time(self):
        provider = TheOddsApiProvider(
            "k",
            sport="soccer_epl",
            max_market_age_seconds=60,
            transport=lambda *_: HttpJsonResponse(
                [event(market_last_update="2026-09-22T13:00:00Z")],
                200,
                {},
            ),
            clock=lambda: "2026-09-22T14:00:00+00:00",
        )
        with self.assertRaisesRegex(TheOddsApiPayloadError, "stale"):
            provider.read_batch()

    def test_duplicate_exact_source_identity_fails_before_batch_publication(self):
        duplicate = event()
        provider = TheOddsApiProvider(
            "k",
            sport="soccer_epl",
            transport=lambda *_: HttpJsonResponse([duplicate, duplicate], 200, {}),
            clock=lambda: "2026-09-22T14:00:00+00:00",
        )
        with self.assertRaisesRegex(TheOddsApiPayloadError, "duplicate exact"):
            provider.read_batch()

    def test_float_ingress_is_rejected_in_injected_payload(self):
        provider = TheOddsApiProvider(
            "k",
            sport="soccer_epl",
            transport=lambda *_: HttpJsonResponse([event(price=2.1)], 200, {}),
            clock=lambda: "2026-09-22T14:00:00+00:00",
        )
        with self.assertRaisesRegex(TheOddsApiPayloadError, "exact Decimal/string"):
            provider.read_batch()

    def test_empty_response_is_narrow_dynamic_no_data_not_completeness(self):
        provider = TheOddsApiProvider(
            "k",
            sport="soccer_epl",
            transport=lambda *_: HttpJsonResponse(
                [], 200, {"x-requests-last": "0"}
            ),
            clock=lambda: "2026-09-22T14:00:00+00:00",
        )
        batch = provider.read_batch()
        self.assertEqual(batch.quotes, ())
        self.assertIn("DYNAMIC_COVERAGE", batch.quality_flags)
        self.assertIn("EMPTY_RESPONSE", batch.quality_flags)
        self.assertEqual(provider.last_request_evidence.quota_last, 0)

    def test_http_auth_quota_or_provider_failure_is_typed_unavailability(self):
        provider = TheOddsApiProvider(
            "k",
            sport="soccer_epl",
            transport=lambda *_: HttpJsonResponse({"message": "quota"}, 429, {}),
            clock=lambda: "2026-09-22T14:00:00+00:00",
        )
        with self.assertRaises(TheOddsApiTransportError) as context:
            provider.read_batch()
        self.assertIsInstance(context.exception, ProviderUnavailableError)
        self.assertEqual(context.exception.status_code, 429)

    def test_historical_evidence_binds_actual_snapshot_and_freshness_to_snapshot_time(self):
        payload = {
            "timestamp": "2026-09-22T12:40:00Z",
            "previous_timestamp": "2026-09-22T12:35:00Z",
            "next_timestamp": "2026-09-22T12:45:00Z",
            "data": [event(market_last_update="2026-09-22T12:39:00Z")],
        }
        seen = {}

        def transport(url, timeout):
            seen["url"] = url
            return HttpJsonResponse(
                payload, 200, {"x-requests-last": "10"}
            )

        provider = TheOddsApiProvider(
            "k",
            sport="soccer_epl",
            max_market_age_seconds=120,
            transport=transport,
            clock=lambda: "2026-09-22T14:00:00+00:00",
        )
        snapshot = provider.read_historical_snapshot("2026-09-22T12:42:00Z")
        self.assertEqual(snapshot.requested_at, "2026-09-22T12:42:00Z")
        self.assertEqual(snapshot.snapshot_at, "2026-09-22T12:40:00Z")
        self.assertEqual(snapshot.batch.cursor, "2026-09-22T12:40:00Z")
        self.assertEqual(
            snapshot.batch.quotes[0].metadata["historical_snapshot_at"],
            "2026-09-22T12:40:00Z",
        )
        self.assertEqual(snapshot.request_evidence.quota_last, 10)
        self.assertIn("date=2026-09-22T12%3A42%3A00Z", seen["url"])

    def test_historical_observed_at_is_sampled_after_response_returns(self):
        response_returned = {"value": False}
        payload = {
            "timestamp": "2026-09-22T12:40:00Z",
            "previous_timestamp": None,
            "next_timestamp": None,
            "data": [event(market_last_update="2026-09-22T12:39:00Z")],
        }

        def transport(url, timeout):
            response_returned["value"] = True
            return HttpJsonResponse(payload, 200, {})

        def clock():
            self.assertTrue(response_returned["value"])
            return "2026-09-22T14:00:01+00:00"

        provider = TheOddsApiProvider(
            "k",
            sport="soccer_epl",
            transport=transport,
            clock=clock,
        )
        snapshot = provider.read_historical_snapshot(
            "2026-09-22T12:42:00Z"
        )

        self.assertEqual(
            snapshot.batch.quotes[0].observed_ts,
            "2026-09-22T14:00:01+00:00",
        )
        self.assertEqual(
            snapshot.request_evidence.observed_at,
            "2026-09-22T14:00:01+00:00",
        )

    def test_historical_market_update_after_actual_snapshot_fails_closed(self):
        payload = {
            "timestamp": "2026-09-22T12:40:00Z",
            "previous_timestamp": None,
            "next_timestamp": None,
            "data": [event(market_last_update="2026-09-22T12:41:00Z")],
        }
        provider = TheOddsApiProvider(
            "k",
            sport="soccer_epl",
            transport=lambda *_: HttpJsonResponse(payload, 200, {}),
            clock=lambda: "2026-09-22T14:00:00+00:00",
        )
        with self.assertRaisesRegex(TheOddsApiPayloadError, "historical snapshot"):
            provider.read_historical_snapshot("2026-09-22T12:42:00Z")

    def test_historical_correction_changes_deterministic_sequence(self):
        payloads = iter(
            [
                {
                    "timestamp": "2026-09-22T12:40:00Z",
                    "previous_timestamp": None,
                    "next_timestamp": None,
                    "data": [
                        event(
                            price=Decimal("2.10"),
                            market_last_update="2026-09-22T12:39:00Z",
                        )
                    ],
                },
                {
                    "timestamp": "2026-09-22T12:40:00Z",
                    "previous_timestamp": None,
                    "next_timestamp": None,
                    "data": [
                        event(
                            price=Decimal("2.11"),
                            market_last_update="2026-09-22T12:39:00Z",
                        )
                    ],
                },
            ]
        )
        response_digests = iter(("1" * 64, "2" * 64))

        def transport(url, timeout):
            return HttpJsonResponse(
                next(payloads),
                200,
                {},
                body_sha256=next(response_digests),
            )

        provider = TheOddsApiProvider(
            "k",
            sport="soccer_epl",
            transport=transport,
            clock=lambda: "2026-09-22T14:00:00+00:00",
        )
        first = provider.read_historical_snapshot("2026-09-22T12:42:00Z")
        second = provider.read_historical_snapshot("2026-09-22T12:42:00Z")
        self.assertEqual(first.snapshot_at, second.snapshot_at)
        self.assertNotEqual(first.batch.cursor, second.batch.cursor)
        self.assertEqual(first.batch.cursor, "1" * 64)
        self.assertEqual(second.batch.cursor, "2" * 64)
        self.assertNotEqual(
            first.batch.quotes[0].sequence,
            second.batch.quotes[0].sequence,
        )

    def test_batch_pagination_is_local_and_does_not_refetch_until_snapshot_consumed(self):
        payload = event()
        payload["bookmakers"][0]["markets"][0]["outcomes"].append(
            {
                "name": "Beta",
                "price": Decimal("1.80"),
                "sid": "outcome-100",
            }
        )
        calls = []

        def transport(url, timeout):
            calls.append(url)
            return HttpJsonResponse([payload], 200, {})

        provider = TheOddsApiProvider(
            "k",
            sport="soccer_epl",
            transport=transport,
            clock=lambda: "2026-09-22T14:00:00+00:00",
        )
        first = provider.read_batch(max_items=1)
        second = provider.read_batch(max_items=1)
        self.assertIn("TRUNCATED_BATCH", first.quality_flags)
        self.assertNotIn("TRUNCATED_BATCH", second.quality_flags)
        self.assertEqual(len(calls), 1)

    def test_secret_bearing_network_origin_is_pinned_and_timeout_is_finite(self):
        with self.assertRaisesRegex(ValueError, "canonical The Odds API"):
            TheOddsApiProvider(
                "secret",
                sport="soccer_epl",
                base_url="https://attacker.example",
            )
        with self.assertRaisesRegex(ValueError, "finite positive"):
            TheOddsApiProvider(
                "secret",
                sport="soccer_epl",
                timeout_seconds=float("inf"),
            )

    def test_default_transport_bounds_response_before_decode_and_digest(self):
        seen = {}

        class OversizedResponse:
            status = 200
            headers = {}

            def __init__(self, final_url):
                self.final_url = final_url

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, size=-1):
                seen["read_size"] = size
                return b"x" * size

            def geturl(self):
                return self.final_url

        class FakeOpener:
            def open(self, request, timeout):
                return OversizedResponse(request.full_url)

        url = (
            "https://api.the-odds-api.com/v4/sports/soccer_epl/odds"
            "?apiKey=secret&regions=eu&markets=h2h"
        )
        with patch.object(
            odds_api_module,
            "build_opener",
            lambda *_: FakeOpener(),
        ):
            with self.assertRaisesRegex(TheOddsApiPayloadError, "bounded size"):
                odds_api_module._default_transport(url, 1.0)

        self.assertEqual(
            seen["read_size"],
            odds_api_module.THE_ODDS_API_MAX_RESPONSE_BYTES + 1,
        )

    def test_default_transport_binds_exact_response_digest_into_evidence(self):
        raw_body = (
            b'[{"id":"0123456789abcdef0123456789abcdef",'
            b'"sport_key":"soccer_epl","commence_time":"2026-09-22T15:00:00Z",'
            b'"bookmakers":[{"key":"book-a","sid":"event-77","markets":[{'
            b'"key":"h2h","last_update":"2026-09-22T13:50:00Z",'
            b'"sid":"market-88","outcomes":[{"name":"Alpha","price":2.10,'
            b'"sid":"outcome-99"}]}]}]}]'
        )

        class FakeResponse:
            status = 200
            headers = {}

            def __init__(self, final_url):
                self.final_url = final_url

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, size=-1):
                self.read_size = size
                return raw_body

            def geturl(self):
                return self.final_url

        class FakeOpener:
            def open(self, request, timeout):
                return FakeResponse(request.full_url)

        provider = TheOddsApiProvider(
            "secret",
            sport="soccer_epl",
            clock=lambda: "2026-09-22T14:00:00+00:00",
        )
        with patch.object(
            odds_api_module,
            "build_opener",
            lambda *_: FakeOpener(),
        ):
            batch = provider.read_batch()

        expected = hashlib.sha256(raw_body).hexdigest()
        request = batch.quotes[0].metadata["request"]
        self.assertTrue(request["provider_origin_verified"])
        self.assertFalse(request["receipt_clock_verified"])
        self.assertEqual(request["response_sha256"], expected)
        self.assertEqual(batch.cursor, expected)
        self.assertEqual(batch.quotes[0].decimal_odds, Decimal("2.10"))

    def test_pending_current_pages_keep_snapshot_local_provenance(self):
        raw_body = (
            b'[{"id":"0123456789abcdef0123456789abcdef",'
            b'"sport_key":"soccer_epl","commence_time":"2026-09-22T15:00:00Z",'
            b'"bookmakers":[{"key":"book-a","sid":"event-77","markets":[{'
            b'"key":"h2h","last_update":"2026-09-22T13:50:00Z",'
            b'"sid":"market-88","outcomes":['
            b'{"name":"Alpha","price":2.10,"sid":"outcome-99"},'
            b'{"name":"Beta","price":1.80,"sid":"outcome-100"}]}]}]}]'
        )

        class FakeResponse:
            status = 200
            headers = {}

            def __init__(self, final_url):
                self.final_url = final_url

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self, size=-1):
                return raw_body

            def geturl(self):
                return self.final_url

        class FakeOpener:
            def open(self, request, timeout):
                return FakeResponse(request.full_url)

        provider = TheOddsApiProvider(
            "secret",
            sport="soccer_epl",
            max_market_age_seconds=None,
        )
        with patch.object(
            odds_api_module,
            "build_opener",
            lambda *_: FakeOpener(),
        ):
            first = provider.read_batch(max_items=1)

        self.assertIn("TRUNCATED_BATCH", first.quality_flags)
        self.assertNotIn("UNVERIFIED_PROVIDER_ORIGIN", first.quality_flags)
        self.assertNotIn("UNVERIFIED_RECEIPT_CLOCK", first.quality_flags)

        historical_payload = {
            "timestamp": "2026-09-22T12:40:00Z",
            "previous_timestamp": None,
            "next_timestamp": None,
            "data": [
                event(
                    market_last_update="2026-09-22T12:39:00Z",
                )
            ],
        }
        provider.transport = lambda *_: HttpJsonResponse(
            historical_payload,
            200,
            {},
        )
        provider.clock = lambda: "2026-09-22T14:00:00+00:00"
        historical = provider.read_historical_snapshot("2026-09-22T12:42:00Z")
        self.assertIn(
            "UNVERIFIED_PROVIDER_ORIGIN",
            historical.batch.quality_flags,
        )

        second = provider.read_batch(max_items=1)
        self.assertNotIn("TRUNCATED_BATCH", second.quality_flags)
        self.assertNotIn("UNVERIFIED_PROVIDER_ORIGIN", second.quality_flags)
        self.assertNotIn("UNVERIFIED_RECEIPT_CLOCK", second.quality_flags)

    def test_post_construction_seam_swap_cannot_retain_verified_authority(self):
        digest = "a" * 64
        provider = TheOddsApiProvider(
            "secret",
            sport="soccer_epl",
        )
        provider.transport = lambda url, timeout: HttpJsonResponse(
            [event()],
            200,
            {},
            final_url=url,
            body_sha256=digest,
        )
        provider.clock = lambda: "2026-09-22T14:00:00+00:00"

        batch = provider.read_batch()
        request = batch.quotes[0].metadata["request"]

        self.assertFalse(request["provider_origin_verified"])
        self.assertFalse(request["receipt_clock_verified"])
        self.assertIn("UNVERIFIED_PROVIDER_ORIGIN", batch.quality_flags)
        self.assertIn("UNVERIFIED_RECEIPT_CLOCK", batch.quality_flags)
        self.assertEqual(request["response_sha256"], digest)

    def test_response_rejects_event_outside_explicit_event_ids_scope(self):
        provider = TheOddsApiProvider(
            "k",
            sport="soccer_epl",
            event_ids=("different-event",),
            transport=lambda *_: HttpJsonResponse([event()], 200, {}),
            clock=lambda: "2026-09-22T14:00:00+00:00",
        )
        with self.assertRaisesRegex(TheOddsApiPayloadError, "eventIds request scope"):
            provider.read_batch()

    def test_response_rejects_bookmaker_outside_explicit_bookmakers_scope(self):
        payload = event()
        payload["bookmakers"][0]["key"] = "book-b"
        provider = TheOddsApiProvider(
            "k",
            sport="soccer_epl",
            regions=(),
            bookmakers=("book-a",),
            transport=lambda *_: HttpJsonResponse([payload], 200, {}),
            clock=lambda: "2026-09-22T14:00:00+00:00",
        )
        with self.assertRaisesRegex(
            TheOddsApiPayloadError,
            "bookmakers request scope",
        ):
            provider.read_batch()

    def test_response_rejects_market_outside_requested_markets_scope(self):
        payload = event(
            market_key="totals",
            point=Decimal("2.5"),
        )
        provider = TheOddsApiProvider(
            "k",
            sport="soccer_epl",
            markets=("h2h",),
            transport=lambda *_: HttpJsonResponse([payload], 200, {}),
            clock=lambda: "2026-09-22T14:00:00+00:00",
        )
        with self.assertRaisesRegex(TheOddsApiPayloadError, "requested markets scope"):
            provider.read_batch()

    def test_historical_response_uses_the_same_request_scope_fence(self):
        payload = {
            "timestamp": "2026-09-22T12:40:00Z",
            "previous_timestamp": None,
            "next_timestamp": None,
            "data": [
                event(
                    market_key="totals",
                    point=Decimal("2.5"),
                    market_last_update="2026-09-22T12:39:00Z",
                )
            ],
        }
        provider = TheOddsApiProvider(
            "k",
            sport="soccer_epl",
            markets=("h2h",),
            transport=lambda *_: HttpJsonResponse(payload, 200, {}),
            clock=lambda: "2026-09-22T14:00:00+00:00",
        )
        with self.assertRaisesRegex(TheOddsApiPayloadError, "requested markets scope"):
            provider.read_historical_snapshot("2026-09-22T12:42:00Z")

    def test_injected_transport_and_clock_are_explicitly_non_authoritative(self):
        provider = TheOddsApiProvider(
            "k",
            sport="soccer_epl",
            transport=lambda *_: HttpJsonResponse([event()], 200, {}),
            clock=lambda: "2026-09-22T14:00:00+00:00",
        )
        batch = provider.read_batch()
        request = batch.quotes[0].metadata["request"]

        self.assertFalse(request["provider_origin_verified"])
        self.assertFalse(request["receipt_clock_verified"])
        self.assertIn("UNVERIFIED_PROVIDER_ORIGIN", batch.quality_flags)
        self.assertIn("UNVERIFIED_RECEIPT_CLOCK", batch.quality_flags)

    def test_default_transport_rejects_redirect_shape_and_final_url_drift(self):
        seen = {}

        class FakeResponse:
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return b"[]"

            def geturl(self):
                return "https://attacker.example/steal?apiKey=secret"

        class FakeOpener:
            def open(self, request, timeout):
                seen["request_url"] = request.full_url
                return FakeResponse()

        def fake_build_opener(handler):
            seen["handler"] = handler
            return FakeOpener()

        url = (
            "https://api.the-odds-api.com/v4/sports/soccer_epl/odds"
            "?apiKey=secret&regions=eu&markets=h2h"
        )
        with patch.object(odds_api_module, "build_opener", fake_build_opener):
            with self.assertRaisesRegex(
                TheOddsApiTransportError,
                "final URL",
            ):
                odds_api_module._default_transport(url, 1.0)

        self.assertIsInstance(seen["handler"], odds_api_module._RejectRedirects)
        self.assertEqual(seen["request_url"], url)

    def test_secret_bearing_http_error_context_is_not_retained(self):
        secret = "SECRET_SENTINEL_DO_NOT_RETAIN"
        url = (
            "https://api.the-odds-api.com/v4/sports/soccer_epl/odds"
            f"?apiKey={secret}&regions=eu&markets=h2h"
        )

        class FailingOpener:
            def open(self, request, timeout):
                raise HTTPError(request.full_url, 401, "Unauthorized", {}, None)

        with patch.object(
            odds_api_module,
            "build_opener",
            lambda *_: FailingOpener(),
        ):
            with self.assertRaises(TheOddsApiTransportError) as context:
                odds_api_module._default_transport(url, 1.0)

        self.assertEqual(context.exception.status_code, 401)
        self.assertIsNone(context.exception.__cause__)
        self.assertIsNone(context.exception.__context__)
        self.assertNotIn(secret, str(context.exception))

    def test_adapter_exposes_no_provider_write_or_real_money_surface(self):
        provider = TheOddsApiProvider(
            "k",
            sport="soccer_epl",
            transport=lambda *_: None,
        )
        for name in (
            "place_bet",
            "cancel_bet",
            "cashout",
            "settle",
            "deposit",
            "withdraw",
        ):
            self.assertFalse(hasattr(provider, name))


if __name__ == "__main__":
    unittest.main()

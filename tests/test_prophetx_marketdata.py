import hashlib
import json
import unittest
from unittest.mock import patch
from decimal import Decimal, localcontext

from autosport.domain import MarketType
from autosport.prophetx_marketdata import (
    ProphetXJsonResponse,
    ProphetXPayloadError,
    ProphetXRestMarketProvider,
    ProphetXTransportError,
    _decode_provider_json,
    _default_transport,
    american_to_decimal,
)
from autosport.providers import CanonicalNormalizer, ProviderUnavailableError


def _response(payload, status_code=200):
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return ProphetXJsonResponse(
        payload=payload,
        status_code=status_code,
        headers={},
        body_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _market_payload():
    return {
        "data": {
            "markets": [
                {
                    "event_id": 1001,
                    "market_id": "market-1",
                    "type": "moneyline",
                    "status": "open",
                    "sub_type": None,
                    "selections": [
                        [
                            {
                                "strike_id": "strike-a",
                                "outcome_id": "outcome-a",
                                "competitor_id": "team-a",
                                "name": "Team A",
                                "price": 150,
                                "quantity": "12.50",
                            },
                            {
                                "strike_id": "strike-a",
                                "outcome_id": "outcome-a",
                                "competitor_id": "team-a",
                                "name": "Team A",
                                "price": 140,
                                "quantity": "5",
                            },
                        ],
                        [
                            {
                                "strike_id": "strike-b",
                                "outcome_id": "outcome-b",
                                "competitor_id": "team-b",
                                "name": "Team B",
                                "price": -200,
                                "quantity": "9",
                            }
                        ],
                    ],
                }
            ]
        }
    }


class ProphetXMarketDataTests(unittest.TestCase):
    def _provider(self, payload=None, *, clock="2026-09-22T19:00:00+00:00"):
        calls = []

        def transport(url, headers, timeout):
            calls.append((url, dict(headers), timeout))
            return _response(_market_payload() if payload is None else payload)

        provider = ProphetXRestMarketProvider(
            "secret-market-token",
            (1001,),
            transport=transport,
            clock=lambda: clock,
        )
        return provider, calls

    def test_transport_error_is_typed_provider_unavailability(self):
        error = ProphetXTransportError("unavailable", 503)
        self.assertIsInstance(error, ProviderUnavailableError)
        self.assertEqual(error.status_code, 503)

    @patch("autosport.prophetx_marketdata.build_opener")
    def test_builtin_transport_timeout_is_typed_and_sanitized(self, build_opener):
        build_opener.return_value.open.side_effect = TimeoutError(
            "socket timeout secret-provider-detail"
        )
        with self.assertRaises(ProphetXTransportError) as caught:
            _default_transport(
                "https://api.sandbox.prophetx.dev/partner/v3/affiliate/get_markets"
                "?event_id=1001&get_all_market=true",
                {"Authorization": "Bearer should-not-leak"},
                1.0,
            )
        self.assertEqual(str(caught.exception), "provider transport unavailable")
        self.assertIsNone(caught.exception.status_code)

    def test_fixed_read_only_market_endpoint_keeps_token_out_of_url(self):
        provider, calls = self._provider()
        provider.read_batch()
        self.assertEqual(len(calls), 1)
        url, headers, timeout = calls[0]
        self.assertEqual(
            url,
            "https://api.sandbox.prophetx.dev/partner/v3/affiliate/get_markets"
            "?event_id=1001&get_all_market=true",
        )
        self.assertNotIn("secret-market-token", url)
        self.assertEqual(headers["Authorization"], "Bearer secret-market-token")
        self.assertEqual(headers["Accept"], "application/json")
        self.assertEqual(timeout, 10.0)

    def test_strike_id_is_canonical_instrument_not_outcome_id(self):
        provider, _ = self._provider()
        batch = provider.read_batch()
        self.assertEqual(
            [quote.provider_selection_id for quote in batch.quotes],
            ["strike-a", "strike-b"],
        )
        self.assertNotEqual(
            batch.quotes[0].provider_selection_id,
            batch.quotes[0].metadata["outcome_id"],
        )

    def test_opposing_outcomes_are_distinct_buy_only_instruments(self):
        provider, _ = self._provider()
        batch = provider.read_batch()
        self.assertEqual(len(batch.quotes), 2)
        self.assertEqual(batch.quotes[0].provider_market_id, batch.quotes[1].provider_market_id)
        self.assertNotEqual(
            batch.quotes[0].provider_selection_id,
            batch.quotes[1].provider_selection_id,
        )
        for quote in batch.quotes:
            self.assertNotIn("side", quote.metadata)
            self.assertNotIn("sell", quote.metadata)

    def test_multiple_liquidity_levels_are_preserved_without_selection_id_fabrication(self):
        provider, _ = self._provider()
        quote = provider.read_batch().quotes[0]
        self.assertEqual(quote.provider_selection_id, "strike-a")
        self.assertEqual(quote.decimal_odds, Decimal("2.5"))
        self.assertEqual(quote.metadata["depth_level_count"], 2)
        self.assertEqual(
            quote.metadata["depth_levels"],
            [
                {
                    "american_odds": "150",
                    "decimal_odds": "2.5",
                    "quantity": "12.50",
                },
                {
                    "american_odds": "140",
                    "decimal_odds": "2.4",
                    "quantity": "5",
                },
            ],
        )
        self.assertEqual(
            quote.metadata["primary_level_semantics"],
            "provider_order_level_0",
        )
        self.assertTrue(quote.metadata["depth_is_aggregated"])
        self.assertFalse(quote.metadata["execution_fillability_proven"])

    def test_positive_and_negative_american_odds_convert_exactly_for_finite_cases(self):
        self.assertEqual(american_to_decimal(150), Decimal("2.5"))
        self.assertEqual(american_to_decimal(-200), Decimal("1.5"))

    def test_repeating_american_conversion_uses_fixed_context_not_ambient_context(self):
        expected = Decimal("1.909090909090909090909090909090909")
        with localcontext() as context:
            context.prec = 6
            self.assertEqual(american_to_decimal(-110), expected)

    def test_binary_float_price_is_rejected_at_adapter_boundary(self):
        payload = _market_payload()
        payload["data"]["markets"][0]["selections"][0][0]["price"] = 150.0
        provider, _ = self._provider(payload)
        with self.assertRaisesRegex(ProphetXPayloadError, "integer ingress"):
            provider.read_batch()

    def test_binary_float_liquidity_is_rejected_at_adapter_boundary(self):
        payload = _market_payload()
        payload["data"]["markets"][0]["selections"][0][0]["quantity"] = 12.5
        provider, _ = self._provider(payload)
        with self.assertRaisesRegex(ProphetXPayloadError, "exact decimal ingress"):
            provider.read_batch()

    def test_invalid_american_price_fails_closed(self):
        for bad in (0, 99, -99, "100.5", "NaN"):
            with self.subTest(bad=bad):
                payload = _market_payload()
                payload["data"]["markets"][0]["selections"][0][0]["price"] = bad
                provider, _ = self._provider(payload)
                with self.assertRaises(ProphetXPayloadError):
                    provider.read_batch()

    def test_negative_or_nonfinite_liquidity_fails_closed(self):
        for bad in ("-0.01", "NaN", "Infinity"):
            with self.subTest(bad=bad):
                payload = _market_payload()
                payload["data"]["markets"][0]["selections"][0][0]["quantity"] = bad
                provider, _ = self._provider(payload)
                with self.assertRaises(ProphetXPayloadError):
                    provider.read_batch()

    def test_market_event_identity_must_match_exact_requested_event(self):
        payload = _market_payload()
        payload["data"]["markets"][0]["event_id"] = 1002
        provider, _ = self._provider(payload)
        with self.assertRaisesRegex(ProphetXPayloadError, "requested event"):
            provider.read_batch()

    def test_duplicate_market_id_is_rejected(self):
        payload = _market_payload()
        payload["data"]["markets"].append(dict(payload["data"]["markets"][0]))
        provider, _ = self._provider(payload)
        with self.assertRaisesRegex(ProphetXPayloadError, "duplicate market_id"):
            provider.read_batch()

    def test_duplicate_strike_id_is_rejected(self):
        payload = _market_payload()
        payload["data"]["markets"][0]["selections"].append(
            [{"strike_id": "strike-a", "price": 130, "quantity": "1"}]
        )
        provider, _ = self._provider(payload)
        with self.assertRaisesRegex(ProphetXPayloadError, "duplicate strike_id"):
            provider.read_batch()

    def test_liquidity_levels_cannot_cross_wire_strike_identity(self):
        payload = _market_payload()
        payload["data"]["markets"][0]["selections"][0][1]["strike_id"] = "strike-x"
        provider, _ = self._provider(payload)
        with self.assertRaisesRegex(ProphetXPayloadError, "disagree on strike_id"):
            provider.read_batch()

    def test_liquidity_levels_cannot_cross_wire_outcome_identity(self):
        payload = _market_payload()
        payload["data"]["markets"][0]["selections"][0][1]["outcome_id"] = "outcome-x"
        provider, _ = self._provider(payload)
        with self.assertRaisesRegex(ProphetXPayloadError, "disagree on outcome_id"):
            provider.read_batch()

    def test_duplicate_price_level_is_rejected_not_double_counted(self):
        payload = _market_payload()
        payload["data"]["markets"][0]["selections"][0][1]["price"] = 150
        provider, _ = self._provider(payload)
        with self.assertRaisesRegex(ProphetXPayloadError, "duplicate price level"):
            provider.read_batch()

    def test_unknown_or_missing_market_status_never_becomes_open(self):
        unknown = _market_payload()
        unknown["data"]["markets"][0]["status"] = "HALTED_FUTURE_ENUM"
        provider, _ = self._provider(unknown)
        quote = provider.read_batch().quotes[0]
        self.assertEqual(quote.status, "unknown")
        self.assertEqual(quote.metadata["market_status_raw"], "HALTED_FUTURE_ENUM")

        missing = _market_payload()
        missing["data"]["markets"][0].pop("status")
        provider, _ = self._provider(missing)
        quote = provider.read_batch().quotes[0]
        self.assertEqual(quote.status, "unknown")
        self.assertIsNone(quote.metadata["market_status_raw"])

    def test_source_timestamp_is_not_fabricated_from_receive_time(self):
        provider, _ = self._provider()
        quote = provider.read_batch().quotes[0]
        self.assertIsNone(quote.source_ts)
        self.assertFalse(quote.metadata["provider_source_timestamp_available"])
        self.assertEqual(quote.observed_ts, "2026-09-22T19:00:00+00:00")

    def test_market_type_mapping_is_narrow_and_unknown_types_remain_other(self):
        mapping = {
            "moneyline": MarketType.WINNER,
            "sup_moneyline": MarketType.WINNER,
            "moneyline_3_way": MarketType.WINNER,
            "spread": MarketType.HANDICAP,
            "total": MarketType.TOTAL,
            "future_new_market": MarketType.OTHER,
        }
        for raw, expected in mapping.items():
            with self.subTest(raw=raw):
                payload = _market_payload()
                payload["data"]["markets"][0]["type"] = raw
                provider, _ = self._provider(payload)
                self.assertEqual(provider.read_batch().quotes[0].market_type, expected)

    def test_sub_type_is_optional_and_never_synthesized(self):
        payload = _market_payload()
        payload["data"]["markets"][0].pop("sub_type")
        provider, _ = self._provider(payload)
        quote = provider.read_batch().quotes[0]
        self.assertIsNone(quote.metadata["market_sub_type"])

    def test_exact_response_digest_is_bound_to_every_quote(self):
        payload = _market_payload()
        response = _response(payload)

        def transport(*_):
            return response

        provider = ProphetXRestMarketProvider(
            "token",
            (1001,),
            transport=transport,
            clock=lambda: "2026-09-22T19:00:00+00:00",
        )
        for quote in provider.read_batch().quotes:
            self.assertEqual(quote.metadata["response_sha256"], response.body_sha256)

    def test_batch_cursor_is_payload_bound_and_not_receive_time_bound(self):
        payload = _market_payload()

        def make(clock):
            provider = ProphetXRestMarketProvider(
                "token",
                (1001,),
                transport=lambda *_: _response(payload),
                clock=lambda: clock,
            )
            return provider.read_batch().cursor

        self.assertEqual(
            make("2026-09-22T19:00:00+00:00"),
            make("2026-09-22T19:00:01+00:00"),
        )

    def test_receive_timestamp_drives_monotone_current_sequence(self):
        first, _ = self._provider(clock="2026-09-22T19:00:00+00:00")
        second, _ = self._provider(clock="2026-09-22T19:00:01+00:00")
        self.assertLess(
            first.read_batch().quotes[0].sequence,
            second.read_batch().quotes[0].sequence,
        )

    def test_max_items_slices_one_acquired_snapshot_without_refetch(self):
        provider, calls = self._provider()
        first = provider.read_batch(max_items=1)
        second = provider.read_batch(max_items=1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(first.quotes), 1)
        self.assertEqual(len(second.quotes), 1)
        self.assertEqual(first.cursor, second.cursor)
        self.assertIn("TRUNCATED_BATCH", first.quality_flags)
        self.assertNotIn("TRUNCATED_BATCH", second.quality_flags)

    def test_empty_market_snapshot_is_explicit_narrow_empty_truth(self):
        provider, _ = self._provider({"data": {"markets": []}})
        batch = provider.read_batch()
        self.assertEqual(batch.quotes, ())
        self.assertIn("PROVIDER_DECLARED_EMPTY_MARKET", batch.quality_flags)
        self.assertIn("BOUNDED_PROVIDER_DEPTH", batch.quality_flags)

    def test_unsupported_payload_shape_fails_closed(self):
        provider, _ = self._provider({"data": {"unexpected": []}})
        with self.assertRaisesRegex(ProphetXPayloadError, "unsupported shape"):
            provider.read_batch()

    def test_non_200_injected_response_is_unavailability_not_empty(self):
        provider = ProphetXRestMarketProvider(
            "token",
            (1001,),
            transport=lambda *_: _response({}, 429),
        )
        with self.assertRaises(ProphetXTransportError) as caught:
            provider.read_batch()
        self.assertEqual(caught.exception.status_code, 429)

    def test_transport_exception_text_is_sanitized_and_token_not_reemitted(self):
        token = "top-secret-token"

        def transport(*_):
            raise ProphetXTransportError(f"socket error {token}", 503)

        provider = ProphetXRestMarketProvider(token, (1001,), transport=transport)
        with self.assertRaises(ProphetXTransportError) as caught:
            provider.read_batch()
        self.assertNotIn(token, str(caught.exception))
        self.assertEqual(caught.exception.status_code, 503)

    def test_strict_json_decoder_rejects_duplicate_keys_and_nonfinite_constants(self):
        with self.assertRaisesRegex(ProphetXPayloadError, "duplicate JSON key"):
            _decode_provider_json(b'{"data":1,"data":2}')
        with self.assertRaisesRegex(ProphetXPayloadError, "non-standard JSON constant"):
            _decode_provider_json(b'{"price":NaN}')

    def test_json_decoder_preserves_decimal_ingress_without_binary_float(self):
        decoded = _decode_provider_json(b'{"quantity":12.50}')
        self.assertEqual(decoded["quantity"], Decimal("12.50"))
        self.assertIs(type(decoded["quantity"]), Decimal)

    def test_event_configuration_is_exact_and_duplicate_free(self):
        for bad in ((), (True,), (0,), (-1,), (1001, 1001), [1001]):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    ProphetXRestMarketProvider("token", bad)  # type: ignore[arg-type]

    def test_canonical_normalizer_accepts_metadata_without_decimal_type_drift(self):
        provider, _ = self._provider()
        batch = provider.read_batch()
        event = CanonicalNormalizer().normalize(batch.source_id, batch.quotes[0])
        self.assertEqual(event.selection_id, "prophetx:sandbox:strike-a")
        self.assertEqual(event.metadata["depth_levels"][0]["quantity"], "12.50")
        self.assertEqual(event.metadata["environment"], "sandbox")

    def test_adapter_exposes_no_write_or_account_surface(self):
        provider, _ = self._provider()
        for name in (
            "submit_order",
            "submit_multiple_orders",
            "place_order",
            "cancel_order",
            "get_balance",
            "login",
        ):
            self.assertFalse(hasattr(provider, name))


if __name__ == "__main__":
    unittest.main()

import unittest
from decimal import Decimal, localcontext

from autosport.betfair_fill_or_kill import (
    BetfairFillOrKillError,
    BetfairFillOrKillImmediateReport,
    BetfairFillOrKillRequest,
    FillOrKillStructuralOutcome,
    inspect_betfair_fill_or_kill_lifecycle,
    resolve_betfair_fill_or_kill_lifecycle,
)


class BetfairFillOrKillTests(unittest.TestCase):
    RESPONSE_SHA = "a" * 64

    def _request(self, **overrides):
        values = {
            "market_id": "1.23456789",
            "selection_id": "12345",
            "requested_size": "5",
            "limit_price": "21",
        }
        values.update(overrides)
        return BetfairFillOrKillRequest(**values)

    def _report(self, request, **overrides):
        values = {
            "request_projection_sha256": request.request_projection_sha256,
            "response_sha256": self.RESPONSE_SHA,
            "top_status": "SUCCESS",
            "instruction_status": "SUCCESS",
            "size_matched": "0",
            "average_price_matched": "0",
            "bet_id": "72666364933",
        }
        values.update(overrides)
        return BetfairFillOrKillImmediateReport(**values)

    def test_request_projection_is_exact_fok_lapse_limit(self):
        request = self._request(min_fill_size="3")
        self.assertEqual(
            request.provider_instruction,
            {
                "selectionId": 12345,
                "handicap": 0,
                "side": "BACK",
                "orderType": "LIMIT",
                "limitOrder": {
                    "size": "5",
                    "price": "21",
                    "persistenceType": "LAPSE",
                    "timeInForce": "FILL_OR_KILL",
                    "minFillSize": "3",
                },
            },
        )
        self.assertEqual(len(request.request_projection_sha256), 64)

    def test_rejects_standard_limit_persist_lay_or_non_limit(self):
        cases = (
            ({"time_in_force": "GOOD_TILL_CANCELLED"}, "FILL_OR_KILL"),
            ({"persistence_type": "PERSIST"}, "LAPSE"),
            ({"side": "LAY"}, "BACK only"),
            ({"order_type": "MARKET_ON_CLOSE"}, "LIMIT"),
        )
        for overrides, message in cases:
            with self.subTest(overrides=overrides), self.assertRaisesRegex(
                BetfairFillOrKillError, message
            ):
                self._request(**overrides)

    def test_min_fill_is_threshold_and_cannot_exceed_requested_size(self):
        request = self._request(min_fill_size="3")
        report = self._report(
            request,
            size_matched="3.44",
            average_price_matched="21.32267441860465",
        )
        evidence = inspect_betfair_fill_or_kill_lifecycle(request, report)
        self.assertEqual(
            evidence.outcome,
            FillOrKillStructuralOutcome.MINIMUM_MATCHED_REMAINDER_CANCELLED,
        )
        self.assertEqual(evidence.size_matched, Decimal("3.44"))
        self.assertEqual(evidence.unmatched_remainder, Decimal("1.56"))
        self.assertTrue(evidence.unmatched_remainder_terminal_by_fok_contract)

        with self.assertRaisesRegex(BetfairFillOrKillError, "cannot exceed"):
            self._request(min_fill_size="6")

    def test_success_with_zero_match_is_killed_not_accepted_fill(self):
        request = self._request(min_fill_size="5")
        evidence = inspect_betfair_fill_or_kill_lifecycle(
            request,
            self._report(request),
        )
        self.assertEqual(evidence.outcome, FillOrKillStructuralOutcome.KILLED_ZERO)
        self.assertEqual(evidence.size_matched, Decimal("0"))
        self.assertEqual(evidence.unmatched_remainder, Decimal("5"))
        self.assertFalse(evidence.provider_origin_verified)
        self.assertFalse(evidence.grants_execution_authority)
        self.assertFalse(evidence.grants_real_money_authority)

    def test_without_min_fill_positive_partial_match_is_invalid(self):
        request = self._request()
        report = self._report(
            request,
            size_matched="3",
            average_price_matched="21.5",
        )
        with self.assertRaisesRegex(BetfairFillOrKillError, "fully match or match zero"):
            inspect_betfair_fill_or_kill_lifecycle(request, report)

    def test_without_min_fill_full_match_is_valid(self):
        request = self._request()
        report = self._report(
            request,
            size_matched="5",
            average_price_matched="21.1",
        )
        evidence = inspect_betfair_fill_or_kill_lifecycle(request, report)
        self.assertEqual(evidence.outcome, FillOrKillStructuralOutcome.FULLY_MATCHED)
        self.assertEqual(evidence.unmatched_remainder, Decimal("0"))

    def test_positive_match_below_minimum_is_invalid(self):
        request = self._request(min_fill_size="3")
        report = self._report(
            request,
            size_matched="2.99",
            average_price_matched="21.5",
        )
        with self.assertRaisesRegex(BetfairFillOrKillError, "below the declared"):
            inspect_betfair_fill_or_kill_lifecycle(request, report)

    def test_fok_price_is_aggregate_vwap_floor_not_fragment_floor(self):
        request = self._request(min_fill_size="3")
        report = self._report(
            request,
            size_matched="3.44",
            average_price_matched="21.32267441860465",
        )
        evidence = inspect_betfair_fill_or_kill_lifecycle(request, report)
        self.assertTrue(evidence.aggregate_vwap_limit_satisfied)
        self.assertFalse(evidence.per_fragment_price_floor_proven)

        below = self._report(
            request,
            size_matched="3.44",
            average_price_matched="20.99",
        )
        with self.assertRaisesRegex(BetfairFillOrKillError, "VWAP is below"):
            inspect_betfair_fill_or_kill_lifecycle(request, below)

    def test_report_must_bind_exact_request_projection(self):
        request = self._request(min_fill_size="3")
        report = self._report(
            request,
            request_projection_sha256="b" * 64,
        )
        with self.assertRaisesRegex(BetfairFillOrKillError, "exact FOK request"):
            inspect_betfair_fill_or_kill_lifecycle(request, report)

    def test_report_cannot_overmatch_or_claim_vwap_without_match(self):
        request = self._request()
        over = self._report(
            request,
            size_matched="5.01",
            average_price_matched="21.5",
        )
        with self.assertRaisesRegex(BetfairFillOrKillError, "more than"):
            inspect_betfair_fill_or_kill_lifecycle(request, over)

        with self.assertRaisesRegex(BetfairFillOrKillError, "zero matched size"):
            self._report(
                request,
                size_matched="0",
                average_price_matched="21",
            )

    def test_non_success_immediate_report_is_not_structurally_terminalized_here(self):
        request = self._request()
        for field in ("top_status", "instruction_status"):
            kwargs = {field: "FAILURE"}
            with self.subTest(field=field), self.assertRaisesRegex(
                BetfairFillOrKillError, "SUCCESS/SUCCESS"
            ):
                self._report(request, **kwargs)

    def test_float_and_noncanonical_selection_inputs_fail_closed(self):
        with self.assertRaisesRegex(BetfairFillOrKillError, "bool/float"):
            self._request(requested_size=5.0)
        with self.assertRaisesRegex(BetfairFillOrKillError, "positive integer text"):
            self._request(selection_id="00123")

    def test_extreme_decimal_exponents_fail_before_fixed_point_materialization(self):
        for field, value in (
            ("requested_size", Decimal("1E+1000000")),
            ("requested_size", Decimal("1E-1000000")),
        ):
            with self.subTest(field=field, value=str(value)), self.assertRaisesRegex(
                BetfairFillOrKillError,
                "fixed-point representation exceeds resource bound",
            ):
                self._request(**{field: value})

        request = self._request()
        for field, value in (
            ("size_matched", Decimal("0E-1000000")),
            ("average_price_matched", Decimal("1E+1000000")),
        ):
            with self.subTest(field=field, value=str(value)), self.assertRaisesRegex(
                BetfairFillOrKillError,
                "fixed-point representation exceeds resource bound",
            ):
                self._report(request, **{field: value})

    def test_unmatched_remainder_is_exact_and_context_independent(self):
        requested = Decimal("1.2345678901234567890123456789")
        matched = Decimal("0.0000000000000000000000000001")
        expected_remainder = Decimal("1.2345678901234567890123456788")
        request = self._request(
            requested_size=requested,
            min_fill_size=matched,
        )
        report = self._report(
            request,
            size_matched=matched,
            average_price_matched="21.5",
        )

        with localcontext() as context:
            context.prec = 5
            low_precision = inspect_betfair_fill_or_kill_lifecycle(request, report)
        with localcontext() as context:
            context.prec = 80
            high_precision = inspect_betfair_fill_or_kill_lifecycle(request, report)

        self.assertEqual(low_precision.unmatched_remainder, expected_remainder)
        self.assertEqual(high_precision.unmatched_remainder, expected_remainder)
        self.assertEqual(low_precision, high_precision)
        self.assertEqual(low_precision.evidence_id, high_precision.evidence_id)

    def test_structural_evidence_is_deterministic(self):
        request = self._request(min_fill_size="3")
        report = self._report(
            request,
            size_matched="3.44",
            average_price_matched="21.32267441860465",
        )
        first = inspect_betfair_fill_or_kill_lifecycle(request, report)
        second = inspect_betfair_fill_or_kill_lifecycle(request, report)
        self.assertEqual(first, second)
        self.assertEqual(first.evidence_id, second.evidence_id)
        self.assertEqual(len(first.evidence_id), 64)

    def test_positive_resolver_fails_closed_without_origin_authority(self):
        request = self._request(min_fill_size="3")
        report = self._report(
            request,
            size_matched="3.44",
            average_price_matched="21.32267441860465",
        )
        with self.assertRaisesRegex(
            BetfairFillOrKillError,
            "canonical product-issued request and provider-origin authority",
        ):
            resolve_betfair_fill_or_kill_lifecycle(request, report)


if __name__ == "__main__":
    unittest.main()

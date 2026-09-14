import unittest
from decimal import Decimal, Overflow, ROUND_DOWN, Underflow, localcontext

from autosport.candidate_search import BeamParlayCandidateSearch, CandidateLeg


class CandidateSearchDecimalRangeTests(unittest.TestCase):
    @staticmethod
    def _leg(event_id: str, odds: str, probability: str = "1") -> CandidateLeg:
        return CandidateLeg(
            f"{event_id}|winner|selection",
            event_id,
            Decimal(odds),
            Decimal(probability),
        )

    def test_finite_per_leg_odds_that_overflow_combination_fail_closed(self) -> None:
        search = BeamParlayCandidateSearch(beam_width=4, max_legs=2, result_limit=4)
        legs = [
            self._leg("event-1", "1E+999999"),
            self._leg("event-2", "1E+999999"),
        ]

        with self.assertRaisesRegex(
            ValueError,
            "candidate combined economics exceed Decimal range",
        ):
            search.search(legs, minimum_legs=2)

    def test_disabled_decimal_overflow_trap_cannot_publish_finite_saturation(self) -> None:
        search = BeamParlayCandidateSearch(beam_width=4, max_legs=2, result_limit=4)
        legs = [
            self._leg("event-1", "1E+999999"),
            self._leg("event-2", "1E+999999"),
        ]

        with localcontext() as context:
            context.traps[Overflow] = False
            context.rounding = ROUND_DOWN
            with self.assertRaisesRegex(
                ValueError,
                "candidate combined economics exceed Decimal range",
            ):
                search.search(legs, minimum_legs=2)

    def test_finite_nonzero_probabilities_that_underflow_combination_fail_closed(self) -> None:
        search = BeamParlayCandidateSearch(beam_width=4, max_legs=2, result_limit=4)
        legs = [
            self._leg("event-1", "2", "1E-999999"),
            self._leg("event-2", "2", "1E-999999"),
        ]

        with localcontext() as context:
            self.assertFalse(context.traps[Underflow])
            with self.assertRaisesRegex(
                ValueError,
                "candidate combined economics exceed Decimal range",
            ):
                search.search(legs, minimum_legs=2)

    def test_trapped_per_leg_underflow_is_normalized_before_ranking(self) -> None:
        search = BeamParlayCandidateSearch(beam_width=4, max_legs=2, result_limit=4)
        legs = [
            self._leg("event-1", "2", "1E-999999999"),
            self._leg("event-2", "2", "0.5"),
        ]

        with localcontext() as context:
            context.traps[Underflow] = True
            with self.assertRaisesRegex(
                ValueError,
                "candidate combined economics exceed Decimal range",
            ):
                search.search(legs, minimum_legs=2)

    def test_exact_zero_probability_is_not_mistaken_for_underflow(self) -> None:
        search = BeamParlayCandidateSearch(beam_width=4, max_legs=2, result_limit=4)
        legs = [
            self._leg("event-1", "2", "0"),
            self._leg("event-2", "2", "0.5"),
        ]

        result = search.search(legs, minimum_legs=2)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].combined_odds, Decimal("4"))
        self.assertEqual(result[0].independent_probability, Decimal("0"))
        self.assertEqual(result[0].expected_profit_per_unit, Decimal("-1"))

    def test_large_representable_candidate_remains_valid(self) -> None:
        search = BeamParlayCandidateSearch(beam_width=4, max_legs=2, result_limit=4)
        legs = [
            self._leg("event-1", "1E+100", "1E-100"),
            self._leg("event-2", "1E+100", "1E-100"),
        ]

        result = search.search(legs, minimum_legs=2)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].combined_odds, Decimal("1E+200"))
        self.assertEqual(result[0].independent_probability, Decimal("1E-200"))
        self.assertEqual(result[0].expected_profit_per_unit, Decimal("0"))


if __name__ == "__main__":
    unittest.main()

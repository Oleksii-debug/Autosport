import unittest
from decimal import Decimal, Overflow, localcontext

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

    def test_disabled_decimal_overflow_trap_cannot_publish_infinity(self) -> None:
        search = BeamParlayCandidateSearch(beam_width=4, max_legs=2, result_limit=4)
        legs = [
            self._leg("event-1", "1E+999999"),
            self._leg("event-2", "1E+999999"),
        ]

        with localcontext() as context:
            context.traps[Overflow] = False
            with self.assertRaisesRegex(
                ValueError,
                "candidate combined economics exceed Decimal range",
            ):
                search.search(legs, minimum_legs=2)

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

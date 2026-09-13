import unittest
from decimal import Decimal

from autosport.candidate_search import BeamParlayCandidateSearch, CandidateLeg


class CandidateSearchInputIntegrityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.search = BeamParlayCandidateSearch(beam_width=20, max_legs=3, result_limit=20)

    @staticmethod
    def _leg(
        quote_key: str = "event-1|winner|alice",
        event_id: str = "event-1",
        odds: Decimal = Decimal("2.00"),
        probability: Decimal = Decimal("0.55"),
    ) -> CandidateLeg:
        return CandidateLeg(quote_key, event_id, odds, probability)

    def test_non_finite_odds_fail_before_beam_sort_or_multiplication(self) -> None:
        for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with self.subTest(value=str(value)):
                with self.assertRaisesRegex(ValueError, "decimal_odds must be finite"):
                    self.search.search([self._leg(odds=value)], minimum_legs=1)

    def test_non_finite_probability_fails_before_beam_sort_or_multiplication(self) -> None:
        for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with self.subTest(value=str(value)):
                with self.assertRaisesRegex(ValueError, "probability must be finite"):
                    self.search.search([self._leg(probability=value)], minimum_legs=1)

    def test_economic_ranges_fail_closed(self) -> None:
        for odds in (Decimal("0"), Decimal("1"), Decimal("-2")):
            with self.subTest(odds=str(odds)):
                with self.assertRaisesRegex(ValueError, "decimal_odds must be greater than 1"):
                    self.search.search([self._leg(odds=odds)], minimum_legs=1)
        for probability in (Decimal("-0.01"), Decimal("1.01")):
            with self.subTest(probability=str(probability)):
                with self.assertRaisesRegex(ValueError, "probability must be between 0 and 1"):
                    self.search.search([self._leg(probability=probability)], minimum_legs=1)

    def test_identity_must_be_canonical_and_event_bound(self) -> None:
        invalid = (
            self._leg(quote_key=""),
            self._leg(quote_key=" event-1|winner|alice"),
            self._leg(quote_key="event-1|winner"),
            self._leg(quote_key="event-1||alice"),
            self._leg(event_id="event-2"),
        )
        for leg in invalid:
            with self.subTest(quote_key=leg.quote_key, event_id=leg.event_id):
                with self.assertRaises(ValueError):
                    self.search.search([leg], minimum_legs=1)

    def test_economics_require_decimal_instead_of_implicit_coercion(self) -> None:
        with self.assertRaisesRegex(ValueError, "decimal_odds must be Decimal"):
            self.search.search([self._leg(odds="2.0")], minimum_legs=1)  # type: ignore[arg-type]
        with self.assertRaisesRegex(ValueError, "probability must be Decimal"):
            self.search.search([self._leg(probability="0.5")], minimum_legs=1)  # type: ignore[arg-type]

    def test_duplicate_quote_key_fails_instead_of_order_dependent_overwrite(self) -> None:
        first = self._leg()
        second = self._leg(odds=Decimal("2.20"), probability=Decimal("0.51"))
        with self.assertRaisesRegex(ValueError, "duplicate candidate quote_key"):
            self.search.search([first, second], minimum_legs=1)

    def test_valid_inputs_still_generate_deterministic_finite_candidates(self) -> None:
        legs = [
            self._leg(),
            self._leg(
                quote_key="event-2|winner|bob",
                event_id="event-2",
                odds=Decimal("1.90"),
                probability=Decimal("0.60"),
            ),
        ]
        first = self.search.search(legs, minimum_legs=2)
        second = self.search.search(list(reversed(legs)), minimum_legs=2)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 1)
        self.assertEqual(
            tuple(leg.quote_key for leg in first[0].legs),
            ("event-1|winner|alice", "event-2|winner|bob"),
        )
        self.assertTrue(first[0].combined_odds.is_finite())
        self.assertTrue(first[0].independent_probability.is_finite())
        self.assertTrue(first[0].expected_profit_per_unit.is_finite())


if __name__ == "__main__":
    unittest.main()

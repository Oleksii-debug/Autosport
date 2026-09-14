import unittest
from decimal import Decimal

from autosport.candidate_optimizer import PortfolioAwareCandidateOptimizer
from autosport.candidate_search import CandidateLeg, ParlayCandidate
from autosport.scenario_search import ScenarioGroup, ScenarioOutcome


def _candidate(event_id: str) -> ParlayCandidate:
    leg = CandidateLeg(
        f"{event_id}|winner|a",
        event_id,
        Decimal("2"),
        Decimal("0.5"),
    )
    return ParlayCandidate(
        (leg,),
        leg.decimal_odds,
        leg.probability,
        Decimal("0"),
    )


def _groups() -> list[ScenarioGroup]:
    return [
        ScenarioGroup(
            event_id,
            (
                ScenarioOutcome(f"{event_id}|winner|a", Decimal("0.5")),
                ScenarioOutcome(f"{event_id}|winner|b", Decimal("0.5")),
            ),
        )
        for event_id in ("e1", "e2")
    ]


class CandidateOptimizerInputDeterminismTests(unittest.TestCase):
    def test_result_limit_requires_positive_non_boolean_integer(self) -> None:
        for invalid in (True, False, 1.5, Decimal("2"), "2", None):
            with self.subTest(value=repr(invalid)):
                with self.assertRaisesRegex(
                    ValueError,
                    "result_limit must be a positive non-boolean integer",
                ):
                    PortfolioAwareCandidateOptimizer(result_limit=invalid)  # type: ignore[arg-type]

        for invalid in (0, -1):
            with self.subTest(value=invalid):
                with self.assertRaisesRegex(
                    ValueError,
                    "result_limit must be a positive non-boolean integer",
                ):
                    PortfolioAwareCandidateOptimizer(result_limit=invalid)

    def test_equal_rank_candidates_have_input_order_independent_limit_selection(self) -> None:
        first = _candidate("e1")
        second = _candidate("e2")
        optimizer = PortfolioAwareCandidateOptimizer(result_limit=1)

        forward = optimizer.evaluate_candidates(
            [],
            [first, second],
            _groups(),
            stake="1",
        )
        reversed_input = optimizer.evaluate_candidates(
            [],
            [second, first],
            _groups(),
            stake="1",
        )

        self.assertEqual(len(forward), 1)
        self.assertEqual(len(reversed_input), 1)
        self.assertEqual(
            forward[0].candidate.legs[0].quote_key,
            reversed_input[0].candidate.legs[0].quote_key,
        )

    def test_canonical_tie_break_includes_economic_leg_values(self) -> None:
        low_probability_leg = CandidateLeg(
            "e1|winner|a",
            "e1",
            Decimal("2"),
            Decimal("0.4"),
        )
        high_probability_leg = CandidateLeg(
            "e1|winner|a",
            "e1",
            Decimal("2"),
            Decimal("0.6"),
        )
        low_probability = ParlayCandidate(
            (low_probability_leg,),
            Decimal("2"),
            Decimal("0.4"),
            Decimal("-0.2"),
        )
        high_probability = ParlayCandidate(
            (high_probability_leg,),
            Decimal("2"),
            Decimal("0.6"),
            Decimal("0.2"),
        )
        groups = [
            ScenarioGroup(
                "e1",
                (
                    ScenarioOutcome("e1|winner|a", Decimal("0.5")),
                    ScenarioOutcome("e1|winner|b", Decimal("0.5")),
                ),
            )
        ]

        forward = PortfolioAwareCandidateOptimizer().evaluate_candidates(
            [],
            [low_probability, high_probability],
            groups,
            stake="1",
        )
        reversed_input = PortfolioAwareCandidateOptimizer().evaluate_candidates(
            [],
            [high_probability, low_probability],
            groups,
            stake="1",
        )

        self.assertEqual(
            [item.candidate.independent_probability for item in forward],
            [item.candidate.independent_probability for item in reversed_input],
        )


if __name__ == "__main__":
    unittest.main()

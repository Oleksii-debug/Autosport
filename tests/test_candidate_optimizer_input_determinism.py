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


if __name__ == "__main__":
    unittest.main()

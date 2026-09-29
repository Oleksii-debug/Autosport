import unittest
from decimal import Decimal

from autosport.candidate_optimizer import PortfolioAwareCandidateOptimizer
from autosport.candidate_search import CandidateLeg, ParlayCandidate
from autosport.scenario_search import ScenarioGroup, ScenarioOutcome


class CandidateEventIsolationTests(unittest.TestCase):
    def test_manual_candidate_cannot_bypass_generator_event_isolation_via_separate_groups(self):
        winner = CandidateLeg(
            "match-1|winner|alice",
            "match-1",
            Decimal("2.00"),
            Decimal("0.55"),
        )
        total = CandidateLeg(
            "match-1|total|over",
            "match-1",
            Decimal("1.90"),
            Decimal("0.60"),
        )
        candidate = ParlayCandidate(
            (winner, total),
            Decimal("3.8000"),
            Decimal("0.3300"),
            Decimal("0.254000"),
        )
        groups = [
            ScenarioGroup(
                "match-1-winner",
                (
                    ScenarioOutcome(winner.quote_key, Decimal("0.55")),
                    ScenarioOutcome("match-1|winner|bob", Decimal("0.45")),
                ),
            ),
            ScenarioGroup(
                "match-1-total",
                (
                    ScenarioOutcome(total.quote_key, Decimal("0.60")),
                    ScenarioOutcome("match-1|total|under", Decimal("0.40")),
                ),
            ),
        ]

        with self.assertRaisesRegex(ValueError, "multiple legs from one event"):
            PortfolioAwareCandidateOptimizer().evaluate_candidates(
                [],
                [candidate],
                groups,
                stake="10",
            )


if __name__ == "__main__":
    unittest.main()

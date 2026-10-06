import unittest
from decimal import Decimal

from autosport.candidate_optimizer import PortfolioAwareCandidateOptimizer
from autosport.candidate_search import CandidateLeg, ParlayCandidate
from autosport.scenario_search import ScenarioGroup, ScenarioOutcome


def _candidate(leg: CandidateLeg) -> ParlayCandidate:
    probability = leg.probability
    odds = leg.decimal_odds
    return ParlayCandidate(
        (leg,),
        odds,
        probability,
        probability * odds - Decimal("1"),
    )


def _group(leg: CandidateLeg) -> ScenarioGroup:
    return ScenarioGroup(
        "event-winner",
        (
            ScenarioOutcome(leg.quote_key, Decimal("0.5")),
            ScenarioOutcome("event|winner|other", Decimal("0.5")),
        ),
    )


class CandidateOptimizerFiniteEconomicsTests(unittest.TestCase):
    def test_non_finite_synthetic_stake_fails_before_portfolio_math(self) -> None:
        leg = CandidateLeg("event|winner|alice", "event", Decimal("2"), Decimal("0.5"))
        optimizer = PortfolioAwareCandidateOptimizer()
        for value in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "stake must be finite"):
                    optimizer.evaluate_candidates([], [_candidate(leg)], [_group(leg)], stake=value)

    def test_non_finite_candidate_odds_fail_before_scenario_evaluation(self) -> None:
        optimizer = PortfolioAwareCandidateOptimizer()
        for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with self.subTest(value=str(value)):
                leg = CandidateLeg("event|winner|alice", "event", value, Decimal("0.5"))
                with self.assertRaisesRegex(ValueError, "decimal odds must be finite"):
                    optimizer.evaluate_candidates([], [_candidate(leg)], [_group(leg)], stake="1")

    def test_non_finite_candidate_probability_fails_before_scenario_evaluation(self) -> None:
        optimizer = PortfolioAwareCandidateOptimizer()
        for value in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
            with self.subTest(value=str(value)):
                leg = CandidateLeg("event|winner|alice", "event", Decimal("2"), value)
                with self.assertRaisesRegex(ValueError, "probability must be finite"):
                    optimizer.evaluate_candidates([], [_candidate(leg)], [_group(leg)], stake="1")

    def test_finite_candidate_still_produces_finite_impact(self) -> None:
        leg = CandidateLeg("event|winner|alice", "event", Decimal("2"), Decimal("0.5"))
        impact = PortfolioAwareCandidateOptimizer().evaluate_candidates(
            [], [_candidate(leg)], [_group(leg)], stake="1"
        )[0]
        self.assertTrue(impact.stake.is_finite())
        self.assertTrue(impact.ranking_risk_change.is_finite())
        self.assertTrue(impact.standalone_expected_profit.is_finite())


if __name__ == "__main__":
    unittest.main()

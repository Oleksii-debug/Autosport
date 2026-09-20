from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal

from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.live_opportunity_proof import (
    ApplicableCostCoverage,
    LiveFreshnessFence,
    OpportunityProofClassification,
    OpportunityProofError,
    classify_executable_opportunity,
)
from autosport.opportunity import Opportunity, OpportunityDecision, QuoteRef, StrategyClass
from autosport.portfolio_plan import EvidenceTruth, OpportunityEvidence, OpportunityIntent
from autosport.risk import ProposedTicketRiskContext
from autosport.scenario_search import ScenarioSearchReport


class LiveOpportunityProofTests(unittest.TestCase):
    DECISION_AT = "2026-09-18T13:20:00+00:00"

    @staticmethod
    def _goal() -> EconomicGoalContract:
        return EconomicGoalContract(
            goal_id="goal-live-proof",
            revision=1,
            bankroll_id="paper-bankroll",
            currency="USD",
            max_stake_fraction=Decimal("0.10"),
            max_session_loss_fraction=Decimal("1"),
            max_day_loss_fraction=Decimal("1"),
            max_drawdown_fraction=Decimal("1"),
            max_capital_at_risk_fraction=Decimal("1"),
            max_turnover_fraction=Decimal("1000"),
            max_risk_of_ruin=Decimal("1"),
            max_execution_slippage_fraction=Decimal("1"),
            max_quote_age_seconds=Decimal("3600"),
            max_concurrent_positions=10,
        )

    @classmethod
    def _intent(
        cls,
        *,
        strategy: StrategyClass = StrategyClass.ARBITRAGE,
        complete: bool = True,
        execution_feasible: bool = True,
        execution_bound: bool = True,
        truth: EvidenceTruth = EvidenceTruth.EXACT,
        decision: OpportunityDecision = OpportunityDecision.ACTIONABLE,
    ) -> OpportunityIntent:
        goal = cls._goal()
        legs = (
            TicketLeg(
                "event-1",
                "market-1",
                "home",
                Decimal("2.10"),
                sport="soccer",
            ),
            TicketLeg(
                "event-1",
                "market-1",
                "away",
                Decimal("2.10"),
                sport="soccer",
            ),
        )
        quotes = (
            MarketEvent(
                event_id="event-1",
                market_id="market-1",
                selection_id="home",
                decimal_odds=Decimal("2.10"),
                observed_ts="2026-09-18T13:19:59+00:00",
                source_id="provider-a",
                sequence=1,
                source_ts="2026-09-18T13:19:59+00:00",
                ingest_ts="2026-09-18T13:19:59+00:00",
                sport="soccer",
            ),
            MarketEvent(
                event_id="event-1",
                market_id="market-1",
                selection_id="away",
                decimal_odds=Decimal("2.10"),
                observed_ts="2026-09-18T13:19:59+00:00",
                source_id="provider-b",
                sequence=1,
                source_ts="2026-09-18T13:19:59+00:00",
                ingest_ts="2026-09-18T13:19:59+00:00",
                sport="soccer",
            ),
        )
        context = ProposedTicketRiskContext(
            legs=legs,
            quotes=quotes,
            bankroll_id=goal.bankroll_id,
            currency=goal.currency,
            proposal_ts=cls.DECISION_AT,
        )
        opportunity = Opportunity(
            strategy_class=strategy,
            decision=decision,
            quotes=tuple(
                QuoteRef.from_market_event(
                    quote,
                    market_snapshot_hash="9" * 64,
                )
                for quote in quotes
            ),
        )
        evidence = OpportunityEvidence(
            evidence_id="live-proof-evidence",
            observed_at="2026-09-18T13:19:59+00:00",
            causal_cutoff="2026-09-18T13:19:58+00:00",
            reproducibility_sha256="a" * 64,
            truth=truth,
            outcome_space_complete=complete,
            terminal_state_space_sha256="c" * 64 if complete else None,
            execution_assumptions_sha256=(
                "d" * 64 if execution_bound else None
            ),
            execution_feasible=execution_feasible,
        )
        return OpportunityIntent(
            intent_id="intent-live-proof",
            opportunity=opportunity,
            evidence=evidence,
            risk_context=context,
            signal_strength=Decimal("0.05"),
            strategy_id="strategy-live-proof",
            config_sha256="e" * 64,
        )

    @staticmethod
    def _scenario(
        *,
        worst: Decimal = Decimal("5"),
        exact: bool = True,
        exhaustive: bool = True,
        worst_proven: bool = True,
    ) -> ScenarioSearchReport:
        return ScenarioSearchReport(
            mode=(
                "authoritative-exact-enumeration"
                if exact
                else "authoritative-conservative-enumeration"
            ),
            total_states=2,
            nodes_explored=2,
            observed_worst=worst,
            observed_best=Decimal("10"),
            conservative_floor=Decimal("-100"),
            conservative_ceiling=Decimal("100"),
            worst_proven=worst_proven,
            best_proven=exact,
            expected_case=None,
            expected_mode=None,
            outcome_space_exhaustive=exhaustive,
            outcome_space_exact=exact,
            outcome_authority_sha256s=("f" * 64,),
        )

    @staticmethod
    def _cost(
        intent: OpportunityIntent,
        *,
        amount: Decimal = Decimal("1"),
        complete: bool = True,
    ) -> ApplicableCostCoverage:
        return ApplicableCostCoverage(
            intent_sha256=intent.intent_sha256,
            total_applicable_cost=amount,
            coverage_complete=complete,
            evidence_sha256="1" * 64,
        )

    @staticmethod
    def _fresh(
        intent: OpportunityIntent,
        *,
        valid_until: str = "2026-09-18T13:20:05+00:00",
    ) -> LiveFreshnessFence:
        return LiveFreshnessFence(
            intent_sha256=intent.intent_sha256,
            valid_until=valid_until,
            policy_sha256="2" * 64,
        )

    def _classify(
        self,
        intent: OpportunityIntent,
        scenario: ScenarioSearchReport | None = None,
        *,
        cost: ApplicableCostCoverage | None = None,
        fresh: LiveFreshnessFence | None = None,
        decision_at: str | None = None,
    ):
        return classify_executable_opportunity(
            intent,
            scenario or self._scenario(),
            cost_coverage=cost or self._cost(intent),
            freshness=fresh or self._fresh(intent),
            decision_at=decision_at or self.DECISION_AT,
        )

    def test_exact_complete_current_after_cost_arbitrage_is_theoretical_until_product_authority_resolves(self) -> None:
        intent = self._intent()
        proof = self._classify(intent)
        self.assertEqual(
            proof.classification,
            OpportunityProofClassification.THEORETICAL_ARBITRAGE_ONLY,
        )
        self.assertEqual(proof.gross_min_pnl, Decimal("5"))
        self.assertEqual(proof.after_cost_min_pnl, Decimal("4"))
        self.assertIn("caller assertions", proof.reason)
        self.assertEqual(len(proof.proof_sha256), 64)

    def test_caller_forged_zero_cost_complete_witness_cannot_mint_positive(self) -> None:
        intent = self._intent()
        forged = self._cost(intent, amount=Decimal("0"), complete=True)
        proof = self._classify(intent, cost=forged)
        self.assertNotEqual(
            proof.classification,
            OpportunityProofClassification.OUTCOME_INDEPENDENT_POSITIVE,
        )

    def test_caller_forged_future_freshness_cannot_mint_positive(self) -> None:
        intent = self._intent()
        forged = self._fresh(intent, valid_until="2099-01-01T00:00:00+00:00")
        proof = self._classify(intent, fresh=forged)
        self.assertNotEqual(
            proof.classification,
            OpportunityProofClassification.OUTCOME_INDEPENDENT_POSITIVE,
        )

    def test_caller_constructed_exact_scenario_cannot_mint_positive(self) -> None:
        intent = self._intent()
        forged = self._scenario(worst=Decimal("999"), exact=True, exhaustive=True)
        proof = self._classify(intent, scenario=forged)
        self.assertNotEqual(
            proof.classification,
            OpportunityProofClassification.OUTCOME_INDEPENDENT_POSITIVE,
        )

    def test_scenario_from_another_intent_cannot_mint_positive(self) -> None:
        first = self._intent()
        second = replace(first, intent_id="other-intent")
        scenario_from_first = self._scenario()
        proof = self._classify(second, scenario=scenario_from_first)
        self.assertNotEqual(
            proof.classification,
            OpportunityProofClassification.OUTCOME_INDEPENDENT_POSITIVE,
        )

    def test_incomplete_outcome_space_is_partial_coverage(self) -> None:
        intent = self._intent(complete=False)
        proof = self._classify(intent)
        self.assertEqual(
            proof.classification,
            OpportunityProofClassification.PARTIAL_COVERAGE,
        )

    def test_conservative_or_approximate_terminal_space_is_theoretical_only(self) -> None:
        intent = self._intent()
        proof = self._classify(
            intent,
            self._scenario(exact=False, worst_proven=False),
        )
        self.assertEqual(
            proof.classification,
            OpportunityProofClassification.THEORETICAL_ARBITRAGE_ONLY,
        )

    def test_approximate_opportunity_evidence_is_theoretical_only(self) -> None:
        intent = self._intent(truth=EvidenceTruth.APPROXIMATE)
        proof = self._classify(intent)
        self.assertEqual(
            proof.classification,
            OpportunityProofClassification.THEORETICAL_ARBITRAGE_ONLY,
        )

    def test_incomplete_cost_coverage_cannot_create_guarantee(self) -> None:
        intent = self._intent()
        proof = self._classify(intent, cost=self._cost(intent, complete=False))
        self.assertEqual(
            proof.classification,
            OpportunityProofClassification.THEORETICAL_ARBITRAGE_ONLY,
        )

    def test_complete_costs_can_remove_theoretical_positive_floor(self) -> None:
        intent = self._intent()
        proof = self._classify(
            intent,
            cost=self._cost(intent, amount=Decimal("6")),
        )
        self.assertEqual(
            proof.classification,
            OpportunityProofClassification.THEORETICAL_ARBITRAGE_ONLY,
        )
        self.assertEqual(proof.after_cost_min_pnl, Decimal("-1"))

    def test_nonpositive_exact_gross_floor_is_risked_portfolio(self) -> None:
        intent = self._intent()
        proof = self._classify(
            intent,
            self._scenario(worst=Decimal("0")),
            cost=self._cost(intent, amount=Decimal("0")),
        )
        self.assertEqual(
            proof.classification,
            OpportunityProofClassification.RISKED_PORTFOLIO,
        )

    def test_execution_infeasible_or_unbound_is_execution_risk(self) -> None:
        infeasible = self._intent(execution_feasible=False)
        self.assertEqual(
            self._classify(infeasible).classification,
            OpportunityProofClassification.EXECUTION_RISK_PRESENT,
        )
        unbound = self._intent(execution_bound=False)
        self.assertEqual(
            self._classify(unbound).classification,
            OpportunityProofClassification.EXECUTION_RISK_PRESENT,
        )

    def test_stale_quote_fence_is_execution_risk(self) -> None:
        intent = self._intent()
        proof = self._classify(
            intent,
            fresh=self._fresh(
                intent,
                valid_until="2026-09-18T13:19:59.500000+00:00",
            ),
        )
        self.assertEqual(
            proof.classification,
            OpportunityProofClassification.EXECUTION_RISK_PRESENT,
        )

    def test_decision_before_observation_fails_closed(self) -> None:
        intent = self._intent()
        with self.assertRaisesRegex(OpportunityProofError, "cannot precede"):
            self._classify(
                intent,
                decision_at="2026-09-18T13:19:58+00:00",
            )

    def test_hedge_does_not_inherit_arbitrage_guarantee(self) -> None:
        intent = self._intent(strategy=StrategyClass.HEDGE_REBALANCE)
        proof = self._classify(intent)
        self.assertEqual(
            proof.classification,
            OpportunityProofClassification.HEDGED_BUT_NOT_GUARANTEED,
        )

    def test_wait_decision_is_never_actionable_guarantee(self) -> None:
        intent = self._intent(decision=OpportunityDecision.WAIT)
        proof = self._classify(intent)
        self.assertEqual(
            proof.classification,
            OpportunityProofClassification.RISKED_PORTFOLIO,
        )

    def test_cross_intent_cost_and_freshness_evidence_fail_closed(self) -> None:
        first = self._intent()
        second = replace(first, intent_id="other-intent")
        with self.assertRaisesRegex(OpportunityProofError, "different intent"):
            self._classify(first, cost=self._cost(second))
        with self.assertRaisesRegex(OpportunityProofError, "different intent"):
            self._classify(first, fresh=self._fresh(second))

    def test_equivalent_timezone_spelling_has_same_proof_identity(self) -> None:
        intent = self._intent()
        utc = self._classify(intent, decision_at="2026-09-18T13:20:00Z")
        plus_two = self._classify(
            intent,
            decision_at="2026-09-18T15:20:00+02:00",
        )
        self.assertEqual(utc.proof_sha256, plus_two.proof_sha256)
        self.assertEqual(utc.decision_at, "2026-09-18T13:20:00Z")

    def test_subclass_spoofing_is_rejected(self) -> None:
        intent = self._intent()

        class ForgedIntent(OpportunityIntent):
            pass

        forged = ForgedIntent(
            intent_id=intent.intent_id,
            opportunity=intent.opportunity,
            evidence=intent.evidence,
            risk_context=intent.risk_context,
            signal_strength=intent.signal_strength,
            strategy_id=intent.strategy_id,
            config_sha256=intent.config_sha256,
            model_id=intent.model_id,
        )
        with self.assertRaisesRegex(OpportunityProofError, "exact canonical"):
            self._classify(forged)


if __name__ == "__main__":
    unittest.main()

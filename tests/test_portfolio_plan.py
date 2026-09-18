import unittest
from dataclasses import replace
from decimal import Decimal

from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.paper import PaperBook
from autosport.portfolio_plan import (
    EvidenceTruth,
    OpportunityClass,
    OpportunityEvidence,
    OpportunityIntent,
    PortfolioAction,
    PortfolioPlan,
    build_portfolio_plan,
)
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext


class PortfolioPlanTests(unittest.TestCase):
    DECISION_TS = "2026-09-18T13:20:00+00:00"

    @staticmethod
    def _goal(**overrides: object) -> EconomicGoalContract:
        values: dict[str, object] = {
            "goal_id": "goal-portfolio-plan",
            "revision": 1,
            "bankroll_id": "paper-bankroll",
            "currency": "USD",
            "max_stake_fraction": Decimal("0.10"),
            "max_session_loss_fraction": Decimal("1"),
            "max_day_loss_fraction": Decimal("1"),
            "max_drawdown_fraction": Decimal("1"),
            "max_capital_at_risk_fraction": Decimal("1"),
            "max_turnover_fraction": Decimal("1000"),
            "max_risk_of_ruin": Decimal("1"),
            "max_execution_slippage_fraction": Decimal("1"),
            "max_quote_age_seconds": Decimal("3600"),
            "max_concurrent_positions": 10,
        }
        values.update(overrides)
        return EconomicGoalContract(**values)  # type: ignore[arg-type]

    @staticmethod
    def _policy(goal: EconomicGoalContract) -> PaperRiskPolicy:
        return PaperRiskPolicy(
            max_ticket_fraction=Decimal("1"),
            max_committed_fraction=Decimal("1"),
            minimum_cash_reserve_fraction=Decimal("0"),
            economic_goal=goal,
        )

    @classmethod
    def _context(
        cls,
        goal: EconomicGoalContract,
        *,
        suffix: str = "1",
    ) -> ProposedTicketRiskContext:
        leg = TicketLeg(
            f"event-{suffix}",
            f"market-{suffix}",
            f"selection-{suffix}",
            Decimal("2"),
        )
        quote = MarketEvent(
            event_id=leg.event_id,
            market_id=leg.market_id,
            selection_id=leg.selection_id,
            decimal_odds=Decimal("2"),
            observed_ts="2026-09-18T13:19:59+00:00",
            source_id=f"provider-{suffix}",
            sequence=1,
            source_ts="2026-09-18T13:19:59+00:00",
            ingest_ts="2026-09-18T13:19:59+00:00",
        )
        return ProposedTicketRiskContext(
            legs=(leg,),
            quotes=(quote,),
            bankroll_id=goal.bankroll_id,
            currency=goal.currency,
            proposal_ts=cls.DECISION_TS,
        )

    @staticmethod
    def _evidence(
        *,
        evidence_id: str = "evidence-1",
        forecast: bool = False,
        truth: EvidenceTruth = EvidenceTruth.EXACT,
        outcome_space_complete: bool = False,
        execution_feasible: bool = True,
    ) -> OpportunityEvidence:
        return OpportunityEvidence(
            evidence_id=evidence_id,
            observed_at="2026-09-18T13:19:59+00:00",
            causal_cutoff="2026-09-18T13:19:58+00:00",
            reproducibility_sha256="a" * 64,
            truth=truth,
            forecast_sha256=("b" * 64 if forecast else None),
            outcome_space_complete=outcome_space_complete,
            terminal_state_space_sha256=(
                "c" * 64 if outcome_space_complete else None
            ),
            execution_assumptions_sha256=(
                "d" * 64 if outcome_space_complete else None
            ),
            execution_feasible=execution_feasible,
        )

    @classmethod
    def _intent(
        cls,
        goal: EconomicGoalContract,
        *,
        suffix: str = "1",
        opportunity_class: OpportunityClass = OpportunityClass.PREDICTIVE_EDGE,
        signal: Decimal = Decimal("0.05"),
        evidence: OpportunityEvidence | None = None,
    ) -> OpportunityIntent:
        needs_forecast = opportunity_class in {
            OpportunityClass.PREDICTIVE_EDGE,
            OpportunityClass.HYBRID,
        }
        return OpportunityIntent(
            intent_id=f"intent-{suffix}",
            opportunity_class=opportunity_class,
            evidence=evidence
            or cls._evidence(
                evidence_id=f"evidence-{suffix}",
                forecast=needs_forecast,
                outcome_space_complete=opportunity_class
                in {OpportunityClass.ARBITRAGE, OpportunityClass.DUTCHING},
            ),
            risk_context=cls._context(goal, suffix=suffix),
            signal_strength=signal,
            strategy_id=f"strategy-{suffix}",
            model_id=(f"model-{suffix}" if needs_forecast else None),
            config_sha256="e" * 64,
        )

    def test_predictive_intent_delegates_stake_math_to_canonical_risk_policy(
        self,
    ) -> None:
        goal = self._goal()
        policy = self._policy(goal)
        book = PaperBook("1000")
        intent = self._intent(goal)

        plan = build_portfolio_plan(
            book,
            (intent,),
            policy,
            self.DECISION_TS,
            dependency_graph_sha256="f" * 64,
        )

        self.assertEqual(plan.action, PortfolioAction.STAKE_VECTOR)
        self.assertEqual(plan.stakes, (Decimal("50.00"),))
        self.assertEqual(book.balance, Decimal("1000"))
        self.assertEqual(book.tickets, {})
        self.assertEqual(plan.risk_policy_sha256, policy.provenance_sha256)
        self.assertIsNotNone(plan.economic_goal_contract_sha256)
        self.assertEqual(len(plan.plan_sha256), 64)

    def test_nonforecast_arbitrage_uses_same_portfolio_risk_path(self) -> None:
        goal = self._goal()
        intent = self._intent(
            goal,
            opportunity_class=OpportunityClass.ARBITRAGE,
            signal=Decimal("0.04"),
        )

        plan = build_portfolio_plan(
            PaperBook("1000"),
            (intent,),
            self._policy(goal),
            self.DECISION_TS,
            dependency_graph_sha256="f" * 64,
        )

        self.assertEqual(plan.action, PortfolioAction.STAKE_VECTOR)
        self.assertEqual(plan.stakes, (Decimal("40.00"),))
        self.assertIsNone(intent.evidence.forecast_sha256)
        self.assertIsNone(intent.model_id)

    def test_predictive_and_hybrid_require_forecast_model_identity(self) -> None:
        goal = self._goal()
        with self.assertRaisesRegex(ValueError, "requires forecast_sha256"):
            OpportunityIntent(
                intent_id="bad-predictive",
                opportunity_class=OpportunityClass.PREDICTIVE_EDGE,
                evidence=self._evidence(),
                risk_context=self._context(goal),
                signal_strength=Decimal("0.01"),
                strategy_id="strategy",
                model_id="model",
                config_sha256="e" * 64,
            )

        with self.assertRaisesRegex(ValueError, "requires model_id"):
            OpportunityIntent(
                intent_id="bad-hybrid",
                opportunity_class=OpportunityClass.HYBRID,
                evidence=self._evidence(forecast=True),
                risk_context=self._context(goal),
                signal_strength=Decimal("0.01"),
                strategy_id="strategy",
                model_id=None,
                config_sha256="e" * 64,
            )

    def test_positive_action_fails_closed_on_approximate_or_missing_dependency_truth(
        self,
    ) -> None:
        goal = self._goal()
        policy = self._policy(goal)
        intent = self._intent(goal)

        approximate = build_portfolio_plan(
            PaperBook("1000"),
            (intent,),
            policy,
            self.DECISION_TS,
            portfolio_truth=EvidenceTruth.APPROXIMATE,
            dependency_graph_sha256="f" * 64,
        )
        missing_graph = build_portfolio_plan(
            PaperBook("1000"),
            (intent,),
            policy,
            self.DECISION_TS,
            dependency_graph_sha256=None,
        )

        self.assertEqual(approximate.action, PortfolioAction.WAIT)
        self.assertEqual(approximate.stakes, (Decimal("0"),))
        self.assertIn("exact portfolio", approximate.reason)
        self.assertEqual(missing_graph.action, PortfolioAction.WAIT)
        self.assertIn("dependency-graph", missing_graph.reason)

    def test_outcome_independent_positive_requires_complete_exact_terminal_evidence(
        self,
    ) -> None:
        goal = self._goal()
        approximate = self._intent(
            goal,
            opportunity_class=OpportunityClass.ARBITRAGE,
            evidence=self._evidence(truth=EvidenceTruth.APPROXIMATE),
        )
        plan = build_portfolio_plan(
            PaperBook("1000"),
            (approximate,),
            self._policy(goal),
            self.DECISION_TS,
            dependency_graph_sha256="f" * 64,
        )
        self.assertEqual(plan.action, PortfolioAction.WAIT)
        self.assertIn("outcome-independent", plan.reason)

        incomplete = self._intent(
            goal,
            suffix="2",
            opportunity_class=OpportunityClass.ARBITRAGE,
            evidence=self._evidence(
                evidence_id="incomplete",
                outcome_space_complete=False,
            ),
        )
        plan = build_portfolio_plan(
            PaperBook("1000"),
            (incomplete,),
            self._policy(goal),
            self.DECISION_TS,
            dependency_graph_sha256="f" * 64,
        )
        self.assertEqual(plan.action, PortfolioAction.WAIT)
        self.assertIn("terminal-state", plan.reason)

    def test_future_or_infeasible_evidence_cannot_create_positive_action(self) -> None:
        goal = self._goal()
        policy = self._policy(goal)
        future_evidence = replace(
            self._evidence(forecast=True),
            observed_at="2026-09-18T13:20:01+00:00",
            causal_cutoff="2026-09-18T13:20:01+00:00",
        )
        future = self._intent(goal, evidence=future_evidence)
        future_plan = build_portfolio_plan(
            PaperBook("1000"),
            (future,),
            policy,
            self.DECISION_TS,
            dependency_graph_sha256="f" * 64,
        )
        infeasible = self._intent(
            goal,
            suffix="2",
            evidence=self._evidence(
                evidence_id="infeasible",
                forecast=True,
                execution_feasible=False,
            ),
        )
        infeasible_plan = build_portfolio_plan(
            PaperBook("1000"),
            (infeasible,),
            policy,
            self.DECISION_TS,
            dependency_graph_sha256="f" * 64,
        )

        self.assertEqual(future_plan.action, PortfolioAction.WAIT)
        self.assertIn("future", future_plan.reason)
        self.assertEqual(infeasible_plan.action, PortfolioAction.WAIT)
        self.assertIn("not proven feasible", infeasible_plan.reason)

    def test_zero_signal_and_owner_emergency_stop_produce_zero_semantics(self) -> None:
        goal = self._goal()
        zero = build_portfolio_plan(
            PaperBook("1000"),
            (self._intent(goal, signal=Decimal("0")),),
            self._policy(goal),
            self.DECISION_TS,
            dependency_graph_sha256=None,
        )
        self.assertEqual(zero.action, PortfolioAction.ZERO)

        stopped_goal = self._goal(emergency_stop=True)
        stopped = build_portfolio_plan(
            PaperBook("1000"),
            (self._intent(stopped_goal),),
            self._policy(stopped_goal),
            self.DECISION_TS,
            dependency_graph_sha256="f" * 64,
        )
        self.assertEqual(stopped.action, PortfolioAction.ZERO)
        self.assertIn("emergency stop", stopped.reason)

    def test_duplicate_intent_identity_waits_and_plan_roundtrip_is_hash_stable(
        self,
    ) -> None:
        goal = self._goal()
        intent = self._intent(goal)
        policy = self._policy(goal)
        duplicate = build_portfolio_plan(
            PaperBook("1000"),
            (intent, intent),
            policy,
            self.DECISION_TS,
            dependency_graph_sha256="f" * 64,
        )
        self.assertEqual(duplicate.action, PortfolioAction.WAIT)
        self.assertIn("duplicate", duplicate.reason)

        plan = build_portfolio_plan(
            PaperBook("1000"),
            (intent,),
            policy,
            self.DECISION_TS,
            dependency_graph_sha256="f" * 64,
        )
        restored = PortfolioPlan.from_dict(plan.to_dict())
        self.assertEqual(restored, plan)
        self.assertEqual(restored.plan_sha256, plan.plan_sha256)


if __name__ == "__main__":
    unittest.main()

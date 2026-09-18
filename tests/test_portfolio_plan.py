import unittest
from dataclasses import replace
from decimal import Decimal

from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.opportunity import (
    ForecastRef,
    Opportunity,
    OpportunityDecision,
    QuoteRef,
    StrategyClass,
)
from autosport.paper import PaperBook
from autosport.portfolio_plan import (
    EvidenceTruth,
    OpportunityEvidence,
    OpportunityIntent,
    PortfolioAction,
    PortfolioDependencyGraph,
    PortfolioPlan,
    build_portfolio_plan,
)
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext


class PortfolioPlanTests(unittest.TestCase):
    DECISION_TS = "2026-09-18T13:20:00+00:00"
    SNAPSHOT_SHA = "9" * 64

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
        sport: str = "soccer",
    ) -> ProposedTicketRiskContext:
        leg = TicketLeg(
            f"event-{suffix}",
            f"market-{suffix}",
            f"selection-{suffix}",
            Decimal("2"),
            sport=sport,
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
            sport=sport,
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
    def _opportunity(
        cls,
        context: ProposedTicketRiskContext,
        *,
        strategy_class: StrategyClass,
        decision: OpportunityDecision = OpportunityDecision.ACTIONABLE,
    ) -> Opportunity:
        refs = tuple(
            QuoteRef.from_market_event(
                quote,
                market_snapshot_hash=cls.SNAPSHOT_SHA,
            )
            for quote in context.quotes
        )
        forecast_dependent = strategy_class in {
            StrategyClass.PREDICTIVE_EDGE,
            StrategyClass.HYBRID,
        }
        forecasts = (
            tuple(
                ForecastRef(
                    forecast_id=f"forecast-{index}",
                    forecast_hash=("8" * 63) + str(index % 10),
                    quote_key=quote.quote_key,
                    probability=Decimal("0.55"),
                    input_cutoff_ts="2026-09-18T13:19:58+00:00",
                    market_snapshot_hash=quote.market_snapshot_hash or cls.SNAPSHOT_SHA,
                    quote_market_event_hash=quote.market_event_hash,
                )
                for index, quote in enumerate(refs)
            )
            if forecast_dependent
            else ()
        )
        return Opportunity(
            strategy_class=strategy_class,
            decision=decision,
            quotes=refs,
            claims_probability_edge=forecast_dependent,
            forecasts=forecasts,
        )

    @classmethod
    def _intent(
        cls,
        goal: EconomicGoalContract,
        *,
        suffix: str = "1",
        strategy_class: StrategyClass = StrategyClass.PREDICTIVE_EDGE,
        decision: OpportunityDecision = OpportunityDecision.ACTIONABLE,
        signal: Decimal = Decimal("0.05"),
        evidence: OpportunityEvidence | None = None,
        sport: str = "soccer",
    ) -> OpportunityIntent:
        context = cls._context(goal, suffix=suffix, sport=sport)
        requires_complete = strategy_class in {
            StrategyClass.ARBITRAGE,
            StrategyClass.DUTCHING,
            StrategyClass.HEDGE_REBALANCE,
        }
        return OpportunityIntent(
            intent_id=f"intent-{suffix}",
            opportunity=cls._opportunity(
                context,
                strategy_class=strategy_class,
                decision=decision,
            ),
            evidence=evidence
            or cls._evidence(
                evidence_id=f"evidence-{suffix}",
                outcome_space_complete=requires_complete,
            ),
            risk_context=context,
            signal_strength=signal,
            strategy_id=f"strategy-{suffix}",
            model_id=(
                f"model-{suffix}"
                if strategy_class
                in {StrategyClass.PREDICTIVE_EDGE, StrategyClass.HYBRID}
                else None
            ),
            config_sha256="e" * 64,
        )

    @staticmethod
    def _graph(
        book: PaperBook,
        intents: tuple[OpportunityIntent, ...],
        *,
        dependency_edges: tuple[tuple[str, str], ...] = (),
    ) -> PortfolioDependencyGraph:
        return PortfolioDependencyGraph.for_inputs(
            book,
            intents,
            dependency_edges=dependency_edges,
        )

    def test_predictive_intent_uses_canonical_opportunity_and_risk_policy(self) -> None:
        goal = self._goal()
        policy = self._policy(goal)
        book = PaperBook("1000")
        intent = self._intent(goal)
        graph = self._graph(book, (intent,))

        plan = build_portfolio_plan(
            book,
            (intent,),
            policy,
            self.DECISION_TS,
            dependency_graph=graph,
        )

        self.assertEqual(intent.opportunity_class, StrategyClass.PREDICTIVE_EDGE)
        self.assertEqual(plan.action, PortfolioAction.STAKE_VECTOR)
        self.assertEqual(plan.stakes, (Decimal("50.00"),))
        self.assertEqual(book.balance, Decimal("1000"))
        self.assertEqual(book.tickets, {})
        self.assertEqual(plan.risk_policy_sha256, policy.provenance_sha256)
        self.assertEqual(plan.dependency_graph_sha256, graph.graph_sha256)
        self.assertEqual(len(intent.intent_sha256), 64)
        self.assertEqual(len(plan.plan_sha256), 64)

    def test_nonforecast_arbitrage_uses_same_portfolio_risk_path(self) -> None:
        goal = self._goal()
        book = PaperBook("1000")
        intent = self._intent(
            goal,
            strategy_class=StrategyClass.ARBITRAGE,
            signal=Decimal("0.04"),
        )

        plan = build_portfolio_plan(
            book,
            (intent,),
            self._policy(goal),
            self.DECISION_TS,
            dependency_graph=self._graph(book, (intent,)),
        )

        self.assertEqual(plan.action, PortfolioAction.STAKE_VECTOR)
        self.assertEqual(plan.stakes, (Decimal("40.00"),))
        self.assertFalse(intent.opportunity.forecasts)
        self.assertIsNone(intent.model_id)

    def test_hedge_rebalance_is_labeled_without_bypassing_risk_policy(self) -> None:
        goal = self._goal()
        book = PaperBook("1000")
        intent = self._intent(
            goal,
            strategy_class=StrategyClass.HEDGE_REBALANCE,
            signal=Decimal("0.03"),
        )

        plan = build_portfolio_plan(
            book,
            (intent,),
            self._policy(goal),
            self.DECISION_TS,
            dependency_graph=self._graph(book, (intent,)),
        )

        self.assertEqual(plan.action, PortfolioAction.HEDGE_REBALANCE)
        self.assertEqual(plan.stakes, (Decimal("30.00"),))
        self.assertEqual(plan.reason, "endogenous whole-portfolio stake vector derived")

    def test_intent_rejects_quote_or_sport_identity_drift(self) -> None:
        goal = self._goal()
        soccer = self._context(goal, suffix="same", sport="soccer")
        tennis = self._context(goal, suffix="same", sport="tennis")
        opportunity = self._opportunity(
            soccer,
            strategy_class=StrategyClass.ARBITRAGE,
        )

        with self.assertRaisesRegex(ValueError, "exactly match canonical opportunity"):
            OpportunityIntent(
                intent_id="identity-drift",
                opportunity=opportunity,
                evidence=self._evidence(outcome_space_complete=True),
                risk_context=tennis,
                signal_strength=Decimal("0.01"),
                strategy_id="strategy",
                config_sha256="e" * 64,
            )

    def test_predictive_requires_model_identity_but_nonforecast_rejects_it(self) -> None:
        goal = self._goal()
        predictive_context = self._context(goal)
        with self.assertRaisesRegex(ValueError, "requires model_id"):
            OpportunityIntent(
                intent_id="bad-predictive",
                opportunity=self._opportunity(
                    predictive_context,
                    strategy_class=StrategyClass.PREDICTIVE_EDGE,
                ),
                evidence=self._evidence(),
                risk_context=predictive_context,
                signal_strength=Decimal("0.01"),
                strategy_id="strategy",
                model_id=None,
                config_sha256="e" * 64,
            )

        arb_context = self._context(goal, suffix="arb")
        with self.assertRaisesRegex(ValueError, "forecast-dependent"):
            OpportunityIntent(
                intent_id="bad-arb",
                opportunity=self._opportunity(
                    arb_context,
                    strategy_class=StrategyClass.ARBITRAGE,
                ),
                evidence=self._evidence(outcome_space_complete=True),
                risk_context=arb_context,
                signal_strength=Decimal("0.01"),
                strategy_id="strategy",
                model_id="must-not-authorize",
                config_sha256="e" * 64,
            )

    def test_positive_action_fails_closed_on_approximate_or_missing_dependency_truth(self) -> None:
        goal = self._goal()
        policy = self._policy(goal)
        intent = self._intent(goal)
        book = PaperBook("1000")
        graph = self._graph(book, (intent,))

        approximate = build_portfolio_plan(
            book,
            (intent,),
            policy,
            self.DECISION_TS,
            portfolio_truth=EvidenceTruth.APPROXIMATE,
            dependency_graph=graph,
        )
        missing_graph = build_portfolio_plan(
            book,
            (intent,),
            policy,
            self.DECISION_TS,
            dependency_graph=None,
        )

        self.assertEqual(approximate.action, PortfolioAction.WAIT)
        self.assertEqual(approximate.stakes, (Decimal("0"),))
        self.assertIn("exact portfolio", approximate.reason)
        self.assertEqual(missing_graph.action, PortfolioAction.WAIT)
        self.assertIn("dependency-graph", missing_graph.reason)

    def test_dependency_graph_is_bound_to_exact_portfolio_and_candidate_vector(self) -> None:
        goal = self._goal()
        book = PaperBook("1000")
        first = self._intent(goal, suffix="1")
        second = self._intent(goal, suffix="2")
        first_graph = self._graph(book, (first,))

        mismatch = build_portfolio_plan(
            book,
            (second,),
            self._policy(goal),
            self.DECISION_TS,
            dependency_graph=first_graph,
        )
        self.assertEqual(mismatch.action, PortfolioAction.WAIT)
        self.assertIn("does not bind exact portfolio and candidate inputs", mismatch.reason)

        with self.assertRaisesRegex(TypeError, "PortfolioDependencyGraph"):
            build_portfolio_plan(
                book,
                (first,),
                self._policy(goal),
                self.DECISION_TS,
                dependency_graph="f" * 64,  # type: ignore[arg-type]
            )

    def test_outcome_independent_positive_requires_complete_exact_terminal_evidence(self) -> None:
        goal = self._goal()
        approximate = self._intent(
            goal,
            strategy_class=StrategyClass.ARBITRAGE,
            evidence=self._evidence(
                truth=EvidenceTruth.APPROXIMATE,
                outcome_space_complete=True,
            ),
        )
        book = PaperBook("1000")
        plan = build_portfolio_plan(
            book,
            (approximate,),
            self._policy(goal),
            self.DECISION_TS,
            dependency_graph=self._graph(book, (approximate,)),
        )
        self.assertEqual(plan.action, PortfolioAction.WAIT)
        self.assertIn("outcome-independent", plan.reason)

        incomplete = self._intent(
            goal,
            suffix="2",
            strategy_class=StrategyClass.ARBITRAGE,
            evidence=self._evidence(
                evidence_id="incomplete",
                outcome_space_complete=False,
            ),
        )
        book = PaperBook("1000")
        plan = build_portfolio_plan(
            book,
            (incomplete,),
            self._policy(goal),
            self.DECISION_TS,
            dependency_graph=self._graph(book, (incomplete,)),
        )
        self.assertEqual(plan.action, PortfolioAction.WAIT)
        self.assertIn("terminal-state", plan.reason)

    def test_future_infeasible_and_canonical_wait_cannot_create_positive_action(self) -> None:
        goal = self._goal()
        policy = self._policy(goal)
        future_evidence = replace(
            self._evidence(),
            observed_at="2026-09-18T13:20:01+00:00",
            causal_cutoff="2026-09-18T13:20:01+00:00",
        )
        future = self._intent(goal, evidence=future_evidence)
        future_book = PaperBook("1000")
        future_plan = build_portfolio_plan(
            future_book,
            (future,),
            policy,
            self.DECISION_TS,
            dependency_graph=self._graph(future_book, (future,)),
        )
        infeasible = self._intent(
            goal,
            suffix="2",
            evidence=self._evidence(
                evidence_id="infeasible",
                execution_feasible=False,
            ),
        )
        infeasible_book = PaperBook("1000")
        infeasible_plan = build_portfolio_plan(
            infeasible_book,
            (infeasible,),
            policy,
            self.DECISION_TS,
            dependency_graph=self._graph(infeasible_book, (infeasible,)),
        )
        canonical_wait = self._intent(
            goal,
            suffix="3",
            decision=OpportunityDecision.WAIT,
        )
        wait_plan = build_portfolio_plan(
            PaperBook("1000"),
            (canonical_wait,),
            policy,
            self.DECISION_TS,
            dependency_graph=None,
        )

        self.assertEqual(future_plan.action, PortfolioAction.WAIT)
        self.assertIn("future", future_plan.reason)
        self.assertEqual(infeasible_plan.action, PortfolioAction.WAIT)
        self.assertIn("not proven feasible", infeasible_plan.reason)
        self.assertEqual(wait_plan.action, PortfolioAction.WAIT)
        self.assertIn("canonical opportunity decision", wait_plan.reason)

    def test_mixed_candidate_preflight_selectively_zeros_unsafe_candidate(self) -> None:
        goal = self._goal()
        policy = self._policy(goal)
        safe = self._intent(goal, suffix="safe", signal=Decimal("0.05"))
        future = self._intent(
            goal,
            suffix="future",
            signal=Decimal("0.04"),
            evidence=replace(
                self._evidence(evidence_id="future"),
                observed_at="2026-09-18T13:20:01+00:00",
                causal_cutoff="2026-09-18T13:20:01+00:00",
            ),
        )
        intents = (safe, future)
        book = PaperBook("1000")

        plan = build_portfolio_plan(
            book,
            intents,
            policy,
            self.DECISION_TS,
            dependency_graph=self._graph(book, intents),
        )

        self.assertEqual(plan.action, PortfolioAction.STAKE_VECTOR)
        self.assertEqual(plan.stakes, (Decimal("50.00"), Decimal("0")))
        self.assertIn("ineligible intents zeroed", plan.reason)
        self.assertIn("future", plan.reason)

    def test_zero_decision_and_owner_emergency_stop_produce_zero_semantics(self) -> None:
        goal = self._goal()
        canonical_zero = build_portfolio_plan(
            PaperBook("1000"),
            (
                self._intent(
                    goal,
                    decision=OpportunityDecision.ZERO,
                    signal=Decimal("0.05"),
                ),
            ),
            self._policy(goal),
            self.DECISION_TS,
            dependency_graph=None,
        )
        self.assertEqual(canonical_zero.action, PortfolioAction.ZERO)

        stopped_goal = self._goal(emergency_stop=True)
        stopped_intent = self._intent(stopped_goal)
        stopped_book = PaperBook("1000")
        stopped = build_portfolio_plan(
            stopped_book,
            (stopped_intent,),
            self._policy(stopped_goal),
            self.DECISION_TS,
            dependency_graph=self._graph(stopped_book, (stopped_intent,)),
        )
        self.assertEqual(stopped.action, PortfolioAction.ZERO)
        self.assertIn("emergency stop", stopped.reason)

    def test_positive_plan_readback_preserves_authority_and_immutable_identity(self) -> None:
        goal = self._goal()
        intent = self._intent(goal)
        policy = self._policy(goal)
        book = PaperBook("1000")
        plan = build_portfolio_plan(
            book,
            (intent,),
            policy,
            self.DECISION_TS,
            dependency_graph=self._graph(book, (intent,)),
        )
        payload = plan.to_dict()
        restored = PortfolioPlan.from_dict(payload)
        self.assertEqual(restored, plan)
        self.assertEqual(restored.plan_sha256, plan.plan_sha256)

        numeric_stake = dict(payload)
        numeric_stake["stakes"] = [0.1]
        with self.assertRaisesRegex(ValueError, "serialized portfolio plan is invalid"):
            PortfolioPlan.from_dict(numeric_stake)

        missing_graph = dict(payload)
        missing_graph["dependency_graph"] = None
        missing_graph["dependency_graph_sha256"] = None
        with self.assertRaisesRegex(ValueError, "serialized portfolio plan is invalid"):
            PortfolioPlan.from_dict(missing_graph)

        approximate = dict(payload)
        approximate["portfolio_truth"] = EvidenceTruth.APPROXIMATE.value
        with self.assertRaisesRegex(ValueError, "serialized portfolio plan is invalid"):
            PortfolioPlan.from_dict(approximate)

        tampered_reason = dict(payload)
        tampered_reason["reason"] = "tampered durable plan"
        with self.assertRaisesRegex(ValueError, "serialized portfolio plan is invalid"):
            PortfolioPlan.from_dict(tampered_reason)

    def test_duplicate_intent_waits_without_accepting_fake_dependency_hash(self) -> None:
        goal = self._goal()
        intent = self._intent(goal)
        policy = self._policy(goal)
        duplicate = build_portfolio_plan(
            PaperBook("1000"),
            (intent, intent),
            policy,
            self.DECISION_TS,
            dependency_graph=None,
        )
        self.assertEqual(duplicate.action, PortfolioAction.WAIT)
        self.assertIn("duplicate", duplicate.reason)


if __name__ == "__main__":
    unittest.main()

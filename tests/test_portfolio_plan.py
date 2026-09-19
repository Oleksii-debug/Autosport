import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from autosport.decision_ledger import DecisionLedgerIntegrityError, JsonlDecisionLedger
from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.market_outcomes import (
    MarketSettlementOutcomeAuthority,
    assess_betfair_historical_market_definition_authority,
)
from autosport.opportunity import (
    ForecastRef,
    Opportunity,
    PredictiveEligibilityEvidence,
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
    PortfolioDependencyEvidence,
    RobustPortfolioProposal,
    PortfolioPlan,
    PortfolioPlanReconciliationRequired,
    TerminalStateCompletenessEvidence,
    VerifiedTerminalEconomics,
    build_portfolio_plan,
    persist_portfolio_plan_decision,
)
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext
from autosport.scenario_search import ScenarioGroup, ScenarioOutcome


class PortfolioPlanTests(unittest.TestCase):
    DECISION_TS = "2026-09-18T13:20:00+00:00"
    SNAPSHOT_SHA = "9" * 64
    TERMINAL_EXECUTION_CHECKS = (
        ("market_quote_freshness_status", "3" * 64),
        ("paper_risk_policy", "4" * 64),
        ("routing_feasibility", "5" * 64),
        ("settlement_rule_scope", "6" * 64),
    )

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
        odds: Decimal = Decimal("2"),
        event_id: str | None = None,
        market_id: str | None = None,
        selection_id: str | None = None,
        source_id: str | None = None,
    ) -> ProposedTicketRiskContext:
        leg = TicketLeg(
            event_id or f"event-{suffix}",
            market_id or f"market-{suffix}",
            selection_id or f"selection-{suffix}",
            odds,
            sport=sport,
        )
        quote = MarketEvent(
            event_id=leg.event_id,
            market_id=leg.market_id,
            selection_id=leg.selection_id,
            decimal_odds=odds,
            observed_ts="2026-09-18T13:19:59+00:00",
            source_id=source_id or f"provider-{suffix}",
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
        model_id: str | None = None,
        uncertainty: Decimal = Decimal("0"),
        maximum_uncertainty: Decimal = Decimal("0.20"),
        sample_size: int = 100,
        valid_until: str | None = None,
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
        predictive_model_id = model_id or "model-1"
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
                    model_id=predictive_model_id,
                    model_version="1",
                    strategy_version="strategy-v1",
                    uncertainty=uncertainty,
                    predictive_eligibility=PredictiveEligibilityEvidence(
                        evaluation_id=f"evaluation-{index}",
                        evaluation_sha256=("7" * 63) + str(index % 10),
                        protocol_sha256="6" * 64,
                        model_id=predictive_model_id,
                        model_version="1",
                        strategy_version="strategy-v1",
                        uncertainty_kind="absolute_probability_radius_v1",
                        sample_size=sample_size,
                        minimum_sample_size=50,
                        maximum_uncertainty=maximum_uncertainty,
                        as_of="2026-09-18T13:19:00+00:00",
                        valid_until=valid_until or cls.DECISION_TS,
                    ),
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
        odds: Decimal = Decimal("2"),
        event_id: str | None = None,
        market_id: str | None = None,
        selection_id: str | None = None,
        source_id: str | None = None,
    ) -> OpportunityIntent:
        context = cls._context(
            goal,
            suffix=suffix,
            sport=sport,
            odds=odds,
            event_id=event_id,
            market_id=market_id,
            selection_id=selection_id,
            source_id=source_id,
        )
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
                model_id=(
                    f"model-{suffix}"
                    if strategy_class
                    in {StrategyClass.PREDICTIVE_EDGE, StrategyClass.HYBRID}
                    else None
                ),
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

    @classmethod
    def _betfair_authority(
        cls,
        selection_ids: tuple[str, ...] = ("101", "202"),
    ) -> MarketSettlementOutcomeAuthority:
        assessment = assess_betfair_historical_market_definition_authority(
            market_id="1.23456789",
            market_definition={
                "eventId": "event-betfair-1",
                "eventTypeId": "2593174",
                "marketType": "MATCH_ODDS",
                "status": "OPEN",
                "runners": [
                    {"id": selection_id}
                    for selection_id in selection_ids
                ],
            },
            provider_publish_at="2026-09-18T13:19:57+00:00",
            observed_at="2026-09-18T13:19:58+00:00",
        )
        if assessment.authority is None:
            raise AssertionError(
                f"expected verified Betfair authority: {assessment.refusal_reason}"
            )
        return assessment.authority

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

    @classmethod
    def _bind_terminal_state(
        cls,
        intents: tuple[OpportunityIntent, ...],
        groups: tuple[ScenarioGroup, ...],
    ) -> tuple[OpportunityIntent, ...]:
        state_sha256 = TerminalStateCompletenessEvidence.state_space_sha256_for(
            groups
        )
        execution_sha256 = (
            TerminalStateCompletenessEvidence.execution_assumptions_sha256_for(
                verifier_identity="paper-terminal-verifier",
                verification_protocol_sha256="1" * 64,
                reproducibility_bundle_sha256="2" * 64,
                execution_check_sha256s=cls.TERMINAL_EXECUTION_CHECKS,
            )
        )
        return tuple(
            replace(
                intent,
                evidence=replace(
                    intent.evidence,
                    truth=EvidenceTruth.EXACT,
                    outcome_space_complete=True,
                    terminal_state_space_sha256=state_sha256,
                    execution_assumptions_sha256=execution_sha256,
                    execution_feasible=True,
                ),
            )
            for intent in intents
        )

    @classmethod
    def _terminal_witness(
        cls,
        book: PaperBook,
        intents: tuple[OpportunityIntent, ...],
        graph: PortfolioDependencyGraph,
        groups: tuple[ScenarioGroup, ...],
    ) -> TerminalStateCompletenessEvidence:
        portfolio_sha256 = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)
        assert portfolio_sha256 is not None
        return TerminalStateCompletenessEvidence(
            evidence_id="terminal-completeness-1",
            verifier_identity="paper-terminal-verifier",
            verification_protocol_sha256="1" * 64,
            reproducibility_bundle_sha256="2" * 64,
            causal_cutoff="2026-09-18T13:19:58+00:00",
            evaluated_at="2026-09-18T13:19:59+00:00",
            portfolio_sha256=portfolio_sha256,
            dependency_graph_sha256=graph.graph_sha256,
            intent_sha256s=tuple(intent.intent_sha256 for intent in intents),
            candidate_sha256s=tuple(intent.candidate_sha256 for intent in intents),
            scenario_groups=groups,
            execution_check_sha256s=cls.TERMINAL_EXECUTION_CHECKS,
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

    def test_nonforecast_live_price_movement_uses_same_portfolio_risk_path(self) -> None:
        goal = self._goal()
        book = PaperBook("1000")
        intent = self._intent(
            goal,
            strategy_class=StrategyClass.LIVE_PRICE_MOVEMENT,
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

    def test_hash_only_hedge_rebalance_fails_closed_without_terminal_economics(self) -> None:
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

        self.assertEqual(plan.action, PortfolioAction.WAIT)
        self.assertEqual(plan.stakes, (Decimal("0"),))
        self.assertIn("verified terminal-state economics", plan.reason)

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


    def test_typed_dependency_evidence_is_required_to_admit_correlated_predictive_candidates(self) -> None:
        goal = self._goal()
        first = self._intent(goal, suffix="joint-a", signal=Decimal("0.05"))
        second = self._intent(goal, suffix="joint-b", signal=Decimal("0.04"))
        intents = (first, second)
        book = PaperBook("1000")
        graph = self._graph(
            book,
            intents,
            dependency_edges=(tuple(sorted((first.candidate_sha256, second.candidate_sha256))),),
        )
        evidence = self._dependency_evidence(
            book,
            intents,
            dependency=Decimal("0.20"),
            uncertainty=Decimal("0.05"),
            fee=Decimal("0.01"),
            partial_fill=Decimal("0.05"),
        )
        plan = build_portfolio_plan(
            book,
            intents,
            self._policy(goal),
            self.DECISION_TS,
            dependency_graph=graph,
            dependency_evidence=evidence,
        )
        self.assertEqual(plan.action, PortfolioAction.STAKE_VECTOR)
        self.assertEqual(plan.stakes, (
            Decimal("35.74"),
            Decimal("28.59"),
        ))
        self.assertIn("endogenous whole-portfolio stake vector", plan.reason)

    def test_robust_dependency_evidence_rejects_future_or_incomplete_provenance(self) -> None:
        goal = self._goal()
        first = self._intent(goal, suffix="prov-a", signal=Decimal("0.05"))
        second = self._intent(goal, suffix="prov-b", signal=Decimal("0.04"))
        book = PaperBook("1000")
        evidence = self._dependency_evidence(
            book,
            (first, second),
            as_of="2026-09-18T13:20:01+00:00",
            valid_until="2026-09-18T13:21:00+00:00",
        )
        graph = self._graph(
            book,
            (first, second),
            dependency_edges=(tuple(sorted((first.candidate_sha256, second.candidate_sha256))),),
        )
        plan = build_portfolio_plan(
            book,
            (first, second),
            self._policy(goal),
            self.DECISION_TS,
            dependency_graph=graph,
            dependency_evidence=evidence,
        )
        self.assertEqual(plan.action, PortfolioAction.WAIT)
        self.assertIn("stale", plan.reason)

    def test_correlated_positive_candidates_fail_closed_without_joint_risk_authority(self) -> None:
        goal = self._goal()
        first = self._intent(goal, suffix="corr-a", signal=Decimal("0.05"))
        second = self._intent(goal, suffix="corr-b", signal=Decimal("0.04"))
        intents = (first, second)
        book = PaperBook("1000")
        edge = tuple(sorted((first.candidate_sha256, second.candidate_sha256)))
        graph = self._graph(book, intents, dependency_edges=(edge,))

        plan = build_portfolio_plan(
            book,
            intents,
            self._policy(goal),
            self.DECISION_TS,
            dependency_graph=graph,
        )

        self.assertEqual(plan.action, PortfolioAction.WAIT)
        self.assertEqual(plan.stakes, (Decimal("0"), Decimal("0")))
        self.assertIn("complete canonical joint-dependency proof", plan.reason)


    def _dependency_evidence(
        self,
        book: PaperBook,
        intents: tuple[OpportunityIntent, ...],
        *,
        dependency: Decimal = Decimal("0.25"),
        uncertainty: Decimal = Decimal("0.10"),
        fee: Decimal = Decimal("0.01"),
        partial_fill: Decimal = Decimal("0.10"),
        as_of: str = "2026-09-18T13:19:59+00:00",
        valid_until: str = "2026-09-18T13:20:00+00:00",
    ) -> PortfolioDependencyEvidence:
        portfolio_sha = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)
        assert portfolio_sha is not None
        candidates = tuple(intent.candidate_sha256 for intent in intents)
        pairs = tuple(
            (left, right, dependency)
            for index, left in enumerate(candidates)
            for right in candidates[index + 1:]
        )
        return PortfolioDependencyEvidence(
            evidence_id="dependency-evidence-1",
            portfolio_sha256=portfolio_sha,
            intent_sha256s=tuple(intent.intent_sha256 for intent in intents),
            candidate_sha256s=candidates,
            population_id="historical-soccer-v1",
            method="empirical-upper-bound",
            sample_size=100,
            causal_cutoff="2026-09-18T13:19:58+00:00",
            as_of=as_of,
            valid_until=valid_until,
            reproducibility_sha256="f" * 64,
            pairwise_dependency_upper_bounds=pairs,
            uncertainty_fraction=uncertainty,
            fee_fraction=fee,
            partial_fill_stress_fraction=partial_fill,
        )

    def test_dependency_evidence_requires_complete_pair_matrix_and_round_trips(self) -> None:
        goal = self._goal()
        first = self._intent(goal, suffix="dep-a", signal=Decimal("0.05"))
        second = self._intent(goal, suffix="dep-b", signal=Decimal("0.04"))
        book = PaperBook("1000")
        evidence = self._dependency_evidence(book, (first, second))
        self.assertEqual(
            PortfolioDependencyEvidence.from_dict(evidence.to_dict()),
            evidence,
        )
        with self.assertRaisesRegex(ValueError, "cover every candidate pair"):
            PortfolioDependencyEvidence(
                evidence_id=evidence.evidence_id,
                portfolio_sha256=evidence.portfolio_sha256,
                intent_sha256s=evidence.intent_sha256s,
                candidate_sha256s=evidence.candidate_sha256s,
                population_id=evidence.population_id,
                method=evidence.method,
                sample_size=evidence.sample_size,
                causal_cutoff=evidence.causal_cutoff,
                as_of=evidence.as_of,
                valid_until=evidence.valid_until,
                reproducibility_sha256=evidence.reproducibility_sha256,
                pairwise_dependency_upper_bounds=(),
                uncertainty_fraction=evidence.uncertainty_fraction,
                fee_fraction=evidence.fee_fraction,
                partial_fill_stress_fraction=evidence.partial_fill_stress_fraction,
            )

    def test_under_supported_dependency_evidence_fails_closed_to_wait(self) -> None:
        goal = self._goal()
        first = self._intent(goal, suffix="weak-a", signal=Decimal("0.05"))
        second = self._intent(goal, suffix="weak-b", signal=Decimal("0.04"))
        intents = (first, second)
        book = PaperBook("1000")
        graph = self._graph(
            book,
            intents,
            dependency_edges=(
                tuple(sorted((first.candidate_sha256, second.candidate_sha256))),
            ),
        )
        evidence = replace(
            self._dependency_evidence(
                book,
                intents,
                dependency=Decimal("0"),
                uncertainty=Decimal("0"),
                fee=Decimal("0"),
                partial_fill=Decimal("0"),
            ),
            sample_size=2,
        )

        plan = build_portfolio_plan(
            book,
            intents,
            self._policy(goal),
            self.DECISION_TS,
            dependency_graph=graph,
            dependency_evidence=evidence,
        )

        self.assertEqual(plan.action, PortfolioAction.WAIT)
        self.assertEqual(plan.stakes, (Decimal("0"), Decimal("0")))
        self.assertIn("under-supported", plan.reason)
        self.assertFalse(evidence.support_qualified)

    def test_dependency_evidence_stale_or_mismatched_inputs_fail_closed(self) -> None:
        goal = self._goal()
        first = self._intent(goal, suffix="stale-a", signal=Decimal("0.05"))
        second = self._intent(goal, suffix="stale-b", signal=Decimal("0.04"))
        book = PaperBook("1000")
        policy = self._policy(goal)
        graph = self._graph(book, (first, second), dependency_edges=(
            tuple(sorted((first.candidate_sha256, second.candidate_sha256))),
        ))
        future = self._dependency_evidence(
            book,
            (first, second),
            as_of="2026-09-18T13:20:01+00:00",
            valid_until="2026-09-18T13:21:00+00:00",
        )
        future_plan = build_portfolio_plan(
            book,
            (first, second),
            policy,
            self.DECISION_TS,
            dependency_graph=graph,
            dependency_evidence=future,
        )
        self.assertEqual(future_plan.action, PortfolioAction.WAIT)
        self.assertIn("stale", future_plan.reason)

        expired = self._dependency_evidence(
            book,
            (first, second),
            as_of="2026-09-18T13:19:58+00:00",
            valid_until="2026-09-18T13:19:59+00:00",
        )
        expired_plan = build_portfolio_plan(
            book,
            (first, second),
            policy,
            self.DECISION_TS,
            dependency_graph=graph,
            dependency_evidence=expired,
        )
        self.assertEqual(expired_plan.action, PortfolioAction.WAIT)
        self.assertEqual(expired_plan.stakes, (Decimal("0"), Decimal("0")))
        self.assertIn("stale", expired_plan.reason)

        boundary = self._dependency_evidence(
            book,
            (first, second),
            valid_until=self.DECISION_TS,
        )
        boundary_plan = build_portfolio_plan(
            book,
            (first, second),
            policy,
            self.DECISION_TS,
            dependency_graph=graph,
            dependency_evidence=boundary,
        )
        self.assertEqual(boundary_plan.action, PortfolioAction.STAKE_VECTOR)

        other = self._intent(goal, suffix="mismatch")
        mismatch = self._dependency_evidence(book, (first, second))
        mismatch_plan = build_portfolio_plan(
            book,
            (first, other),
            policy,
            self.DECISION_TS,
            dependency_graph=graph,
            dependency_evidence=mismatch,
        )
        self.assertEqual(mismatch_plan.action, PortfolioAction.WAIT)
        self.assertIn("does not bind exact portfolio/candidates", mismatch_plan.reason)
        self.assertIsNone(mismatch_plan.dependency_graph)

    def test_robust_proposal_hash_is_context_free_for_high_precision_decimal(self) -> None:
        exact = Decimal("123456789012345678901234567890.00")
        proposal = RobustPortfolioProposal(
            base_stakes=(exact,),
            proposed_stakes=(exact,),
            dependency_haircut_fraction=Decimal("0.00"),
            uncertainty_fraction=Decimal("0.000"),
            fee_fraction=Decimal("0.0"),
            partial_fill_stress_fraction=Decimal("0"),
            robust_scale=Decimal("1.000"),
        )
        equivalent = RobustPortfolioProposal(
            base_stakes=(Decimal("123456789012345678901234567890"),),
            proposed_stakes=(Decimal("123456789012345678901234567890.0000"),),
            dependency_haircut_fraction=Decimal("0"),
            uncertainty_fraction=Decimal("0"),
            fee_fraction=Decimal("0"),
            partial_fill_stress_fraction=Decimal("0.0000"),
            robust_scale=Decimal("1"),
        )

        self.assertEqual(proposal.proposal_sha256, equivalent.proposal_sha256)
        payload = proposal.to_dict()
        restored = RobustPortfolioProposal.from_dict(payload)
        self.assertEqual(restored, proposal)
        self.assertEqual(restored.base_stakes[0], exact)
        self.assertEqual(restored.proposed_stakes[0], exact)
        self.assertEqual(Decimal(payload["base_stakes"][0]), exact)
        self.assertEqual(Decimal(payload["proposed_stakes"][0]), exact)

    def test_correlated_positive_candidates_use_robust_haircut_and_remain_exact_decimal(self) -> None:
        goal = self._goal()
        first = self._intent(goal, suffix="robust-a", signal=Decimal("0.05"))
        second = self._intent(goal, suffix="robust-b", signal=Decimal("0.04"))
        intents = (first, second)
        book = PaperBook("1000")
        graph = self._graph(
            book,
            intents,
            dependency_edges=(tuple(sorted((first.candidate_sha256, second.candidate_sha256))),),
        )
        evidence = self._dependency_evidence(
            book,
            intents,
            dependency=Decimal("0.25"),
            uncertainty=Decimal("0.10"),
            fee=Decimal("0.01"),
            partial_fill=Decimal("0.10"),
        )
        proposal = RobustPortfolioProposal.derive((Decimal("50.00"), Decimal("40.00")), evidence)
        expected_scale = (
            Decimal("0.75")
            * Decimal("0.90")
            * Decimal("0.99")
            * Decimal("0.90")
        )
        self.assertEqual(proposal.robust_scale, expected_scale)
        self.assertEqual(proposal.proposed_stakes, (
            (Decimal("50") * expected_scale).quantize(Decimal("0.01")),
            (Decimal("40") * expected_scale).quantize(Decimal("0.01")),
        ))
        restored = RobustPortfolioProposal.from_dict(proposal.to_dict())
        self.assertEqual(restored, proposal)

        plan = build_portfolio_plan(
            book,
            intents,
            self._policy(goal),
            self.DECISION_TS,
            dependency_graph=graph,
            dependency_evidence=evidence,
        )
        self.assertEqual(plan.action, PortfolioAction.STAKE_VECTOR)
        self.assertEqual(plan.stakes, proposal.proposed_stakes)
        self.assertTrue(all(stake >= 0 for stake in plan.stakes))
        self.assertTrue(all(isinstance(stake, Decimal) for stake in plan.stakes))
        self.assertEqual(plan.dependency_evidence, evidence)
        self.assertEqual(plan.robust_proposal, proposal)
        self.assertEqual(plan.dependency_evidence_sha256, evidence.evidence_sha256)
        self.assertEqual(plan.robust_proposal_sha256, proposal.proposal_sha256)

        payload = plan.to_dict()
        self.assertEqual(payload["schema_version"], 5)
        self.assertEqual(PortfolioPlan.from_dict(payload), plan)

        tampered_evidence = json.loads(json.dumps(payload))
        dependency_payload = tampered_evidence["dependency_evidence"]
        assert isinstance(dependency_payload, dict)
        dependency_payload["sample_size"] = 99
        with self.assertRaisesRegex(
            ValueError,
            "serialized portfolio plan is invalid",
        ):
            PortfolioPlan.from_dict(tampered_evidence)

        tampered_proposal = json.loads(json.dumps(payload))
        proposal_payload = tampered_proposal["robust_proposal"]
        assert isinstance(proposal_payload, dict)
        proposal_payload["robust_scale"] = "0.5"
        with self.assertRaisesRegex(
            ValueError,
            "serialized portfolio plan is invalid",
        ):
            PortfolioPlan.from_dict(tampered_proposal)

        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "robust-decisions.jsonl"
            first_record = persist_portfolio_plan_decision(
                JsonlDecisionLedger(ledger_path),
                plan,
                intents,
                self._policy(goal),
                initialize_ledger=True,
                replay_run_id="replay-robust-portfolio",
                material_action_id="robust-portfolio-plan",
            )
            restarted = JsonlDecisionLedger(ledger_path)
            retry_record = persist_portfolio_plan_decision(
                restarted,
                plan,
                intents,
                self._policy(goal),
                initialize_ledger=False,
                replay_run_id="replay-robust-portfolio",
                material_action_id="robust-portfolio-plan",
            )
            self.assertEqual(retry_record.decision_id, first_record.decision_id)
            durable = restarted.verified_economic_decision_for_material_action(
                "robust-portfolio-plan",
                goal,
                risk_policy=self._policy(goal),
            )
            self.assertIsNotNone(durable)
            assert durable is not None
            restored_plan = PortfolioPlan.from_dict(
                json.loads(durable.payload["portfolio_plan_json"])
            )
            self.assertEqual(restored_plan, plan)
            self.assertEqual(restored_plan.dependency_evidence, evidence)
            self.assertEqual(restored_plan.robust_proposal, proposal)

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

    def test_complete_hash_only_arbitrage_cannot_create_positive_plan(self) -> None:
        goal = self._goal()
        intent = self._intent(
            goal,
            suffix="hash-only-arb",
            strategy_class=StrategyClass.ARBITRAGE,
            signal=Decimal("0.04"),
        )
        book = PaperBook("1000")

        plan = build_portfolio_plan(
            book,
            (intent,),
            self._policy(goal),
            self.DECISION_TS,
            dependency_graph=self._graph(book, (intent,)),
        )

        self.assertEqual(plan.action, PortfolioAction.WAIT)
        self.assertEqual(plan.stakes, (Decimal("0"),))
        self.assertIn("verified terminal-state economics", plan.reason)

    def test_omitted_dependency_edges_cannot_authorize_joint_positive_vector(self) -> None:
        goal = self._goal()
        first = self._intent(goal, suffix="omitted-a", signal=Decimal("0.05"))
        second = self._intent(goal, suffix="omitted-b", signal=Decimal("0.04"))
        intents = (first, second)
        book = PaperBook("1000")
        graph = self._graph(book, intents)

        self.assertEqual(graph.dependency_edges, ())
        plan = build_portfolio_plan(
            book,
            intents,
            self._policy(goal),
            self.DECISION_TS,
            dependency_graph=graph,
        )

        self.assertEqual(plan.action, PortfolioAction.WAIT)
        self.assertEqual(plan.stakes, (Decimal("0"), Decimal("0")))
        self.assertIn("omitted correlations", plan.reason)

    def test_positive_candidate_with_open_position_fails_closed_without_complete_graph(self) -> None:
        goal = self._goal()
        intent = self._intent(goal, suffix="open-dependent", signal=Decimal("0.05"))
        book = PaperBook("1000")
        book.open_ticket(
            intent.risk_context.legs,
            Decimal("10"),
            reason="existing paper position",
            placed_at="2026-09-18T13:00:00+00:00",
        )
        graph = self._graph(book, (intent,))

        plan = build_portfolio_plan(
            book,
            (intent,),
            self._policy(goal),
            self.DECISION_TS,
            dependency_graph=graph,
        )

        self.assertEqual(plan.action, PortfolioAction.WAIT)
        self.assertEqual(plan.stakes, (Decimal("0"),))
        self.assertIn("existing open positions", plan.reason)

    def test_external_terminal_witness_cannot_authorize_positive_without_market_outcome_authority(self) -> None:
        goal = self._goal()
        base_intents = (
            self._intent(
                goal,
                suffix="arb-a",
                strategy_class=StrategyClass.ARBITRAGE,
                signal=Decimal("0.05"),
                odds=Decimal("3"),
            ),
            self._intent(
                goal,
                suffix="arb-b",
                strategy_class=StrategyClass.ARBITRAGE,
                signal=Decimal("0.05"),
                odds=Decimal("3"),
            ),
        )
        # This group contains every proposed ticket leg, but that fact cannot prove
        # that the real market lacks an additional non-ticket terminal outcome
        # (for example DRAW in a three-way market).  Until canonical market metadata
        # supplies an exhaustive outcome roster, an external ScenarioGroup must not
        # grant positive arbitrage/dutching/hedge authority.
        outcomes = tuple(
            ScenarioOutcome(quote_key=key)
            for key in sorted(
                intent.risk_context.legs[0].quote_key
                for intent in base_intents
            )
        )
        groups = (ScenarioGroup("externally-claimed-complete-market", outcomes),)
        intents = self._bind_terminal_state(base_intents, groups)
        book = PaperBook("1000")
        edge = tuple(
            sorted((intents[0].candidate_sha256, intents[1].candidate_sha256))
        )
        graph = self._graph(book, intents, dependency_edges=(edge,))
        witness = self._terminal_witness(book, intents, graph, groups)

        # Serializer symmetry is part of the restart/provenance boundary even while
        # this witness is deliberately insufficient for positive authority.
        self.assertEqual(
            TerminalStateCompletenessEvidence.from_dict(witness.to_dict()),
            witness,
        )

        plan = build_portfolio_plan(
            book,
            intents,
            self._policy(goal),
            self.DECISION_TS,
            dependency_graph=graph,
            terminal_state_evidence=witness,
        )

        self.assertEqual(plan.action, PortfolioAction.WAIT)
        self.assertEqual(plan.stakes, (Decimal("0"), Decimal("0")))
        self.assertIsNone(plan.terminal_economics)
        self.assertIn("authoritative exhaustive market-outcome semantics", plan.reason)

        # Preserve direct restart/serialization coverage for the typed terminal proof
        # even though external completeness evidence is not positive-action authority.
        proof = VerifiedTerminalEconomics(
            completeness_evidence=witness,
            report_mode="exact-enumeration",
            total_states=len(outcomes),
            worst_terminal_profit=Decimal("1"),
            best_terminal_profit=Decimal("2"),
            worst_proven=True,
            best_proven=True,
        )
        self.assertEqual(VerifiedTerminalEconomics.from_dict(proof.to_dict()), proof)

        # A self-consistent external terminal proof remains diagnostic only.  It must
        # not recover positive outcome-independent authority through direct object
        # construction or through a hash-consistent schema-v4 durable payload when the
        # canonical builder itself must fail closed for missing exhaustive market truth.
        policy = self._policy(goal)
        portfolio_sha256 = PaperRiskPolicy.risk_of_ruin_portfolio_sha256(book)
        self.assertIsNotNone(portfolio_sha256)
        assert portfolio_sha256 is not None
        unsafe_fields = {
            "decision_ts": self.DECISION_TS,
            "action": PortfolioAction.PAPER_PLAN,
            "stakes": (Decimal("50.00"), Decimal("50.00")),
            "intent_ids": tuple(intent.intent_id for intent in intents),
            "intent_sha256s": tuple(intent.intent_sha256 for intent in intents),
            "opportunity_classes": tuple(
                intent.opportunity_class.value for intent in intents
            ),
            "portfolio_sha256": portfolio_sha256,
            "dependency_graph": graph,
            "terminal_economics": proof,
            "economic_goal_contract_sha256": "a" * 64,
            "risk_policy_sha256": policy.provenance_sha256,
            "portfolio_truth": EvidenceTruth.EXACT,
            "reason": "externally asserted positive outcome-independent proof",
            "dependency_evidence": None,
            "robust_proposal": None,
        }
        with self.assertRaisesRegex(
            ValueError,
            "authoritative .*exhaustive market-outcome semantics",
        ):
            PortfolioPlan(**unsafe_fields)

        # Build a deliberately bypassed instance only to synthesize the exact
        # hash-consistent durable payload an attacker/corrupted restart could present.
        # from_dict() must reconstruct through the real invariants and reject it.
        unsafe_plan = object.__new__(PortfolioPlan)
        for field_name, field_value in unsafe_fields.items():
            object.__setattr__(unsafe_plan, field_name, field_value)
        unsafe_payload = unsafe_plan.to_dict()
        with self.assertRaisesRegex(
            ValueError,
            "serialized portfolio plan is invalid",
        ):
            PortfolioPlan.from_dict(unsafe_payload)

        # Typed control evidence still has to bind the OpportunityEvidence identity;
        # fail-closed market exhaustiveness must not mask a weaker control-drift check.
        tampered_checks = tuple(
            (
                name,
                ("7" * 64 if name == "routing_feasibility" else digest),
            )
            for name, digest in witness.execution_check_sha256s
        )
        tampered_witness = replace(
            witness,
            execution_check_sha256s=tampered_checks,
        )
        tampered = build_portfolio_plan(
            book,
            intents,
            self._policy(goal),
            self.DECISION_TS,
            dependency_graph=graph,
            terminal_state_evidence=tampered_witness,
        )
        self.assertEqual(tampered.action, PortfolioAction.WAIT)
        self.assertIn(
            "execution assumptions do not match verified completeness",
            tampered.reason,
        )

    def test_authoritative_betfair_conservative_cover_cannot_authorize_arbitrage(self) -> None:
        goal = self._goal()
        authority = self._betfair_authority()
        base_intents = tuple(
            self._intent(
                goal,
                suffix=f"betfair-arb-{selection_id}",
                strategy_class=StrategyClass.ARBITRAGE,
                signal=Decimal("0.05"),
                odds=Decimal("3"),
                sport="table_tennis",
                event_id="event-betfair-1",
                market_id="1.23456789",
                selection_id=selection_id,
                source_id="betfair_exchange_historical",
            )
            for selection_id in ("101", "202")
        )
        groups = (
            ScenarioGroup(
                "betfair-control-witness",
                tuple(
                    ScenarioOutcome(
                        quote_key=intent.risk_context.legs[0].quote_key
                    )
                    for intent in base_intents
                ),
            ),
        )
        intents = self._bind_terminal_state(base_intents, groups)
        book = PaperBook("1000")
        edge = tuple(
            sorted((intents[0].candidate_sha256, intents[1].candidate_sha256))
        )
        graph = self._graph(book, intents, dependency_edges=(edge,))
        witness = self._terminal_witness(book, intents, graph, groups)

        plan = build_portfolio_plan(
            book,
            intents,
            self._policy(goal),
            self.DECISION_TS,
            dependency_graph=graph,
            terminal_state_evidence=witness,
            market_outcome_authorities=(authority,),
        )

        self.assertEqual(plan.action, PortfolioAction.WAIT)
        self.assertEqual(plan.stakes, (Decimal("0"), Decimal("0")))
        self.assertIsNone(plan.terminal_economics)
        self.assertFalse(authority.terminal_space_exact)
        self.assertIn("exhaustive but not exact", plan.reason)

    def test_authoritative_terminal_proof_requires_reverified_authority_on_readback(self) -> None:
        goal = self._goal()
        authority = self._betfair_authority()
        base_intents = tuple(
            self._intent(
                goal,
                suffix=f"betfair-predictive-{selection_id}",
                strategy_class=StrategyClass.PREDICTIVE_EDGE,
                signal=Decimal("0.03"),
                odds=Decimal("3"),
                sport="table_tennis",
                event_id="event-betfair-1",
                market_id="1.23456789",
                selection_id=selection_id,
                source_id="betfair_exchange_historical",
            )
            for selection_id in ("101", "202")
        )
        groups = (
            ScenarioGroup(
                "betfair-control-witness-predictive",
                tuple(
                    ScenarioOutcome(
                        quote_key=intent.risk_context.legs[0].quote_key
                    )
                    for intent in base_intents
                ),
            ),
        )
        intents = self._bind_terminal_state(base_intents, groups)
        book = PaperBook("1000")
        edge = tuple(
            sorted((intents[0].candidate_sha256, intents[1].candidate_sha256))
        )
        graph = self._graph(book, intents, dependency_edges=(edge,))
        witness = self._terminal_witness(book, intents, graph, groups)

        plan = build_portfolio_plan(
            book,
            intents,
            self._policy(goal),
            self.DECISION_TS,
            dependency_graph=graph,
            terminal_state_evidence=witness,
            market_outcome_authorities=(authority,),
        )

        self.assertEqual(plan.action, PortfolioAction.STAKE_VECTOR)
        self.assertIsNotNone(plan.terminal_economics)
        assert plan.terminal_economics is not None
        self.assertTrue(plan.terminal_economics.outcome_space_exhaustive)
        self.assertFalse(plan.terminal_economics.outcome_space_exact)
        self.assertEqual(
            plan.terminal_economics.outcome_authority_sha256s,
            (authority.authority_sha256,),
        )

        payload = plan.to_dict()
        with self.assertRaisesRegex(
            ValueError,
            "serialized portfolio plan is invalid",
        ):
            PortfolioPlan.from_dict(payload)
        self.assertEqual(
            PortfolioPlan.from_dict(
                payload,
                verified_outcome_authorities=(authority,),
            ),
            plan,
        )

        tampered = json.loads(json.dumps(payload))
        terminal = tampered["terminal_economics"]
        assert isinstance(terminal, dict)
        terminal["outcome_space_exact"] = True
        with self.assertRaisesRegex(
            ValueError,
            "serialized portfolio plan is invalid",
        ):
            PortfolioPlan.from_dict(
                tampered,
                verified_outcome_authorities=(authority,),
            )

    def test_verified_terminal_model_with_nonpositive_minimum_fails_closed(self) -> None:
        goal = self._goal()
        base_intents = (
            self._intent(
                goal,
                suffix="zero-min-a",
                strategy_class=StrategyClass.ARBITRAGE,
                signal=Decimal("0.05"),
                odds=Decimal("2"),
            ),
            self._intent(
                goal,
                suffix="zero-min-b",
                strategy_class=StrategyClass.ARBITRAGE,
                signal=Decimal("0.05"),
                odds=Decimal("2"),
            ),
        )
        groups = (
            ScenarioGroup(
                "complete-zero-min-market",
                tuple(
                    ScenarioOutcome(quote_key=key)
                    for key in sorted(
                        intent.risk_context.legs[0].quote_key
                        for intent in base_intents
                    )
                ),
            ),
        )
        intents = self._bind_terminal_state(base_intents, groups)
        book = PaperBook("1000")
        edge = tuple(
            sorted((intents[0].candidate_sha256, intents[1].candidate_sha256))
        )
        graph = self._graph(book, intents, dependency_edges=(edge,))
        witness = self._terminal_witness(book, intents, graph, groups)

        plan = build_portfolio_plan(
            book,
            intents,
            self._policy(goal),
            self.DECISION_TS,
            dependency_graph=graph,
            terminal_state_evidence=witness,
        )

        self.assertEqual(plan.action, PortfolioAction.WAIT)
        self.assertEqual(plan.stakes, (Decimal("0"), Decimal("0")))
        self.assertIn("minimum terminal net P&L is not positive", plan.reason)

    def test_typed_terminal_witness_cannot_hide_missing_candidate_outcome(self) -> None:
        goal = self._goal()
        base_intents = (
            self._intent(
                goal,
                suffix="missing-a",
                strategy_class=StrategyClass.ARBITRAGE,
                signal=Decimal("0.05"),
                odds=Decimal("3"),
            ),
            self._intent(
                goal,
                suffix="missing-b",
                strategy_class=StrategyClass.ARBITRAGE,
                signal=Decimal("0.05"),
                odds=Decimal("3"),
            ),
        )
        first_key = base_intents[0].risk_context.legs[0].quote_key
        groups = (
            ScenarioGroup(
                "incomplete-terminal-market",
                tuple(
                    ScenarioOutcome(quote_key=key)
                    for key in sorted((first_key, "terminal-other"))
                ),
            ),
        )
        intents = self._bind_terminal_state(base_intents, groups)
        book = PaperBook("1000")
        edge = tuple(
            sorted((intents[0].candidate_sha256, intents[1].candidate_sha256))
        )
        graph = self._graph(book, intents, dependency_edges=(edge,))
        witness = self._terminal_witness(book, intents, graph, groups)

        plan = build_portfolio_plan(
            book,
            intents,
            self._policy(goal),
            self.DECISION_TS,
            dependency_graph=graph,
            terminal_state_evidence=witness,
        )

        self.assertEqual(plan.action, PortfolioAction.WAIT)
        self.assertEqual(plan.stakes, (Decimal("0"), Decimal("0")))
        self.assertIn("ticket leg missing from scenario space", plan.reason)

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

        decimal_tenth = replace(plan, stakes=(Decimal("0.1"),))
        decimal_tenth_payload = decimal_tenth.to_dict()
        self.assertEqual(decimal_tenth_payload["stakes"], ["0.1"])
        self.assertEqual(
            PortfolioPlan.from_dict(decimal_tenth_payload),
            decimal_tenth,
        )
        numeric_stake = dict(decimal_tenth_payload)
        numeric_stake["stakes"] = [0.1]
        with self.assertRaisesRegex(ValueError, "serialized portfolio plan is invalid"):
            PortfolioPlan.from_dict(numeric_stake)

        missing_portfolio = dict(payload)
        missing_portfolio["portfolio_sha256"] = None
        with self.assertRaisesRegex(ValueError, "serialized portfolio plan is invalid"):
            PortfolioPlan.from_dict(missing_portfolio)

        missing_goal = dict(payload)
        missing_goal["economic_goal_contract_sha256"] = None
        with self.assertRaisesRegex(ValueError, "serialized portfolio plan is invalid"):
            PortfolioPlan.from_dict(missing_goal)

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


    def test_predictive_and_nonforecast_plans_survive_restart_idempotently(self) -> None:
        goal = self._goal()
        policy = self._policy(goal)
        cases = (
            ("predictive", StrategyClass.PREDICTIVE_EDGE),
            ("live-price", StrategyClass.LIVE_PRICE_MOVEMENT),
        )

        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "decisions.jsonl"

            for index, (label, strategy_class) in enumerate(cases, start=1):
                intent = self._intent(
                    goal,
                    suffix=f"durable-{index}",
                    strategy_class=strategy_class,
                    signal=Decimal("0.05"),
                )
                book = PaperBook("1000")
                plan = build_portfolio_plan(
                    book,
                    (intent,),
                    policy,
                    self.DECISION_TS,
                    dependency_graph=self._graph(book, (intent,)),
                )
                self.assertEqual(plan.action, PortfolioAction.STAKE_VECTOR)
                material_action_id = f"portfolio-plan-{label}"
                first = persist_portfolio_plan_decision(
                    JsonlDecisionLedger(ledger_path),
                    plan,
                    (intent,),
                    policy,
                    initialize_ledger=(index == 1),
                    replay_run_id="replay-portfolio-plan",
                    material_action_id=material_action_id,
                )

                restarted = JsonlDecisionLedger(ledger_path)
                retry = persist_portfolio_plan_decision(
                    restarted,
                    plan,
                    (intent,),
                    policy,
                    initialize_ledger=False,
                    replay_run_id="replay-portfolio-plan",
                    material_action_id=material_action_id,
                )
                self.assertEqual(retry.decision_id, first.decision_id)

                durable = restarted.verified_economic_decision_for_material_action(
                    material_action_id,
                    goal,
                    risk_policy=policy,
                )
                self.assertIsNotNone(durable)
                assert durable is not None
                self.assertEqual(
                    PortfolioPlan.from_dict(
                        json.loads(durable.payload["portfolio_plan_json"])
                    ),
                    plan,
                )
                intent_evidence = json.loads(
                    durable.payload["portfolio_intent_evidence_json"]
                )
                restored_intent = intent_evidence["intents"][0]
                self.assertEqual(restored_intent["intent_sha256"], intent.intent_sha256)
                self.assertEqual(restored_intent["evidence"], intent.evidence.to_dict())
                self.assertEqual(
                    restored_intent["opportunity"],
                    intent.opportunity.to_dict(),
                )
                self.assertEqual(restored_intent["strategy_id"], intent.strategy_id)
                self.assertEqual(restored_intent["model_id"], intent.model_id)

            self.assertEqual(
                len(JsonlDecisionLedger(ledger_path).verified_records()),
                2,
            )

    def test_durable_plan_missing_ledger_after_restart_fails_closed(self) -> None:
        goal = self._goal()
        policy = self._policy(goal)
        intent = self._intent(goal, suffix="durable-loss")
        book = PaperBook("1000")
        plan = build_portfolio_plan(
            book,
            (intent,),
            policy,
            self.DECISION_TS,
            dependency_graph=self._graph(book, (intent,)),
        )

        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "decisions.jsonl"
            persist_portfolio_plan_decision(
                JsonlDecisionLedger(ledger_path),
                plan,
                (intent,),
                policy,
                initialize_ledger=True,
                replay_run_id="replay-portfolio-plan-loss",
                material_action_id="portfolio-plan-loss",
            )
            self.assertTrue(ledger_path.exists())

            restarted = JsonlDecisionLedger(ledger_path)
            ledger_path.unlink()

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "missing or unreadable",
            ):
                persist_portfolio_plan_decision(
                    restarted,
                    plan,
                    (intent,),
                    policy,
                    initialize_ledger=False,
                    replay_run_id="replay-portfolio-plan-loss",
                    material_action_id="portfolio-plan-loss",
                )
            self.assertFalse(ledger_path.exists())

    def test_durable_plan_conflicting_retry_fails_before_duplicate_append(self) -> None:
        goal = self._goal()
        policy = self._policy(goal)

        with tempfile.TemporaryDirectory() as tmp:
            ledger_path = Path(tmp) / "decisions.jsonl"
            first_intent = self._intent(goal, suffix="durable-conflict-a")
            first_book = PaperBook("1000")
            first_plan = build_portfolio_plan(
                first_book,
                (first_intent,),
                policy,
                self.DECISION_TS,
                dependency_graph=self._graph(first_book, (first_intent,)),
            )
            persist_portfolio_plan_decision(
                JsonlDecisionLedger(ledger_path),
                first_plan,
                (first_intent,),
                policy,
                initialize_ledger=True,
                replay_run_id="replay-portfolio-plan-conflict",
                material_action_id="portfolio-plan-conflict",
            )
            self.assertEqual(
                len(JsonlDecisionLedger(ledger_path).verified_records()),
                1,
            )

            second_intent = self._intent(goal, suffix="durable-conflict-b")
            second_book = PaperBook("1000")
            second_plan = build_portfolio_plan(
                second_book,
                (second_intent,),
                policy,
                self.DECISION_TS,
                dependency_graph=self._graph(second_book, (second_intent,)),
            )
            with self.assertRaisesRegex(
                PortfolioPlanReconciliationRequired,
                "conflicts with current decision intent",
            ):
                persist_portfolio_plan_decision(
                    JsonlDecisionLedger(ledger_path),
                    second_plan,
                    (second_intent,),
                    policy,
                    initialize_ledger=False,
                    replay_run_id="replay-portfolio-plan-conflict",
                    material_action_id="portfolio-plan-conflict",
                )

            self.assertEqual(
                len(JsonlDecisionLedger(ledger_path).verified_records()),
                1,
            )

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

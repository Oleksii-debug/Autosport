from __future__ import annotations

import importlib.util
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from autosport.agents import AgentContext
import autosport.paper_strategy as paper_strategy_module
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.domain import MarketEvent
from autosport.economic_goal import EconomicGoalContract
from autosport.forecasting import ForecastRecord
from autosport.opportunity import (
    ForecastRef,
    PredictiveEligibilityEvidence,
    QuoteRef,
)
from autosport.paper import PaperBook
from autosport.paper_execution_adoption import PaperExecutionAdoptionRuntime
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperExecutionLedger,
    PaperExecutionModelConfig,
)
from autosport.paper_strategy import (
    Forecast,
    PaperDecisionReconciliationRequired,
    PaperValueAgent,
)
from autosport.predictive_authority import resolve_authoritative_forecast_ref
from autosport.risk import PaperRiskPolicy
from autosport.uncertainty_sizing import (
    SizingAction,
    UncertaintySizingEvidence,
    UncertaintySizingPolicy,
    UncertaintySizingRequest,
    evaluate_uncertainty_sizing,
)


_PREDICTIVE_HELPER_PATH = Path(__file__).with_name("test_predictive_authority.py")
_PREDICTIVE_SPEC = importlib.util.spec_from_file_location(
    "_autosport_predictive_authority_helpers_for_paper_value",
    _PREDICTIVE_HELPER_PATH,
)
if _PREDICTIVE_SPEC is None or _PREDICTIVE_SPEC.loader is None:
    raise RuntimeError("cannot load predictive-authority test helpers")
_PREDICTIVE_HELPERS = importlib.util.module_from_spec(_PREDICTIVE_SPEC)
_PREDICTIVE_SPEC.loader.exec_module(_PREDICTIVE_HELPERS)


class PaperValueUncertaintyAbstentionTests(unittest.TestCase):
    @staticmethod
    def _goal() -> EconomicGoalContract:
        return EconomicGoalContract(
            goal_id="goal-paper-uncertainty",
            revision=1,
            bankroll_id="paper-bankroll",
            currency="USD",
            max_stake_fraction=Decimal("0.10"),
            max_capital_at_risk_fraction=Decimal("0.50"),
            max_risk_of_ruin=Decimal("1"),
            max_concurrent_positions=2,
            max_quote_age_seconds=Decimal("5"),
            minimum_data_quality=Decimal("0"),
        )

    @staticmethod
    def _event(
        *,
        decimal_odds: Decimal = Decimal("2"),
    ) -> MarketEvent:
        return MarketEvent(
            event_id="event-uncertainty",
            market_id="market-uncertainty",
            selection_id="selection-uncertainty",
            decimal_odds=decimal_odds,
            observed_ts="2026-09-17T15:00:00+00:00",
            source_id="provider-1",
            sequence=1,
            source_ts="2026-09-17T14:59:59+00:00",
            ingest_ts="2026-09-17T15:00:00+00:00",
        )

    @staticmethod
    def _forecast(
        event: MarketEvent,
        *,
        uncertainty: Decimal,
    ) -> ForecastRecord:
        return ForecastRecord(
            quote_key=event.quote_key,
            probability=Decimal("0.60"),
            model_id="model-uncertainty",
            model_version="model-uncertainty-v1",
            strategy_version="strategy-uncertainty-v1",
            model_training_cutoff_ts="2026-09-17T14:00:00+00:00",
            input_cutoff_ts="2026-09-17T14:59:58+00:00",
            generated_at="2026-09-17T14:59:58+00:00",
            uncertainty=uncertainty,
            market_snapshot_hash="a" * 64,
        )

    @staticmethod
    def _legacy_forecast(event: MarketEvent) -> Forecast:
        return Forecast(
            quote_key=event.quote_key,
            probability=Decimal("0.60"),
            model_id="legacy-paper-control",
            as_of_ts="2026-09-17T14:59:58+00:00",
        )

    @staticmethod
    def _execution_config() -> PaperExecutionModelConfig:
        return PaperExecutionModelConfig(
            model_id="paper-uncertainty-execution-fixture",
            model_version="1",
            evidence_grade=EvidenceGrade.SYNTHETIC,
            evidence_source="test-paper-uncertainty-composition",
            seed="fixed-seed",
            max_quote_age_ms=5_000,
            min_delay_ms=100,
            max_delay_ms=100,
            rejected_bps=0,
            partial_bps=0,
            unknown_bps=0,
            partial_fill_bps=5_000,
            max_slippage_bps=0,
        )

    @classmethod
    def _context(
        cls,
        root: Path,
        event: MarketEvent,
    ) -> tuple[AgentContext, Path]:
        book = PaperBook("100")
        ledger_path = root / "decisions.jsonl"
        runtime = PaperExecutionAdoptionRuntime(
            book=book,
            ledger=PaperExecutionLedger(root / "paper-execution.jsonl"),
            config=cls._execution_config(),
            max_quote_age=timedelta(seconds=5),
            paper_book_path=root / "paper-execution-book.json",
        )
        context = AgentContext(
            book,
            latest_quotes={event.quote_key: event},
            replay_run_id="run-uncertainty",
            decision_ledger=JsonlDecisionLedger(ledger_path),
            paper_execution=runtime,
            paper_provider_accounts=(("provider-1", "paper-account-1"),),
        )
        return context, ledger_path

    @staticmethod
    def _agent(
        goal: EconomicGoalContract,
        forecast: Forecast | ForecastRecord,
        *,
        predictive_ref: ForecastRef | None = None,
        sizing_evidence: UncertaintySizingEvidence | None = None,
        sizing_policy: UncertaintySizingPolicy | None = None,
    ) -> PaperValueAgent:
        refs = (
            None
            if predictive_ref is None
            else {forecast.quote_key: predictive_ref}
        )
        sizing = (
            None
            if sizing_evidence is None
            else {forecast.quote_key: sizing_evidence}
        )
        return PaperValueAgent(
            {forecast.quote_key: forecast},
            stake=Decimal("1"),
            risk_policy=PaperRiskPolicy(economic_goal=goal),
            predictive_forecast_refs=refs,
            uncertainty_sizing_evidence=sizing,
            uncertainty_sizing_policy=sizing_policy,
        )

    @staticmethod
    def _self_attested_ref(
        event: MarketEvent,
        forecast: ForecastRecord,
    ) -> ForecastRef:
        evidence = PredictiveEligibilityEvidence(
            evaluation_id="caller-evaluation",
            evaluation_sha256="1" * 64,
            protocol_sha256="2" * 64,
            admission_policy_sha256="3" * 64,
            model_id=forecast.model_id,
            model_version=forecast.model_version,
            strategy_version=forecast.strategy_version,
            uncertainty_kind="absolute_probability_radius_v1",
            sample_size=500,
            minimum_sample_size=3,
            maximum_uncertainty=Decimal("1"),
            as_of=event.observed_ts,
            valid_until=event.observed_ts,
        )
        quote = QuoteRef.from_market_event(
            event,
            market_snapshot_hash=forecast.market_snapshot_hash,
        )
        return ForecastRef.from_forecast(
            forecast,
            quote,
            predictive_eligibility=evidence,
        )

    @staticmethod
    def _resolver_authorized_ref(
        root: Path,
        event: MarketEvent,
        forecast: ForecastRecord,
    ) -> ForecastRef:
        policy = _PREDICTIVE_HELPERS._policy()
        qualification = _PREDICTIVE_HELPERS._qualification()
        registry, _ = _PREDICTIVE_HELPERS._promoted_registry(
            root,
            qualification,
            policy,
        )
        quote = QuoteRef.from_market_event(
            event,
            market_snapshot_hash=forecast.market_snapshot_hash,
        )
        return resolve_authoritative_forecast_ref(
            registry,
            forecast,
            quote,
            decision_time=event.observed_ts,
            policy=policy,
            qualification=qualification,
        )

    @staticmethod
    def _sizing_evidence(
        event: MarketEvent,
        forecast: ForecastRecord,
        *,
        net_win_profit_per_stake: Decimal | None = None,
    ) -> UncertaintySizingEvidence:
        if forecast.market_snapshot_hash is None or forecast.uncertainty is None:
            raise AssertionError("test forecast must bind snapshot + uncertainty")
        quote = QuoteRef.from_market_event(
            event,
            market_snapshot_hash=forecast.market_snapshot_hash,
        )
        lower = max(
            Decimal("0"),
            forecast.probability - forecast.uncertainty,
        )
        upper = min(
            Decimal("1"),
            forecast.probability + forecast.uncertainty,
        )
        payoff = (
            event.decimal_odds - Decimal("1")
            if net_win_profit_per_stake is None
            else net_win_profit_per_stake
        )
        valid_until = (
            datetime.fromisoformat(event.observed_ts) + timedelta(minutes=5)
        ).isoformat()
        return UncertaintySizingEvidence(
            evidence_id="paper-sizing-" + forecast.forecast_id,
            candidate_id=forecast.forecast_id,
            quote_sha256=quote.market_event_hash,
            probability_model_version_id=forecast.model_version,
            calibration_bundle_sha256="c" * 64,
            causal_cutoff=forecast.input_cutoff_ts,
            produced_at=forecast.generated_at,
            valid_until=valid_until,
            probability_lower=lower,
            probability_point=forecast.probability,
            probability_upper=upper,
            net_win_profit_per_stake=payoff,
            evidence_refs=(
                "evidence://forecast/" + forecast.forecast_id,
                "evidence://quote/" + quote.market_event_hash,
            ),
        )

    @staticmethod
    def _sizing_policy(
        *,
        policy_id: str = "paper-value-canonical-sizing-v1",
        max_uncertainty_width: Decimal = Decimal("0.20"),
        max_bankroll_fraction: Decimal = Decimal("0.10"),
        fractional_kelly: Decimal = Decimal("1"),
    ) -> UncertaintySizingPolicy:
        return UncertaintySizingPolicy(
            policy_id=policy_id,
            fractional_kelly=fractional_kelly,
            max_bankroll_fraction=max_bankroll_fraction,
            max_uncertainty_width=max_uncertainty_width,
            min_conservative_ev_per_stake=Decimal("0"),
        )

    @staticmethod
    def _sizing_decision(
        goal: EconomicGoalContract,
        event: MarketEvent,
        forecast: ForecastRecord,
        evidence: UncertaintySizingEvidence,
        policy: UncertaintySizingPolicy,
    ):
        quote = QuoteRef.from_market_event(
            event,
            market_snapshot_hash=forecast.market_snapshot_hash,
        )
        return evaluate_uncertainty_sizing(
            evidence,
            UncertaintySizingRequest(
                candidate_id=forecast.forecast_id,
                quote_sha256=quote.market_event_hash,
                decision_ts=event.observed_ts,
                bankroll_id=goal.bankroll_id,
                currency=goal.currency,
                bankroll=Decimal("100"),
            ),
            policy,
        )

    def test_goal_active_paper_value_abstains_when_uncertainty_erases_robust_edge(
        self,
    ) -> None:
        goal = self._goal()
        event = self._event()

        # Control case: prove this exact fixture reaches the canonical #623 PAPER
        # execution path. This legacy compatibility forecast is intentionally used
        # only as reachability control; modern ForecastRecord has a stricter
        # predictive-authority boundary below.
        with tempfile.TemporaryDirectory() as tmp:
            control_root = Path(tmp)
            control_context, control_ledger_path = self._context(
                control_root,
                event,
            )

            self._agent(
                goal,
                self._legacy_forecast(event),
            ).on_market_event(event, control_context)

            self.assertEqual(len(control_context.paper_book.tickets), 1)
            self.assertTrue(control_ledger_path.exists())
            self.assertEqual(
                len(control_context.decision_ledger.verified_records()),
                1,
            )

        forecast = self._forecast(
            event,
            uncertainty=Decimal("1"),
        )

        # Point EV is +0.20 at decimal odds 2.0, but the canonical predictive
        # uncertainty semantics are an absolute probability radius. The resulting
        # lower endpoint is zero, so robust BACK edge is not positive.
        self.assertGreater(
            forecast.probability * event.decimal_odds - Decimal("1"),
            0,
        )
        robust_probability_lower = max(
            Decimal("0"),
            forecast.probability - forecast.uncertainty,
        )
        self.assertLessEqual(
            robust_probability_lower * event.decimal_odds - Decimal("1"),
            0,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context, ledger_path = self._context(root, event)

            self._agent(goal, forecast).on_market_event(event, context)

            self.assertEqual(context.paper_book.tickets, {})
            self.assertFalse(
                ledger_path.exists(),
                "abstention must not persist a positive material decision",
            )
            self.assertTrue(
                any(
                    "canonical predictive ForecastRef authority" in note
                    for note in context.notes
                )
            )

    def test_zero_uncertainty_is_not_a_substitute_for_predictive_authority(
        self,
    ) -> None:
        goal = self._goal()
        event = self._event()
        forecast = self._forecast(event, uncertainty=Decimal("0"))

        with tempfile.TemporaryDirectory() as tmp:
            context, ledger_path = self._context(Path(tmp), event)

            self._agent(goal, forecast).on_market_event(event, context)

            self.assertEqual(context.paper_book.tickets, {})
            self.assertFalse(ledger_path.exists())
            self.assertTrue(
                any(
                    "canonical predictive ForecastRef authority" in note
                    for note in context.notes
                )
            )

    def test_self_attested_predictive_evidence_cannot_authorize_paper_exposure(
        self,
    ) -> None:
        goal = self._goal()
        event = self._event()
        forecast = self._forecast(event, uncertainty=Decimal("0.01"))
        caller_ref = self._self_attested_ref(event, forecast)

        with tempfile.TemporaryDirectory() as tmp:
            context, ledger_path = self._context(Path(tmp), event)

            self._agent(
                goal,
                forecast,
                predictive_ref=caller_ref,
            ).on_market_event(event, context)

            self.assertEqual(context.paper_book.tickets, {})
            self.assertFalse(ledger_path.exists())
            self.assertTrue(
                any(
                    "predictive eligibility was not resolved from canonical"
                    in note
                    for note in context.notes
                )
            )

    def test_resolver_minted_predictive_authority_uses_conservative_probability_and_persists_binding(
        self,
    ) -> None:
        event = MarketEvent(
            event_id="event-authority-paper",
            market_id="market-authority-paper",
            selection_id="selection-authority-paper",
            decimal_odds=Decimal("2"),
            observed_ts=_PREDICTIVE_HELPERS._DECISION_TIME,
            source_id="provider-1",
            sequence=1,
            source_ts="2026-01-04T00:01:59+00:00",
            ingest_ts=_PREDICTIVE_HELPERS._DECISION_TIME,
            sport="soccer",
        )
        forecast = ForecastRecord(
            quote_key=event.quote_key,
            probability=Decimal("0.60"),
            model_id="fixture-model",
            model_version="model-1",
            strategy_version="strategy-1",
            model_training_cutoff_ts=_PREDICTIVE_HELPERS._HELPERS.T1,
            input_cutoff_ts="2026-01-03T12:00:00+00:00",
            generated_at=_PREDICTIVE_HELPERS._FORECAST_TIME,
            uncertainty=Decimal("0.04"),
            market_snapshot_hash="a" * 64,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            authorized_ref = self._resolver_authorized_ref(
                root,
                event,
                forecast,
            )
            context, ledger_path = self._context(root, event)
            goal = self._goal()
            sizing_evidence = self._sizing_evidence(event, forecast)
            sizing_policy = self._sizing_policy()
            sizing_decision = self._sizing_decision(
                goal,
                event,
                forecast,
                sizing_evidence,
                sizing_policy,
            )
            self.assertEqual(sizing_decision.action, SizingAction.ELIGIBLE)

            self._agent(
                goal,
                forecast,
                predictive_ref=authorized_ref,
                sizing_evidence=sizing_evidence,
                sizing_policy=sizing_policy,
            ).on_market_event(event, context)

            self.assertEqual(len(context.paper_book.tickets), 1)
            self.assertTrue(ledger_path.exists())
            records = context.decision_ledger.verified_records()
            self.assertEqual(len(records), 1)
            payload = records[0].payload
            self.assertEqual(payload["probability"], "0.60")
            self.assertEqual(payload["uncertainty"], "0.04")
            self.assertEqual(payload["qualified_probability"], "0.56")
            self.assertEqual(
                payload["predictive_forecast_ref"],
                authorized_ref.to_dict(),
            )
            self.assertEqual(
                payload["expected_profit_per_unit"],
                "0.12",
            )
            self.assertEqual(
                payload["uncertainty_sizing_evidence_fingerprint"],
                sizing_evidence.fingerprint_sha256,
            )
            self.assertEqual(
                payload["uncertainty_sizing_policy_fingerprint"],
                sizing_policy.fingerprint_sha256,
            )
            self.assertEqual(
                payload["uncertainty_sizing_decision_fingerprint"],
                sizing_decision.decision_fingerprint_sha256,
            )
            self.assertEqual(
                payload["uncertainty_sizing_bankroll"],
                str(sizing_decision.bankroll),
            )
            self.assertEqual(
                payload["uncertainty_sizing_stake_ceiling"],
                str(sizing_decision.stake_ceiling),
            )
            self.assertLessEqual(
                Decimal(payload["requested_stake"]),
                sizing_decision.stake_ceiling,
            )

            # A restart observes the post-execution PaperBook balance, but must
            # re-resolve the historical sizing decision against the exact
            # decision-time bankroll persisted in the durable record.
            ticket_ids = set(context.paper_book.tickets)
            self.assertLess(context.paper_book.balance, sizing_decision.bankroll)
            self._agent(
                goal,
                forecast,
                predictive_ref=authorized_ref,
                sizing_evidence=sizing_evidence,
                sizing_policy=sizing_policy,
            ).on_market_event(event, context)
            self.assertEqual(set(context.paper_book.tickets), ticket_ids)

            # Once the material decision is durable, #623 recovery owns the
            # historical action before fresh forecast/sizing gates. A newer sizing
            # policy must not retroactively rewrite or re-size that committed action;
            # it only governs future material-action identities.
            changed_policy = self._sizing_policy(
                policy_id="paper-value-canonical-sizing-v2",
            )
            self._agent(
                goal,
                forecast,
                predictive_ref=authorized_ref,
                sizing_evidence=sizing_evidence,
                sizing_policy=changed_policy,
            ).on_market_event(event, context)
            self.assertEqual(set(context.paper_book.tickets), ticket_ids)
            recovered = context.decision_ledger.verified_records()
            self.assertEqual(len(recovered), 1)
            self.assertEqual(
                recovered[0].payload["uncertainty_sizing_policy_fingerprint"],
                sizing_policy.fingerprint_sha256,
            )

    def test_resolver_minted_uncertainty_erases_positive_point_edge_before_material_action(
        self,
    ) -> None:
        goal = self._goal()
        event = self._event(decimal_odds=Decimal("1.80"))
        forecast = self._forecast(
            event,
            uncertainty=Decimal("0.10"),
        )

        point_ev = forecast.probability * event.decimal_odds - Decimal("1")
        self.assertGreater(point_ev, 0)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            authorized_ref = self._resolver_authorized_ref(
                root,
                event,
                forecast,
            )
            context, ledger_path = self._context(root, event)
            agent = self._agent(
                goal,
                forecast,
                predictive_ref=authorized_ref,
            )

            qualified_probability = agent._qualified_forecast_probability(
                forecast,
                event,
                context,
            )
            self.assertEqual(qualified_probability, Decimal("0.50"))
            self.assertLessEqual(
                qualified_probability * event.decimal_odds - Decimal("1"),
                0,
            )

            agent.on_market_event(event, context)

            self.assertEqual(context.paper_book.tickets, {})
            self.assertFalse(ledger_path.exists())

    def test_canonical_sizing_abstains_when_all_in_payoff_erases_nominal_edge(
        self,
    ) -> None:
        goal = self._goal()
        event = self._event()
        forecast = self._forecast(event, uncertainty=Decimal("0.01"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            authorized_ref = self._resolver_authorized_ref(root, event, forecast)
            evidence = self._sizing_evidence(
                event,
                forecast,
                net_win_profit_per_stake=Decimal("0.50"),
            )
            policy = self._sizing_policy()
            decision = self._sizing_decision(
                goal,
                event,
                forecast,
                evidence,
                policy,
            )
            self.assertEqual(decision.action, SizingAction.ABSTAIN)
            self.assertIn("insufficient_conservative_edge", decision.reasons)
            context, ledger_path = self._context(root, event)

            self._agent(
                goal,
                forecast,
                predictive_ref=authorized_ref,
                sizing_evidence=evidence,
                sizing_policy=policy,
            ).on_market_event(event, context)

            self.assertEqual(context.paper_book.tickets, {})
            self.assertFalse(ledger_path.exists())
            self.assertTrue(
                any(
                    "insufficient_conservative_edge" in note
                    for note in context.notes
                )
            )

    def test_canonical_sizing_rejects_net_payoff_above_executable_gross_quote(
        self,
    ) -> None:
        goal = self._goal()
        event = self._event(decimal_odds=Decimal("2"))
        forecast = self._forecast(event, uncertainty=Decimal("0.01"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            authorized_ref = self._resolver_authorized_ref(root, event, forecast)
            evidence = self._sizing_evidence(
                event,
                forecast,
                net_win_profit_per_stake=Decimal("2"),
            )
            policy = self._sizing_policy()

            # The standalone generic evaluator cannot know this executable quote's
            # payoff boundary and therefore considers the forged upside eligible.
            # PaperValue composition must bind sizing economics to execution truth.
            generic = self._sizing_decision(
                goal,
                event,
                forecast,
                evidence,
                policy,
            )
            self.assertEqual(generic.action, SizingAction.ELIGIBLE)
            self.assertGreater(
                evidence.net_win_profit_per_stake,
                event.decimal_odds - Decimal("1"),
            )

            context, ledger_path = self._context(root, event)
            self._agent(
                goal,
                forecast,
                predictive_ref=authorized_ref,
                sizing_evidence=evidence,
                sizing_policy=policy,
            ).on_market_event(event, context)

            self.assertEqual(context.paper_book.tickets, {})
            self.assertFalse(ledger_path.exists())
            self.assertTrue(
                any(
                    "payoff exceeds current executable gross quote payoff" in note
                    for note in context.notes
                )
            )

    def test_canonical_sizing_width_policy_can_abstain_despite_positive_point_edge(
        self,
    ) -> None:
        goal = self._goal()
        event = self._event()
        forecast = self._forecast(event, uncertainty=Decimal("0.01"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            authorized_ref = self._resolver_authorized_ref(root, event, forecast)
            evidence = self._sizing_evidence(event, forecast)
            policy = self._sizing_policy(
                max_uncertainty_width=Decimal("0.01"),
            )
            decision = self._sizing_decision(
                goal,
                event,
                forecast,
                evidence,
                policy,
            )
            self.assertEqual(decision.action, SizingAction.ABSTAIN)
            self.assertIn("uncertainty_too_wide", decision.reasons)
            context, ledger_path = self._context(root, event)

            self._agent(
                goal,
                forecast,
                predictive_ref=authorized_ref,
                sizing_evidence=evidence,
                sizing_policy=policy,
            ).on_market_event(event, context)

            self.assertEqual(context.paper_book.tickets, {})
            self.assertFalse(ledger_path.exists())

    def test_canonical_sizing_ceiling_only_tightens_downstream_risk_stake(
        self,
    ) -> None:
        goal = self._goal()
        event = self._event()
        forecast = self._forecast(event, uncertainty=Decimal("0.01"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            authorized_ref = self._resolver_authorized_ref(root, event, forecast)
            evidence = self._sizing_evidence(event, forecast)
            policy = self._sizing_policy(
                max_bankroll_fraction=Decimal("0.001"),
            )
            decision = self._sizing_decision(
                goal,
                event,
                forecast,
                evidence,
                policy,
            )
            self.assertEqual(decision.action, SizingAction.ELIGIBLE)
            self.assertEqual(decision.stake_ceiling, Decimal("0.100"))
            context, ledger_path = self._context(root, event)

            self._agent(
                goal,
                forecast,
                predictive_ref=authorized_ref,
                sizing_evidence=evidence,
                sizing_policy=policy,
            ).on_market_event(event, context)

            self.assertEqual(len(context.paper_book.tickets), 1)
            ticket = next(iter(context.paper_book.tickets.values()))
            self.assertEqual(ticket.stake, decision.stake_ceiling)
            records = context.decision_ledger.verified_records()
            self.assertEqual(len(records), 1)
            self.assertEqual(
                Decimal(records[0].payload["requested_stake"]),
                decision.stake_ceiling,
            )

    def test_coordinated_sizing_witness_rebind_cannot_mint_eligible(
        self,
    ) -> None:
        goal = self._goal()
        event = self._event()
        forecast = self._forecast(event, uncertainty=Decimal("0.01"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            authorized_ref = self._resolver_authorized_ref(root, event, forecast)
            abstain_evidence = self._sizing_evidence(
                event,
                forecast,
                net_win_profit_per_stake=Decimal("0.50"),
            )
            eligible_evidence = self._sizing_evidence(event, forecast)
            policy = self._sizing_policy()
            forged_decision = self._sizing_decision(
                goal,
                event,
                forecast,
                eligible_evidence,
                policy,
            )
            self.assertEqual(forged_decision.action, SizingAction.ELIGIBLE)
            context, ledger_path = self._context(root, event)

            forged_called = False

            def forged_evaluator(evidence, request, policy):
                nonlocal forged_called
                forged_called = True
                return forged_decision

            missing = object()
            names = (
                "evaluate_uncertainty_sizing",
                "_UNCERTAINTY_SIZING_EVALUATOR",
                "_UNCERTAINTY_SIZING_EVALUATOR_CODE",
            )
            previous = {name: getattr(paper_strategy_module, name, missing) for name in names}
            try:
                paper_strategy_module.evaluate_uncertainty_sizing = forged_evaluator
                paper_strategy_module._UNCERTAINTY_SIZING_EVALUATOR = forged_evaluator
                paper_strategy_module._UNCERTAINTY_SIZING_EVALUATOR_CODE = forged_evaluator.__code__
                self._agent(
                    goal,
                    forecast,
                    predictive_ref=authorized_ref,
                    sizing_evidence=abstain_evidence,
                    sizing_policy=policy,
                ).on_market_event(event, context)
            finally:
                for name, value in previous.items():
                    if value is missing:
                        delattr(paper_strategy_module, name)
                    else:
                        setattr(paper_strategy_module, name, value)

            self.assertFalse(
                forged_called,
                "consumer-owned forged evaluator must not be an authority boundary",
            )
            self.assertEqual(context.paper_book.tickets, {})
            self.assertFalse(ledger_path.exists())
            self.assertTrue(
                any("insufficient_conservative_edge" in note for note in context.notes)
            )

    def test_sizing_evidence_class_method_rebind_cannot_mint_eligible(
        self,
    ) -> None:
        goal = self._goal()
        event = self._event()
        forecast = self._forecast(event, uncertainty=Decimal("0.01"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            authorized_ref = self._resolver_authorized_ref(root, event, forecast)
            abstain_evidence = self._sizing_evidence(
                event,
                forecast,
                net_win_profit_per_stake=Decimal("0.50"),
            )
            eligible_evidence = self._sizing_evidence(event, forecast)
            policy = self._sizing_policy()
            forged_decision = self._sizing_decision(
                goal,
                event,
                forecast,
                eligible_evidence,
                policy,
            )
            self.assertEqual(forged_decision.action, SizingAction.ELIGIBLE)
            context, ledger_path = self._context(root, event)

            forged_called = False
            missing = object()
            original = getattr(
                UncertaintySizingEvidence,
                "evaluate_canonical",
                missing,
            )

            def forged_class_dispatch(self, request, sizing_policy):
                nonlocal forged_called
                forged_called = True
                return forged_decision

            try:
                UncertaintySizingEvidence.evaluate_canonical = forged_class_dispatch
                self._agent(
                    goal,
                    forecast,
                    predictive_ref=authorized_ref,
                    sizing_evidence=abstain_evidence,
                    sizing_policy=policy,
                ).on_market_event(event, context)
            finally:
                if original is missing:
                    delattr(UncertaintySizingEvidence, "evaluate_canonical")
                else:
                    UncertaintySizingEvidence.evaluate_canonical = original

            self.assertFalse(
                forged_called,
                "mutable evidence class dispatch must not be a PAPER sizing authority",
            )
            self.assertEqual(context.paper_book.tickets, {})
            self.assertFalse(ledger_path.exists())
            self.assertTrue(
                any("insufficient_conservative_edge" in note for note in context.notes)
            )

    def test_coordinated_predictive_witness_rebind_cannot_authorize_ref(
        self,
    ) -> None:
        goal = self._goal()
        event = self._event()
        forecast = self._forecast(event, uncertainty=Decimal("0.01"))
        caller_ref = self._self_attested_ref(event, forecast)

        original = ForecastRef.predictive_eligibility_reason

        def permissive(self, decision_time, *, expected_model_id):
            return None

        missing = object()
        names = (
            "_PREDICTIVE_FORECAST_ELIGIBILITY_REASON",
            "_PREDICTIVE_FORECAST_ELIGIBILITY_REASON_CODE",
        )
        previous = {name: getattr(paper_strategy_module, name, missing) for name in names}
        try:
            ForecastRef.predictive_eligibility_reason = permissive
            paper_strategy_module._PREDICTIVE_FORECAST_ELIGIBILITY_REASON = permissive
            paper_strategy_module._PREDICTIVE_FORECAST_ELIGIBILITY_REASON_CODE = permissive.__code__
            with tempfile.TemporaryDirectory() as tmp:
                context, ledger_path = self._context(Path(tmp), event)
                self._agent(
                    goal,
                    forecast,
                    predictive_ref=caller_ref,
                ).on_market_event(event, context)

                self.assertEqual(context.paper_book.tickets, {})
                self.assertFalse(ledger_path.exists())
                self.assertTrue(
                    any(
                        "predictive eligibility was not resolved from canonical" in note
                        for note in context.notes
                    )
                )
        finally:
            ForecastRef.predictive_eligibility_reason = original
            for name, value in previous.items():
                if value is missing:
                    delattr(paper_strategy_module, name)
                else:
                    setattr(paper_strategy_module, name, value)

    def test_predictive_verifier_code_mutation_fails_closed_before_authority(
        self,
    ) -> None:
        goal = self._goal()
        event = self._event()
        forecast = self._forecast(event, uncertainty=Decimal("0.01"))
        caller_ref = self._self_attested_ref(event, forecast)
        verifier = ForecastRef.predictive_eligibility_reason
        original_code = verifier.__code__

        def replacement_factory():
            closure_anchor = object()

            def permissive(
                self,
                decision_time,
                *,
                expected_model_id,
            ):
                if closure_anchor is None:
                    return "unreachable"
                return None

            return permissive

        replacement = replacement_factory()
        self.assertEqual(
            len(replacement.__code__.co_freevars),
            len(verifier.__code__.co_freevars),
        )
        try:
            verifier.__code__ = replacement.__code__
            with tempfile.TemporaryDirectory() as tmp:
                context, ledger_path = self._context(Path(tmp), event)
                self._agent(
                    goal,
                    forecast,
                    predictive_ref=caller_ref,
                ).on_market_event(event, context)

                self.assertEqual(context.paper_book.tickets, {})
                self.assertFalse(ledger_path.exists())
                self.assertTrue(
                    any(
                        "predictive authority verifier integrity changed" in note
                        for note in context.notes
                    )
                )
        finally:
            verifier.__code__ = original_code

    def test_predictive_reference_mapping_is_snapshotted_and_key_bound(self) -> None:
        event = self._event()
        forecast = self._forecast(event, uncertainty=Decimal("0.01"))
        caller_ref = self._self_attested_ref(event, forecast)
        refs = {event.quote_key: caller_ref}

        agent = PaperValueAgent(
            {event.quote_key: forecast},
            risk_policy=PaperRiskPolicy(economic_goal=self._goal()),
            predictive_forecast_refs=refs,
        )
        refs.clear()
        self.assertIs(
            agent.predictive_forecast_refs[event.quote_key],
            caller_ref,
        )

        with self.assertRaisesRegex(
            ValueError,
            "key must match",
        ):
            PaperValueAgent(
                {event.quote_key: forecast},
                predictive_forecast_refs={"wrong-key": caller_ref},
            )


if __name__ == "__main__":
    unittest.main()

from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path

import pytest

from autosport.decision_ledger import JsonlDecisionLedger
from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_provenance import provenance_for
from autosport.opportunity import Opportunity, OpportunityDecision, QuoteRef, StrategyClass
from autosport.paper import PaperBook
from autosport.portfolio_plan import (
    EvidenceTruth,
    OpportunityEvidence,
    OpportunityIntent,
    PortfolioAction,
    PortfolioDependencyGraph,
    PortfolioPlan,
    persist_portfolio_plan_decision,
)
from autosport.pre_evaluation_binding import (
    PreEvaluationDenominatorContext,
    bind_pre_evaluation_session,
)
from autosport.pre_evaluation_evidence import (
    PreEvaluationEvidenceAuthority,
    PreEvaluationPolicy,
)
from autosport.pre_evaluation_product_origin import (
    PreEvaluationProductOriginError,
    derive_product_owned_pre_evaluation_session,
    persist_pre_evaluation_cost_contract_authority,
)
from autosport.pre_evaluation_semantics import (
    PreEvaluationCostContract,
    ProviderSelectionBinding,
    SemanticAttritionReason,
    SemanticFunnelStage,
)
from autosport.provider_observation_authority import (
    CompleteGameBoardRequest,
    CompleteGameBoardSnapshot,
)
from autosport import provider_observation_authority
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SNAPSHOT_SHA = "9" * 64
DECISION_TS = "2026-09-18T13:20:00+00:00"


def _goal() -> EconomicGoalContract:
    return EconomicGoalContract(
        goal_id="goal-pre-evaluation-origin",
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


def _risk_policy() -> PaperRiskPolicy:
    return PaperRiskPolicy(
        max_ticket_fraction=Decimal("1"),
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
        economic_goal=_goal(),
    )


def _snapshot() -> CompleteGameBoardSnapshot:
    request = CompleteGameBoardRequest(
        sport_key="soccer",
        bookmakers=("bookmaker-1",),
    )
    frame = {
        "type": "initial_state",
        "sport_key": "soccer",
        "snapshot_scope": "current_game_board",
        "snapshot_complete": True,
        "truncated": False,
        "resume_mode": "replace",
        "partial": False,
        "data": [
            {
                "event_id": "event-1",
                "bookmaker": "bookmaker-1",
                "kind": "game",
                "market_key": "h2h",
                "last_update": "2026-09-18T13:19:58Z",
            }
        ],
        "count": 1,
    }
    return provider_observation_authority._remember(
        CompleteGameBoardSnapshot(
            request=request,
            captured_at="2026-09-18T13:19:59Z",
            frame_json=json.dumps(frame),
        )
    )


def _provider(snapshot: CompleteGameBoardSnapshot) -> ProviderSelectionBinding:
    return ProviderSelectionBinding(
        row_key="row-a",
        member_sha256=SHA_C,
        event_id="event-1",
        market_id="bookmaker-1:h2h",
        selection_id="bookmaker-1:h2h:home",
        source_id=snapshot.request.source_id,
        source_at="2026-09-18T13:19:58Z",
    )


def _intent(snapshot: CompleteGameBoardSnapshot) -> OpportunityIntent:
    goal = _goal()
    leg = TicketLeg(
        "event-1",
        "bookmaker-1:h2h",
        "bookmaker-1:h2h:home",
        Decimal("2"),
        sport="soccer",
    )
    quote = MarketEvent(
        event_id=leg.event_id,
        market_id=leg.market_id,
        selection_id=leg.selection_id,
        decimal_odds=Decimal("2"),
        observed_ts="2026-09-18T13:19:59+00:00",
        source_id=snapshot.request.source_id,
        sequence=1,
        source_ts="2026-09-18T13:19:59+00:00",
        ingest_ts="2026-09-18T13:19:59+00:00",
        sport="soccer",
    )
    context = ProposedTicketRiskContext(
        legs=(leg,),
        quotes=(quote,),
        bankroll_id=goal.bankroll_id,
        currency=goal.currency,
        proposal_ts=DECISION_TS,
    )
    opportunity = Opportunity(
        strategy_class=StrategyClass.VALUE,
        decision=OpportunityDecision.ACTIONABLE,
        quotes=(QuoteRef.from_market_event(quote, market_snapshot_hash=SNAPSHOT_SHA),),
    )
    evidence = OpportunityEvidence(
        evidence_id="origin-evidence-1",
        observed_at="2026-09-18T13:19:59+00:00",
        causal_cutoff="2026-09-18T13:19:58+00:00",
        reproducibility_sha256=SHA_A,
        truth=EvidenceTruth.EXACT,
        outcome_space_complete=True,
        terminal_state_space_sha256=SHA_B,
        execution_assumptions_sha256=SHA_C,
        execution_feasible=True,
    )
    return OpportunityIntent(
        intent_id="origin-intent-1",
        opportunity=opportunity,
        evidence=evidence,
        risk_context=context,
        signal_strength=Decimal("0.01"),
        strategy_id="strategy-v1",
        config_sha256="e" * 64,
    )


def _bound(snapshot: CompleteGameBoardSnapshot, provider: ProviderSelectionBinding):
    evidence = PreEvaluationEvidenceAuthority(
        PreEvaluationPolicy(max_age_ns=200)
    ).evaluate_session(
        session_id="session-1",
        candidate_ids=(provider.row_key,),
        resolver=lambda _: None,
        evaluated_at_ns=1000,
    )
    context = PreEvaluationDenominatorContext(
        session_id="session-1",
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=SHA_A,
        provider_evidence_sha256=snapshot.evidence_sha256,
    )
    return bind_pre_evaluation_session(
        evidence,
        context=context,
        provider_members=(provider.identity,),
    )


def _persist_intent_authority(
    *,
    ledger: JsonlDecisionLedger,
    intent: OpportunityIntent,
    book: PaperBook,
    risk_policy: PaperRiskPolicy,
    graph: PortfolioDependencyGraph,
) -> None:
    goal = risk_policy.economic_goal
    assert goal is not None
    plan = PortfolioPlan(
        decision_ts=DECISION_TS,
        action=PortfolioAction.WAIT,
        stakes=(Decimal("0"),),
        intent_ids=(intent.intent_id,),
        intent_sha256s=(intent.intent_sha256,),
        opportunity_classes=(intent.opportunity_class.value,),
        portfolio_sha256=graph.portfolio_sha256,
        dependency_graph=graph,
        terminal_economics=None,
        economic_goal_contract_sha256=provenance_for(goal).contract_sha256,
        risk_policy_sha256=risk_policy.provenance_sha256,
        portfolio_truth=EvidenceTruth.EXACT,
        reason="durable pre-evaluation intent origin",
    )
    persist_portfolio_plan_decision(
        ledger,
        plan,
        (intent,),
        risk_policy,
        initialize_ledger=True,
        replay_run_id="origin-replay-1",
        material_action_id="pre-evaluation-origin-1",
    )


def test_durable_product_origin_survives_restart_and_rejects_forged_intent(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot()
    provider = _provider(snapshot)
    intent = _intent(snapshot)
    book = PaperBook("10000")
    risk_policy = _risk_policy()
    graph = PortfolioDependencyGraph.for_inputs(book, (intent,))
    ledger_path = tmp_path / "decision-ledger.jsonl"
    ledger = JsonlDecisionLedger(ledger_path)
    _persist_intent_authority(
        ledger=ledger,
        intent=intent,
        book=book,
        risk_policy=risk_policy,
        graph=graph,
    )
    contract = PreEvaluationCostContract(contract_id="cost-v1", max_cost_micros=10)
    persist_pre_evaluation_cost_contract_authority(
        ledger=ledger,
        material_action_id="pre-evaluation-origin-1",
        risk_policy=risk_policy,
        contract=contract,
    )
    kwargs = {
        "snapshot": snapshot,
        "bound": _bound(snapshot, provider),
        "provider_selections": (provider,),
        "intents": (intent,),
        "risk_policy": risk_policy,
        "book": book,
        "dependency_graph": graph,
        "material_action_id": "pre-evaluation-origin-1",
    }

    first = derive_product_owned_pre_evaluation_session(
        **kwargs,
        ledger=ledger,
        expected_cost_contract_sha256=contract.contract_sha256,
    )
    restarted = derive_product_owned_pre_evaluation_session(
        **kwargs,
        ledger=JsonlDecisionLedger(ledger_path),
    )

    assert restarted.authority_digest == first.authority_digest
    assert restarted.origin.cost_contract_sha256 == contract.contract_sha256
    slot = restarted.resolve_slot(provider.row_key)
    assert slot.decision_stage is SemanticFunnelStage.ELIGIBLE
    assert slot.attrition_reason is SemanticAttritionReason.THEORETICAL_ONLY
    assert slot.opportunity_intent_sha256 == intent.intent_sha256

    alternate = PreEvaluationCostContract(contract_id="cost-v2", max_cost_micros=999)
    with pytest.raises(
        PreEvaluationProductOriginError,
        match="expected cost contract does not match durable product selection",
    ):
        derive_product_owned_pre_evaluation_session(
            **kwargs,
            ledger=JsonlDecisionLedger(ledger_path),
            expected_cost_contract_sha256=alternate.contract_sha256,
        )
    with pytest.raises(
        PreEvaluationProductOriginError,
        match="selection conflicts",
    ):
        persist_pre_evaluation_cost_contract_authority(
            ledger=JsonlDecisionLedger(ledger_path),
            material_action_id="pre-evaluation-origin-1",
            risk_policy=risk_policy,
            contract=alternate,
        )

    forged_intent = replace(intent, config_sha256="f" * 64)
    forged_graph = PortfolioDependencyGraph.for_inputs(book, (forged_intent,))
    with pytest.raises(PreEvaluationProductOriginError, match="durable PortfolioPlan"):
        derive_product_owned_pre_evaluation_session(
            **{
                **kwargs,
                "intents": (forged_intent,),
                "dependency_graph": forged_graph,
            },
            ledger=JsonlDecisionLedger(ledger_path),
        )

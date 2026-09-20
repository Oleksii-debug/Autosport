import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from autosport.domain import MarketEvent, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.opportunity import Opportunity, OpportunityDecision, QuoteRef, StrategyClass
from autosport.paper import PaperBook
from autosport.portfolio_plan import (
    EvidenceTruth,
    OpportunityEvidence,
    OpportunityIntent,
    PortfolioDependencyGraph,
)
from autosport.pre_evaluation_binding import (
    PreEvaluationDenominatorContext,
    ProviderMemberIdentity,
    bind_pre_evaluation_session,
)
from autosport.pre_evaluation_evidence import (
    CanonicalCandidateFacts,
    PreEvaluationEvidenceAuthority,
    PreEvaluationPolicy,
)
from autosport.pre_evaluation_semantics import (
    PreEvaluationCostContract,
    PreEvaluationSemanticAuthority,
    PreEvaluationSemanticStore,
    ProviderSelectionBinding,
    SemanticAttritionReason,
    SemanticFunnelStage,
    SemanticSlotState,
)
from autosport.risk import PaperRiskPolicy, ProposedTicketRiskContext


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SNAPSHOT_SHA = "9" * 64
DECISION_TS = "2026-09-18T13:20:00+00:00"


def _goal(
    *,
    max_quote_age_seconds: Decimal = Decimal("3600"),
) -> EconomicGoalContract:
    return EconomicGoalContract(
        goal_id="goal-pre-evaluation",
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
        max_quote_age_seconds=max_quote_age_seconds,
        max_concurrent_positions=10,
    )


def _risk_policy(
    *,
    max_ticket_fraction: Decimal = Decimal("1"),
    max_quote_age_seconds: Decimal = Decimal("3600"),
) -> PaperRiskPolicy:
    return PaperRiskPolicy(
        max_ticket_fraction=max_ticket_fraction,
        max_committed_fraction=Decimal("1"),
        minimum_cash_reserve_fraction=Decimal("0"),
        economic_goal=_goal(max_quote_age_seconds=max_quote_age_seconds),
    )


def _intent(
    *,
    decision: OpportunityDecision = OpportunityDecision.ACTIONABLE,
    intent_id: str = "intent-1",
) -> OpportunityIntent:
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
        source_id="provider-1",
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
        strategy_class=StrategyClass.ARBITRAGE,
        decision=decision,
        quotes=(QuoteRef.from_market_event(quote, market_snapshot_hash=SNAPSHOT_SHA),),
    )
    evidence = OpportunityEvidence(
        evidence_id=f"evidence-{intent_id}",
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
        intent_id=intent_id,
        opportunity=opportunity,
        evidence=evidence,
        risk_context=context,
        signal_strength=Decimal("0.05"),
        strategy_id="strategy-v1",
        config_sha256="e" * 64,
    )


def _provider(*, empty: bool = False) -> ProviderSelectionBinding:
    return ProviderSelectionBinding(
        row_key="row-a",
        member_sha256=SHA_C,
        event_id=None if empty else "event-1",
        market_id=None if empty else "bookmaker-1:h2h",
        selection_id=None if empty else "bookmaker-1:h2h:home",
        source_id="provider-1",
        source_at="2026-09-18T13:19:58+00:00",
    )


def _bound(
    *,
    intent: OpportunityIntent | None,
    provider: ProviderSelectionBinding | None = None,
    max_age_ns: int = 200,
    observed_at_ns: int = 900,
    cost_limit_micros: int = 10,
    config_enabled: bool = True,
    risk_required_micros: int = 1,
    risk_available_micros: int = 1,
    cost_estimate_micros: int = 4,
):
    provider = provider or _provider()
    facts = None
    if intent is not None:
        facts = CanonicalCandidateFacts(
            candidate_id=provider.row_key,
            observed_at_ns=observed_at_ns,
            config_enabled=config_enabled,
            risk_required_micros=risk_required_micros,
            risk_available_micros=risk_available_micros,
            cost_estimate_micros=cost_estimate_micros,
            cost_limit_micros=cost_limit_micros,
            source_authority_id=f"opportunity-intent:{intent.intent_id}",
            source_revision=intent.intent_sha256,
        )
    evidence = PreEvaluationEvidenceAuthority(
        PreEvaluationPolicy(max_age_ns=max_age_ns)
    ).evaluate_session(
        session_id="session-1",
        candidate_ids=(provider.row_key,),
        resolver=lambda row_key: facts if row_key == provider.row_key else None,
        evaluated_at_ns=1000,
    )
    context = PreEvaluationDenominatorContext(
        session_id="session-1",
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=SHA_A,
        provider_evidence_sha256=SHA_B,
    )
    return bind_pre_evaluation_session(
        evidence,
        context=context,
        provider_members=(provider.identity,),
    )


def _derive(
    *,
    intent: OpportunityIntent | None,
    provider: ProviderSelectionBinding | None = None,
    bound=None,
    risk_policy: PaperRiskPolicy | None = None,
    book: PaperBook | None = None,
):
    provider = provider or _provider()
    book = book or PaperBook("10000")
    intents = () if intent is None else (intent,)
    graph = PortfolioDependencyGraph.for_inputs(book, intents)
    authority = PreEvaluationSemanticAuthority(
        PreEvaluationCostContract(contract_id="cost-v1", max_cost_micros=10)
    )
    return authority.derive_session(
        bound=bound or _bound(intent=intent, provider=provider),
        provider_selections=(provider,),
        intents=intents,
        risk_policy=risk_policy or _risk_policy(),
        book=book,
        dependency_graph=graph,
    )


def test_actionable_slot_semantics_are_derived_from_canonical_objects() -> None:
    intent = _intent()
    session = _derive(intent=intent)
    slot = session.resolve_slot("row-a")

    assert slot.slot_state is SemanticSlotState.CANDIDATE
    assert slot.decision_stage is SemanticFunnelStage.ELIGIBLE
    assert slot.attrition_reason is SemanticAttritionReason.THEORETICAL_ONLY
    assert slot.opportunity_intent_sha256 == intent.intent_sha256
    assert slot.strategy_version_id == intent.strategy_id
    assert slot.config_sha256 == intent.config_sha256
    assert slot.quote_set_sha256 is not None
    assert slot.quote_identity_sha256 is not None
    assert slot.freshness_policy_sha256 == (
        PreEvaluationSemanticAuthority._freshness_policy_sha256(_risk_policy())
    )
    assert slot.freshness_policy_sha256 != _bound(intent=intent).evidence.policy_digest
    assert slot.portfolio_before_id.startswith("paper-book:")
    assert slot.risk_policy_id.startswith("paper-risk-policy:")
    assert slot.economic_goal_id.startswith("goal-pre-evaluation@1:")
    assert slot.dependence_cluster_keys[0].startswith("dependency:")


def test_missing_positive_intent_derives_no_candidate_without_caller_choice() -> None:
    session = _derive(intent=None, bound=_bound(intent=None))
    slot = session.resolve_slot("row-a")
    assert slot.slot_state is SemanticSlotState.NO_CANDIDATE
    assert slot.decision_stage is SemanticFunnelStage.OBSERVED_SLOT
    assert slot.attrition_reason is SemanticAttritionReason.NO_CANDIDATE
    assert slot.opportunity_intent_sha256 is None
    assert slot.quote_set_sha256 is None


def test_empty_provider_member_derives_no_event() -> None:
    provider = _provider(empty=True)
    session = _derive(
        intent=None,
        provider=provider,
        bound=_bound(intent=None, provider=provider),
    )
    slot = session.resolve_slot(provider.row_key)
    assert slot.slot_state is SemanticSlotState.NO_EVENT
    assert slot.attrition_reason is SemanticAttritionReason.NO_EVENT


@pytest.mark.parametrize("decision", [OpportunityDecision.WAIT, OpportunityDecision.ZERO])
def test_canonical_wait_or_zero_intent_derives_wait_zero(decision: OpportunityDecision) -> None:
    intent = _intent(decision=decision)
    slot = _derive(intent=intent).resolve_slot("row-a")
    assert slot.slot_state is SemanticSlotState.WAIT_ZERO
    assert slot.decision_stage is SemanticFunnelStage.OBSERVED_SLOT
    assert slot.attrition_reason is SemanticAttritionReason.WAIT_ZERO
    assert slot.opportunity_intent_sha256 == intent.intent_sha256


def test_legacy_facts_cannot_mint_config_freshness_risk_or_cost_semantics() -> None:
    intent = _intent()
    baseline = _derive(intent=intent)
    baseline_slot = baseline.resolve_slot("row-a")
    assert baseline_slot.attrition_reason is SemanticAttritionReason.THEORETICAL_ONLY

    forged_bounds = (
        _bound(intent=intent, max_age_ns=0),
        _bound(intent=intent, config_enabled=False),
        _bound(
            intent=intent,
            risk_required_micros=2,
            risk_available_micros=1,
        ),
        _bound(
            intent=intent,
            cost_estimate_micros=99,
            cost_limit_micros=0,
        ),
    )
    for forged_bound in forged_bounds:
        forged = _derive(intent=intent, bound=forged_bound)
        assert forged.resolve_slot("row-a").attrition_reason is (
            SemanticAttritionReason.THEORETICAL_ONLY
        )
        assert forged.authority_digest == baseline.authority_digest


def test_canonical_risk_policy_quote_age_owns_freshness_veto() -> None:
    intent = _intent()
    stale = _derive(
        intent=intent,
        risk_policy=_risk_policy(max_quote_age_seconds=Decimal("0")),
    )
    assert stale.resolve_slot("row-a").attrition_reason is SemanticAttritionReason.STALE_QUOTE


def test_legacy_source_identity_cannot_mint_positive_semantic_authority() -> None:
    intent = _intent()
    provider = _provider()
    baseline = _derive(intent=intent, provider=provider)
    bound = _bound(intent=intent, provider=provider)
    facts = bound.evidence.slots[0].facts
    assert facts is not None
    forged_facts = replace(
        facts,
        source_authority_id="forged-authority",
        source_revision="f" * 64,
    )
    forged_evidence = PreEvaluationEvidenceAuthority(
        PreEvaluationPolicy(max_age_ns=200)
    ).evaluate_session(
        session_id="session-1",
        candidate_ids=(provider.row_key,),
        resolver=lambda _: forged_facts,
        evaluated_at_ns=1000,
    )
    forged_bound = bind_pre_evaluation_session(
        forged_evidence,
        context=bound.context,
        provider_members=(provider.identity,),
    )

    forged = _derive(intent=intent, provider=provider, bound=forged_bound)
    assert forged.authority_digest == baseline.authority_digest
    assert forged.resolve_slot("row-a").opportunity_intent_sha256 == intent.intent_sha256


def test_provider_source_timestamp_is_observational_not_semantic_authority() -> None:
    intent = _intent()
    provider = _provider()
    baseline = _derive(intent=intent, provider=provider)
    shifted_provider = replace(
        provider,
        source_at="2026-09-18T13:19:57+00:00",
    )
    shifted = _derive(
        intent=intent,
        provider=shifted_provider,
        bound=_bound(intent=intent, provider=shifted_provider),
    )
    assert shifted.authority_digest == baseline.authority_digest


def test_provider_semantic_binding_must_equal_exact_bound_member_identity() -> None:
    intent = _intent()
    bound = _bound(intent=intent)
    wrong = replace(_provider(), member_sha256=SHA_A)
    with pytest.raises(ValueError, match="exactly equal"):
        _derive(intent=intent, provider=wrong, bound=bound)


def test_actual_risk_policy_is_bound_and_can_deny_candidate() -> None:
    intent = _intent()
    accepted = _derive(intent=intent, risk_policy=_risk_policy())
    denied = _derive(
        intent=intent,
        risk_policy=_risk_policy(max_ticket_fraction=Decimal("0")),
    )

    assert accepted.resolve_slot("row-a").decision_stage is SemanticFunnelStage.ELIGIBLE
    assert denied.resolve_slot("row-a").attrition_reason is SemanticAttritionReason.RISK_REJECTED
    assert accepted.risk_policy_sha256 != denied.risk_policy_sha256
    assert accepted.authority_digest != denied.authority_digest


def test_dependency_graph_and_portfolio_are_exact_authority_inputs() -> None:
    intent = _intent()
    provider = _provider()
    bound = _bound(intent=intent, provider=provider)
    authority = PreEvaluationSemanticAuthority(
        PreEvaluationCostContract(contract_id="cost-v1", max_cost_micros=10)
    )
    book = PaperBook("10000")
    other_book = PaperBook("20000")
    graph = PortfolioDependencyGraph.for_inputs(other_book, (intent,))

    with pytest.raises(ValueError, match="exact portfolio"):
        authority.derive_session(
            bound=bound,
            provider_selections=(provider,),
            intents=(intent,),
            risk_policy=_risk_policy(),
            book=book,
            dependency_graph=graph,
        )


def test_semantic_store_is_immutable_and_restart_requires_reresolved_root(
    tmp_path: Path,
) -> None:
    intent = _intent()
    session = _derive(intent=intent)
    store = PreEvaluationSemanticStore(tmp_path / "semantics.json")
    store.save(session)
    store.save(session)
    assert store.load_expected(session) == session

    changed = _derive(
        intent=intent,
        risk_policy=_risk_policy(max_ticket_fraction=Decimal("0")),
    )
    with pytest.raises(ValueError, match="conflicting authority"):
        store.save(changed)
    with pytest.raises(ValueError, match="re-resolved authority"):
        store.load_expected(changed)


def test_semantic_store_rejects_persisted_field_mutation(tmp_path: Path) -> None:
    session = _derive(intent=_intent())
    store = PreEvaluationSemanticStore(tmp_path / "semantics.json")
    store.save(session)

    raw = json.loads(store.path.read_text(encoding="utf-8"))
    raw["payload"]["slots"][0]["attrition_reason"] = "RISK_REJECTED"
    store.path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError):
        store.load()

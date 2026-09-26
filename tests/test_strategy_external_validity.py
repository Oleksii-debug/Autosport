from __future__ import annotations

import hashlib

import pytest

from autosport.evaluation_intake import ObservationIntakeSnapshot
from autosport.evaluation_universe import (
    AttritionReason,
    EvaluationRow,
    EvaluationUniverse,
    EvaluationUniverseLedger,
    FunnelEvent,
    FunnelStage,
    SlotState,
)
from autosport.external_validity_baseline import (
    BaselineDefinition,
    BaselineKind,
    EvaluationContractFamily,
    FrozenBaselineProtocol,
    FrozenEvidenceScope,
    PolicyEvaluation,
    REQUIRED_BASELINE_KINDS,
    canonical_evaluation_contract,
)
from autosport.opportunity import StrategyClass
from autosport.strategy_external_validity import (
    StrategyEvidenceGrade,
    StrategyExternalValidityError,
    evaluate_strategy_external_validity,
)

H = "a" * 64
H2 = "b" * 64
H3 = "c" * 64
H4 = "d" * 64


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _row(
    key: str,
    *,
    slot: SlotState = SlotState.CANDIDATE,
    strategy: str = "strategy-1",
    terminal: bool = True,
    portfolio_before_id: str = "portfolio-1",
) -> EvaluationRow:
    candidate = slot is SlotState.CANDIDATE
    reason = None if candidate else {
        SlotState.NO_EVENT: AttritionReason.NO_EVENT,
        SlotState.NO_QUOTE: AttritionReason.NO_QUOTE,
        SlotState.SOURCE_OUTAGE: AttritionReason.SOURCE_OUTAGE,
        SlotState.NO_CANDIDATE: AttritionReason.NO_CANDIDATE,
        SlotState.WAIT_ZERO: AttritionReason.WAIT_ZERO,
    }[slot]
    event_id = None if slot is SlotState.NO_EVENT else "event-1"
    return EvaluationRow(
        row_key=key,
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=H,
        universe_id="universe-1",
        slot_state=slot,
        decision_stage=FunnelStage.EXECUTION_MODEL_ELIGIBLE if candidate else FunnelStage.OBSERVED_SLOT,
        attrition_reason=reason,
        sport="football",
        provider_id="provider-1",
        source_id="source-1",
        event_id=event_id,
        market_id=None if event_id is None else "market-1",
        selection_id=None if event_id is None else "selection-1",
        source_at="2026-09-20T00:00:00Z",
        received_at="2026-09-20T00:00:01Z",
        committed_at="2026-09-20T00:00:02Z",
        detection_at="2026-09-20T00:00:03Z" if candidate else None,
        decision_at="2026-09-20T00:00:04Z" if candidate else None,
        quote_set_sha256=H2 if candidate else None,
        freshness_policy_sha256=H3,
        strategy_version_id=strategy,
        model_version_id="model-1" if candidate else None,
        config_sha256=H4,
        portfolio_before_id=portfolio_before_id,
        economic_goal_id="goal-1",
        risk_policy_id="risk-1",
        terminal_space_proof_id="terminal-proof-1" if candidate and terminal else None,
        settlement_proof_id="settlement-rule-proof-1" if candidate else None,
        execution_model_id="paper-model-1" if candidate else None,
        execution_run_id="paper-run-1" if candidate else None,
        execution_plan_id="paper-plan-1" if candidate else None,
        execution_action_id="paper-action-1" if candidate else None,
        decision_quote_id="quote-1" if candidate else None,
        cost_contract_sha256=H2,
        outcome_reveal_not_before="2026-09-20T00:10:00Z",
        dependence_cluster_keys=("event:event-1", "league:league-1"),
    )


def _ledger(*rows: EvaluationRow) -> EvaluationUniverseLedger:
    snapshot = ObservationIntakeSnapshot(
        authority_id="intake-1",
        session_id="session-1",
        source_id="source-1",
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=H,
        universe_id="universe-1",
        first_cycle=1,
        last_cycle=1,
        record_count=1,
        root_sha256=H2,
        expected_row_keys=tuple(sorted(row.row_key for row in rows)),
        committed_at="2026-09-20T00:00:03Z",
        evaluation_not_before="2026-09-20T00:00:03Z",
    )
    universe = EvaluationUniverse._construct(
        intake_snapshot=snapshot,
        universe_id="universe-1",
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=H,
        frozen_at="2026-09-20T00:05:00Z",
        rows=tuple(rows),
    )
    return EvaluationUniverseLedger(universe)


def _definitions() -> tuple[BaselineDefinition, ...]:
    return tuple(
        BaselineDefinition(
            kind=kind,
            baseline_id=f"baseline:{kind.value}",
            implementation_sha256=_hash("impl:" + kind.value),
            config_sha256=_hash("config:" + kind.value),
            supported=kind is BaselineKind.NO_BET_WAIT,
            unsupported_reason=None if kind is BaselineKind.NO_BET_WAIT else "unsupported fixture baseline",
        )
        for kind in REQUIRED_BASELINE_KINDS
    )


def _protocol(
    ledger: EvaluationUniverseLedger,
    *,
    strategy_class: StrategyClass = StrategyClass.ARBITRAGE,
    family: EvaluationContractFamily = EvaluationContractFamily.ARBITRAGE_EXECUTION,
    keys: tuple[str, ...] | None = None,
    candidate_id: str = "strategy-1",
) -> FrozenBaselineProtocol:
    contract = canonical_evaluation_contract(family)
    scope = FrozenEvidenceScope(
        dataset_sha256=H,
        dataset_cutoff="2026-09-20T00:05:00Z",
        cohort_keys=keys or ledger.cohort().row_ids,
        market_evidence_sha256=H2,
        outcome_evidence_sha256=H3,
        cost_model_sha256=H4,
        execution_model_sha256=_hash("paper-model"),
    )
    return FrozenBaselineProtocol(
        protocol_id="extval:strategy-1:v1",
        frozen_at="2026-09-20T00:06:00Z",
        evidence_scope=scope,
        candidate_id=candidate_id,
        candidate_artifact_sha256=_hash(candidate_id + "-artifact"),
        strategy_class=strategy_class,
        evaluation_contract_family=family,
        evaluation_semantics=contract["evaluation_semantics"],
        evaluation_contract_sha256=contract["evaluation_contract_sha256"],
        primary_metric=contract["primary_metric"],
        uncertainty_method=contract["uncertainty_method"],
        baselines=_definitions(),
    )


def _results(protocol: FrozenBaselineProtocol):
    n = protocol.evidence_scope.sample_count
    candidate = PolicyEvaluation(
        policy_id=protocol.candidate_id,
        policy_artifact_sha256=protocol.candidate_artifact_sha256,
        protocol_sha256=protocol.identity_sha256,
        evidence_scope_sha256=protocol.evidence_scope.identity_sha256,
        cohort_sha256=protocol.evidence_scope.cohort_sha256,
        primary_metric=protocol.primary_metric,
        evaluated_at="2026-09-20T00:07:00Z",
        metric_value="0.02",
        uncertainty_low="0",
        uncertainty_high="0.04",
        observed_count=n,
        scored_count=1,
        abstention_count=n - 1,
        total_cost="0.01",
        evaluation_bundle_sha256=_hash("candidate-bundle"),
    )
    definition = next(x for x in protocol.baselines if x.kind is BaselineKind.NO_BET_WAIT)
    baseline = PolicyEvaluation(
        policy_id=definition.baseline_id,
        policy_artifact_sha256=definition.implementation_sha256,
        protocol_sha256=protocol.identity_sha256,
        evidence_scope_sha256=protocol.evidence_scope.identity_sha256,
        cohort_sha256=protocol.evidence_scope.cohort_sha256,
        primary_metric=protocol.primary_metric,
        evaluated_at="2026-09-20T00:07:00Z",
        metric_value="0",
        uncertainty_low="0",
        uncertainty_high="0",
        observed_count=n,
        scored_count=0,
        abstention_count=n,
        total_cost="0",
        evaluation_bundle_sha256=_hash("baseline-bundle"),
        baseline_definition_sha256=definition.definition_sha256,
    )
    return candidate, (baseline,)


def test_complete_denominator_is_preserved_and_truth_is_fail_closed():
    ledger = _ledger(
        _row("candidate"),
        _row("no-event", slot=SlotState.NO_EVENT),
        _row("outage", slot=SlotState.SOURCE_OUTAGE),
        _row("wait", slot=SlotState.WAIT_ZERO),
    )
    protocol = _protocol(ledger)
    candidate, baselines = _results(protocol)
    report = evaluate_strategy_external_validity(ledger, protocol, candidate, baselines)
    payload = report.to_payload()

    assert report.evaluated_row_count == 4
    assert report.model_version_ids == ("model-1",)
    assert dict(report.stage_counts)[FunnelStage.OBSERVED_SLOT.value] == 3
    assert dict(report.attrition_counts)[AttritionReason.NO_EVENT.value] == 1
    assert dict(report.attrition_counts)[AttritionReason.SOURCE_OUTAGE.value] == 1
    assert report.evidence_grade is StrategyEvidenceGrade.THEORETICAL
    assert payload["truth"]["product_issued_policy_evaluation_verified"] is False
    assert payload["truth"]["promotion_authority"] is False
    assert payload["truth"]["real_money_execution"] is False


def test_subset_cannot_replace_frozen_denominator():
    ledger = _ledger(_row("a"), _row("b", slot=SlotState.WAIT_ZERO))
    protocol = _protocol(ledger, keys=(ledger.universe.rows[0].row_id,))
    candidate, baselines = _results(protocol)
    with pytest.raises(StrategyExternalValidityError, match="exact frozen EvaluationUniverse"):
        evaluate_strategy_external_validity(ledger, protocol, candidate, baselines)


def test_attempted_execution_without_outcome_is_materially_unresolved():
    ledger = _ledger(_row("candidate"))
    row = ledger.universe.rows[0]
    ledger = ledger.append(
        FunnelEvent(
            row_id=row.row_id,
            stage=FunnelStage.ATTEMPTED,
            event_at="2026-09-20T00:06:00Z",
            execution_model_id=row.execution_model_id,
            execution_attempt_id="paper-attempt-1",
        )
    )
    protocol = _protocol(ledger)
    candidate, baselines = _results(protocol)

    report = evaluate_strategy_external_validity(ledger, protocol, candidate, baselines)

    assert report.evidence_grade is StrategyEvidenceGrade.INSUFFICIENT
    assert "material_funnel_state_unresolved" in report.external_evidence_gaps


def test_generic_paper_outcome_stage_cannot_mint_paper_model(monkeypatch):
    ledger = _ledger(_row("candidate"))
    monkeypatch.setattr(
        EvaluationUniverseLedger,
        "current_stage",
        lambda self, row_id: FunnelStage.ACCEPTED,
    )
    protocol = _protocol(ledger)
    candidate, baselines = _results(protocol)

    report = evaluate_strategy_external_validity(ledger, protocol, candidate, baselines)

    assert report.evidence_grade is StrategyEvidenceGrade.THEORETICAL
    assert "strategy_specific_paper_proof_not_verified" in report.external_evidence_gaps


def test_live_paper_stage_does_not_replace_later_quote_proof(monkeypatch):
    ledger = _ledger(_row("candidate"))
    monkeypatch.setattr(
        EvaluationUniverseLedger,
        "current_stage",
        lambda self, row_id: FunnelStage.ACCEPTED,
    )
    protocol = _protocol(
        ledger,
        strategy_class=StrategyClass.LIVE_PRICE_MOVEMENT,
        family=EvaluationContractFamily.LIVE_PRICE_EXECUTION,
    )
    candidate, baselines = _results(protocol)

    report = evaluate_strategy_external_validity(ledger, protocol, candidate, baselines)

    assert report.evidence_grade is StrategyEvidenceGrade.THEORETICAL
    assert "strategy_specific_paper_proof_not_verified" in report.external_evidence_gaps
    assert (
        "external_later_quote_execution_evidence_not_verified"
        in report.external_evidence_gaps
    )


def test_hedge_paper_stage_does_not_replace_same_trajectory_comparator(monkeypatch):
    ledger = _ledger(
        _row("candidate-a", portfolio_before_id="portfolio-a"),
        _row("candidate-b", portfolio_before_id="portfolio-b"),
    )
    monkeypatch.setattr(
        EvaluationUniverseLedger,
        "current_stage",
        lambda self, row_id: FunnelStage.ACCEPTED,
    )
    protocol = _protocol(
        ledger,
        strategy_class=StrategyClass.HEDGE_REBALANCE,
        family=EvaluationContractFamily.HEDGE_PORTFOLIO_RISK,
    )
    candidate, baselines = _results(protocol)

    report = evaluate_strategy_external_validity(ledger, protocol, candidate, baselines)

    assert report.portfolio_before_ids == ("portfolio-a", "portfolio-b")
    assert report.evidence_grade is StrategyEvidenceGrade.THEORETICAL
    assert "strategy_specific_paper_proof_not_verified" in report.external_evidence_gaps
    assert (
        "external_same_trajectory_counterfactual_execution_not_verified"
        in report.external_evidence_gaps
    )


@pytest.mark.parametrize(
    ("strategy_class", "family"),
    (
        (StrategyClass.ARBITRAGE, EvaluationContractFamily.ARBITRAGE_EXECUTION),
        (StrategyClass.DUTCHING, EvaluationContractFamily.DUTCHING_EXECUTION),
    ),
)
def test_multileg_paper_stage_does_not_replace_complete_economic_proof(
    monkeypatch,
    strategy_class,
    family,
):
    ledger = _ledger(_row("candidate", terminal=True))
    monkeypatch.setattr(
        EvaluationUniverseLedger,
        "current_stage",
        lambda self, row_id: FunnelStage.ACCEPTED,
    )
    protocol = _protocol(
        ledger,
        strategy_class=strategy_class,
        family=family,
    )
    candidate, baselines = _results(protocol)

    report = evaluate_strategy_external_validity(ledger, protocol, candidate, baselines)

    assert report.evidence_grade is StrategyEvidenceGrade.THEORETICAL
    assert "strategy_specific_paper_proof_not_verified" in report.external_evidence_gaps
    assert (
        "external_multi_leg_acceptance_and_settlement_not_verified"
        in report.external_evidence_gaps
    )


def test_arbitrage_without_terminal_space_authority_is_insufficient():
    ledger = _ledger(_row("candidate", terminal=False))
    protocol = _protocol(ledger)
    candidate, baselines = _results(protocol)
    report = evaluate_strategy_external_validity(ledger, protocol, candidate, baselines)

    assert report.evidence_grade is StrategyEvidenceGrade.INSUFFICIENT
    assert "terminal_space_authority_incomplete" in report.external_evidence_gaps


def test_protocol_candidate_cannot_rebind_frozen_strategy_identity():
    ledger = _ledger(_row("candidate", strategy="strategy-1"))
    protocol = _protocol(ledger, candidate_id="strategy-2")
    candidate, baselines = _results(protocol)
    with pytest.raises(StrategyExternalValidityError, match="candidate_id does not match"):
        evaluate_strategy_external_validity(ledger, protocol, candidate, baselines)


def test_predictive_metric_family_cannot_be_reused_for_execution_validity():
    ledger = _ledger(_row("candidate"))
    protocol = _protocol(
        ledger,
        strategy_class=StrategyClass.PREDICTIVE_EDGE,
        family=EvaluationContractFamily.PREDICTIVE_FORECAST_VALUE,
    )
    candidate, baselines = _results(protocol)
    with pytest.raises(StrategyExternalValidityError, match="supports LIVE_PRICE_MOVEMENT"):
        evaluate_strategy_external_validity(ledger, protocol, candidate, baselines)

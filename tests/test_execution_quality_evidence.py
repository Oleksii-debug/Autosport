from __future__ import annotations

from decimal import Decimal, localcontext

import pytest

from autosport._evaluation_universe_structural_gate import (
    _authorize_structural_intake_for_tests,
)
from autosport.evaluation_intake import (
    ObservationEnumerationWitness,
    ObservationIntakeLedger,
)
from autosport.evaluation_universe import (
    AttritionReason,
    CanonicalPaperExecutionResolver,
    EvaluationRow,
    EvaluationUniverseLedger,
    FunnelEvent,
    FunnelStage,
    SlotState,
    build_frozen_universe,
)
from autosport.execution_quality_evidence import (
    ExecutionQualityEvidenceClass,
    ExecutionQualityEvidenceError,
    PaperExecutionQualityReport,
    PaperExecutionQualitySample,
    build_paper_execution_quality_report,
    validate_paper_execution_quality_report,
    validate_paper_execution_quality_sample,
)
from autosport.paper_execution_reality import (
    EvidenceGrade,
    PaperAttemptOutcome,
    PaperExecutionLedger,
    PaperLegAttempt,
)


H = "a" * 64
H2 = "b" * 64
H3 = "c" * 64


class _Resolver:
    def __init__(self, witnesses: tuple[ObservationEnumerationWitness, ...]) -> None:
        self._witnesses = {item.enumeration_id: item for item in witnesses}

    def resolve_enumeration(self, enumeration_id: str) -> ObservationEnumerationWitness:
        return self._witnesses[enumeration_id]

    def terminal_enumeration_id(self, **identity: str) -> str:
        del identity
        return max(
            self._witnesses.values(),
            key=lambda item: item.cycle_index,
        ).enumeration_id


def _row(
    key: str,
    *,
    slot_state: SlotState = SlotState.CANDIDATE,
    side_identity: str | None = None,
) -> EvaluationRow:
    candidate = slot_state is SlotState.CANDIDATE
    event_id = None if slot_state is SlotState.NO_EVENT else f"event-{key}"
    reason = None
    if not candidate:
        reason = {
            SlotState.NO_EVENT: AttritionReason.NO_EVENT,
            SlotState.NO_QUOTE: AttritionReason.NO_QUOTE,
            SlotState.SOURCE_OUTAGE: AttritionReason.SOURCE_OUTAGE,
            SlotState.NO_CANDIDATE: AttritionReason.NO_CANDIDATE,
            SlotState.WAIT_ZERO: AttritionReason.WAIT_ZERO,
        }[slot_state]
    identity = side_identity or key
    return EvaluationRow(
        row_key=key,
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=H,
        universe_id="universe-1",
        slot_state=slot_state,
        decision_stage=(
            FunnelStage.EXECUTION_MODEL_ELIGIBLE
            if candidate
            else FunnelStage.OBSERVED_SLOT
        ),
        attrition_reason=reason,
        sport="football",
        provider_id="paper-provider",
        source_id="source-1",
        event_id=event_id,
        market_id=None if event_id is None else f"market-{key}",
        selection_id=None if event_id is None else f"selection-{key}",
        source_at="2026-09-20T00:00:00Z",
        received_at="2026-09-20T00:00:01Z",
        committed_at="2026-09-20T00:00:02Z",
        detection_at="2026-09-20T00:00:03Z" if candidate else None,
        decision_at="2026-09-20T00:00:04Z" if candidate else None,
        quote_set_sha256=H2 if candidate else None,
        freshness_policy_sha256=H3,
        strategy_version_id="strategy-1",
        model_version_id="model-1",
        config_sha256=H,
        portfolio_before_id="portfolio-1",
        economic_goal_id="goal-1",
        risk_policy_id="risk-1",
        terminal_space_proof_id="terminal-proof-1",
        settlement_proof_id="settlement-rule-proof-1",
        execution_model_id=f"paper-model-{identity}" if candidate else None,
        execution_run_id=f"paper-run-{identity}" if candidate else None,
        execution_plan_id=f"paper-plan-{identity}" if candidate else None,
        execution_action_id=f"paper-action-{identity}" if candidate else None,
        decision_quote_id=f"quote-{identity}" if candidate else None,
        cost_contract_sha256=H2,
        outcome_reveal_not_before="2026-09-20T00:10:00Z",
        dependence_cluster_keys=(f"event:{event_id or key}",),
    )


def _witness(index: int, row: EvaluationRow) -> ObservationEnumerationWitness:
    return ObservationEnumerationWitness(
        enumeration_id=f"enumeration-{index}",
        session_id="session-1",
        source_id="source-1",
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=H,
        universe_id="universe-1",
        cycle_index=index,
        source_range_id=f"range-{index}",
        stream_epoch="epoch-1",
        start_cursor=f"cursor-{index - 1}",
        end_cursor=f"cursor-{index}",
        acquisition_sha256=H3,
        row_keys=(row.row_key,),
        row_evidence_sha256=((row.row_key, row.row_id),),
        exhaustive=True,
        gap_free=True,
        committed_at="2026-09-20T00:00:02.500000Z",
        evaluation_not_before="2026-09-20T00:00:03Z",
        outcome_reveal_not_before=row.outcome_reveal_not_before,
    )


def _universe(tmp_path, rows: tuple[EvaluationRow, ...]):
    witnesses = tuple(
        _witness(index, row)
        for index, row in enumerate(
            sorted(rows, key=lambda item: item.row_key),
            1,
        )
    )
    intake = ObservationIntakeLedger(
        tmp_path / "intake",
        authority_id="intake-1",
        enumeration_resolver=_Resolver(witnesses),
    )
    for witness in witnesses:
        intake.append_cycle(enumeration_id=witness.enumeration_id)
    _authorize_structural_intake_for_tests(intake)
    return build_frozen_universe(
        intake_ledger=intake,
        universe_id="universe-1",
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=H,
        frozen_at="2026-09-20T00:05:00Z",
        rows=rows,
    )


def _attempt(
    row: EvaluationRow,
    *,
    outcome: PaperAttemptOutcome,
    side: str = "BACK",
    execution_odds: Decimal | None = None,
    execution_stake: Decimal | None = None,
) -> PaperLegAttempt:
    accepted = outcome in {
        PaperAttemptOutcome.ACCEPTED,
        PaperAttemptOutcome.PARTIAL,
    }
    if accepted and execution_odds is None:
        execution_odds = Decimal("2.1")
    if accepted and execution_stake is None:
        execution_stake = (
            Decimal("10")
            if outcome is PaperAttemptOutcome.ACCEPTED
            else Decimal("4")
        )
    return PaperLegAttempt(
        attempt_id=f"attempt-{row.row_key}",
        run_id=row.execution_run_id,
        plan_id=row.execution_plan_id,
        action_id=row.execution_action_id,
        sequence=0,
        bookmaker_id=row.provider_id,
        account_id="paper-account-1",
        event_id=row.event_id,
        market_id=row.market_id,
        selection_id=row.selection_id,
        side=side,
        decision_quote_id=row.decision_quote_id,
        decision_odds=Decimal("2.0"),
        requested_stake=Decimal("10"),
        decision_observed_at="2026-09-20T00:00:00Z",
        execution_observed_at="2026-09-20T00:05:05Z",
        delay_ms=125,
        quote_age_ms=305000,
        outcome=outcome,
        execution_odds=execution_odds,
        execution_stake=execution_stake,
        suspended=False,
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="deterministic-paper-model",
        evidence_id=None,
        evidence_sha256=None,
        model_fingerprint=row.execution_model_id,
        reason="quality projection regression",
    )


def _ledger_with_outcome(tmp_path, frozen, row, attempt):
    paper = PaperExecutionLedger(tmp_path / "paper-execution.json")
    paper.record_attempt(attempt)
    reality = next(
        event["event_sha256"]
        for event in paper.events()
        if event["event_type"] == "ATTEMPT_RECORDED"
        and event["payload"]["attempt_id"] == attempt.attempt_id
    )
    resolver = CanonicalPaperExecutionResolver(paper)
    ledger = EvaluationUniverseLedger(frozen, paper_resolver=resolver)
    ledger = ledger.append(
        FunnelEvent(
            row_id=row.row_id,
            stage=FunnelStage.ATTEMPTED,
            event_at="2026-09-20T00:05:01Z",
            execution_model_id=row.execution_model_id,
            execution_attempt_id=attempt.attempt_id,
        )
    )
    stage = {
        PaperAttemptOutcome.ACCEPTED: FunnelStage.ACCEPTED,
        PaperAttemptOutcome.PARTIAL: FunnelStage.PARTIAL,
        PaperAttemptOutcome.REJECTED: FunnelStage.REJECTED,
        PaperAttemptOutcome.UNKNOWN: FunnelStage.UNKNOWN,
    }[attempt.outcome]
    ledger = ledger.append(
        FunnelEvent(
            row_id=row.row_id,
            stage=stage,
            event_at="2026-09-20T00:05:06Z",
            execution_attempt_id=attempt.attempt_id,
            execution_reality_sha256=reality,
        )
    )
    return ledger, reality


def test_report_keeps_complete_frozen_denominator_and_labels_model_timing(tmp_path):
    candidate = _row("candidate")
    outage = _row("outage", slot_state=SlotState.SOURCE_OUTAGE)
    frozen = _universe(tmp_path, (candidate, outage))
    ledger, reality = _ledger_with_outcome(
        tmp_path,
        frozen,
        candidate,
        _attempt(candidate, outcome=PaperAttemptOutcome.ACCEPTED),
    )

    report = build_paper_execution_quality_report(ledger)
    sample = report.samples[0]
    payload = sample.to_payload()

    assert report.denominator_count == 2
    assert report.rows_without_execution_outcome_count == 1
    assert dict(report.stage_counts)[FunnelStage.ACCEPTED.value] == 1
    assert dict(report.stage_counts)[FunnelStage.OBSERVED_SLOT.value] == 1
    assert dict(report.outcome_counts)[PaperAttemptOutcome.ACCEPTED.value] == 1

    assert sample.canonical_reality_sha256 == reality
    assert sample.evidence_class is ExecutionQualityEvidenceClass.PAPER_EXECUTION_MODEL
    assert sample.signed_price_delta == Decimal("0.1")
    assert sample.adverse_slippage == Decimal("0")
    assert sample.completion_ratio == Decimal("1")
    assert sample.paper_model_delay_ms == 125
    assert sample.paper_model_quote_age_ms == 305000
    assert payload["local_decision_to_submit_ms"] is None
    assert payload["local_submit_to_response_ms"] is None
    assert payload["provider_processing_latency_ms"] is None

    # Reprojection is deterministic and cannot hand-pick away the outage row.
    repeated = build_paper_execution_quality_report(ledger)
    assert repeated.report_sha256 == report.report_sha256
    assert repeated.denominator_row_ids == frozen.row_ids


@pytest.mark.parametrize(
    ("outcome", "execution_odds", "execution_stake", "expected_ratio", "expected_signed", "expected_adverse"),
    [
        (
            PaperAttemptOutcome.PARTIAL,
            Decimal("1.9"),
            Decimal("4"),
            Decimal("0.4"),
            Decimal("-0.1"),
            Decimal("0.1"),
        ),
        (
            PaperAttemptOutcome.REJECTED,
            None,
            None,
            Decimal("0"),
            None,
            None,
        ),
        (
            PaperAttemptOutcome.UNKNOWN,
            None,
            None,
            None,
            None,
            None,
        ),
    ],
)
def test_partial_rejected_and_unknown_keep_price_and_completion_semantics(
    tmp_path,
    outcome,
    execution_odds,
    execution_stake,
    expected_ratio,
    expected_signed,
    expected_adverse,
):
    row = _row(outcome.value.lower())
    frozen = _universe(tmp_path, (row,))
    attempt = _attempt(
        row,
        outcome=outcome,
        execution_odds=execution_odds,
        execution_stake=execution_stake,
    )
    ledger, _ = _ledger_with_outcome(tmp_path, frozen, row, attempt)

    sample = build_paper_execution_quality_report(ledger).samples[0]

    assert sample.completion_ratio == expected_ratio
    assert sample.signed_price_delta == expected_signed
    assert sample.adverse_slippage == expected_adverse


def test_lay_does_not_inherit_back_price_quality_formula(tmp_path):
    row = _row("lay")
    frozen = _universe(tmp_path, (row,))
    ledger, _ = _ledger_with_outcome(
        tmp_path,
        frozen,
        row,
        _attempt(
            row,
            outcome=PaperAttemptOutcome.ACCEPTED,
            side="LAY",
            execution_odds=Decimal("1.9"),
            execution_stake=Decimal("10"),
        ),
    )

    sample = build_paper_execution_quality_report(ledger).samples[0]

    assert sample.completion_ratio == Decimal("1")
    assert sample.signed_price_delta is None
    assert sample.adverse_slippage is None


class _ForgedEvaluationUniverseLedger(EvaluationUniverseLedger):
    pass


def test_report_rejects_ledger_subclass_before_authority_reads(tmp_path):
    row = _row("exact-type")
    frozen = _universe(tmp_path, (row,))
    forged = _ForgedEvaluationUniverseLedger(frozen)

    with pytest.raises(TypeError, match="exact EvaluationUniverseLedger"):
        build_paper_execution_quality_report(forged)


def test_positive_quality_evidence_cannot_be_caller_constructed():
    with pytest.raises(TypeError, match="canonical quality projector"):
        PaperExecutionQualitySample()

    with pytest.raises(TypeError, match="canonical frozen ledger"):
        PaperExecutionQualityReport()

def test_only_canonical_projector_issues_quality_evidence(tmp_path):
    row = _row("issuance")
    frozen = _universe(tmp_path, (row,))
    ledger, _ = _ledger_with_outcome(
        tmp_path,
        frozen,
        row,
        _attempt(row, outcome=PaperAttemptOutcome.ACCEPTED),
    )

    report = build_paper_execution_quality_report(ledger)
    sample = report.samples[0]

    assert validate_paper_execution_quality_sample(sample) is sample
    assert validate_paper_execution_quality_report(report) is report

    sample_values = {
        name: getattr(sample, name)
        for name in PaperExecutionQualitySample.__dataclass_fields__
    }
    sample_values["execution_odds"] = Decimal("999")
    sample_values["signed_price_delta"] = Decimal("997")
    sample_values["adverse_slippage"] = Decimal("0")
    forged_sample = PaperExecutionQualitySample._construct(**sample_values)

    assert forged_sample.sample_sha256 != sample.sample_sha256
    with pytest.raises(
        ExecutionQualityEvidenceError,
        match="product-issued from canonical evidence",
    ):
        validate_paper_execution_quality_sample(forged_sample)

    report_values = {
        name: getattr(report, name)
        for name in PaperExecutionQualityReport.__dataclass_fields__
    }
    report_values["samples"] = (forged_sample,)
    forged_report = PaperExecutionQualityReport._construct(**report_values)

    with pytest.raises(
        ExecutionQualityEvidenceError,
        match="product-issued from canonical evidence",
    ):
        validate_paper_execution_quality_report(forged_report)


def test_post_issuance_quality_mutation_invalidates_issuance(tmp_path):
    row = _row("mutation")
    frozen = _universe(tmp_path, (row,))
    ledger, _ = _ledger_with_outcome(
        tmp_path,
        frozen,
        row,
        _attempt(row, outcome=PaperAttemptOutcome.ACCEPTED),
    )

    report = build_paper_execution_quality_report(ledger)
    sample = report.samples[0]
    assert validate_paper_execution_quality_sample(sample) is sample

    object.__setattr__(sample, "execution_odds", Decimal("999"))

    with pytest.raises(
        ExecutionQualityEvidenceError,
        match="product-issued from canonical evidence",
    ):
        validate_paper_execution_quality_sample(sample)
    with pytest.raises(ExecutionQualityEvidenceError):
        validate_paper_execution_quality_report(report)


def test_quality_decimal_arithmetic_and_identity_ignore_ambient_context(tmp_path):
    row = _row("decimal-context")
    frozen = _universe(tmp_path, (row,))
    ledger, _ = _ledger_with_outcome(
        tmp_path,
        frozen,
        row,
        _attempt(
            row,
            outcome=PaperAttemptOutcome.ACCEPTED,
            execution_odds=Decimal("2.000000000000000000000000000123456789"),
            execution_stake=Decimal("10"),
        ),
    )

    with localcontext() as context:
        context.prec = 6
        low_precision = build_paper_execution_quality_report(ledger)
    with localcontext() as context:
        context.prec = 80
        high_precision = build_paper_execution_quality_report(ledger)

    expected_delta = Decimal("0.000000000000000000000000000123456789")
    assert low_precision.samples[0].signed_price_delta == expected_delta
    assert high_precision.samples[0].signed_price_delta == expected_delta
    assert low_precision.samples[0].sample_sha256 == high_precision.samples[0].sample_sha256
    assert low_precision.report_sha256 == high_precision.report_sha256


def test_distinct_high_precision_execution_odds_do_not_alias_under_low_precision(
    tmp_path,
):
    def project(path, execution_odds):
        path.mkdir()
        row = _row("decimal-distinct")
        frozen = _universe(path, (row,))
        ledger, _ = _ledger_with_outcome(
            path,
            frozen,
            row,
            _attempt(
                row,
                outcome=PaperAttemptOutcome.ACCEPTED,
                execution_odds=execution_odds,
                execution_stake=Decimal("10"),
            ),
        )
        with localcontext() as context:
            context.prec = 6
            return build_paper_execution_quality_report(ledger).samples[0]

    first = project(
        tmp_path / "first",
        Decimal("2.000000000000000000000000000123456781"),
    )
    second = project(
        tmp_path / "second",
        Decimal("2.000000000000000000000000000123456782"),
    )

    assert first.execution_odds != second.execution_odds
    assert first.signed_price_delta != second.signed_price_delta
    assert first.sample_sha256 != second.sample_sha256

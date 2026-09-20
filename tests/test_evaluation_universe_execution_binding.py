from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.evaluation_intake import (
    EvaluationIntakeError,
    EvaluationIntakeIntegrityError,
    ObservationIntakeLedger,
)
from autosport.evaluation_universe import (
    AttritionReason,
    CanonicalPaperExecutionResolver,
    EvaluationRow,
    EvaluationUniverseError,
    EvaluationUniverseIntegrityError,
    EvaluationUniverseLedger,
    FunnelEvent,
    FunnelStage,
    SlotState,
    build_frozen_universe,
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


def _row(
    key: str = "candidate",
    *,
    slot_state: SlotState = SlotState.CANDIDATE,
    run_id: str = "paper-run-1",
    plan_id: str = "paper-plan-1",
    action_id: str = "paper-action-1",
    event_id: str = "event-1",
    market_id: str = "market-1",
    selection_id: str = "selection-1",
    quote_id: str = "quote-1",
) -> EvaluationRow:
    candidate = slot_state is SlotState.CANDIDATE
    reason = None if candidate else {
        SlotState.NO_EVENT: AttritionReason.NO_EVENT,
        SlotState.NO_QUOTE: AttritionReason.NO_QUOTE,
        SlotState.SOURCE_OUTAGE: AttritionReason.SOURCE_OUTAGE,
        SlotState.NO_CANDIDATE: AttritionReason.NO_CANDIDATE,
        SlotState.WAIT_ZERO: AttritionReason.WAIT_ZERO,
    }[slot_state]
    actual_event = None if slot_state is SlotState.NO_EVENT else event_id
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
        provider_id="provider-1",
        source_id="source-1",
        event_id=actual_event,
        market_id=None if actual_event is None else market_id,
        selection_id=None if actual_event is None else selection_id,
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
        execution_model_id="paper-model-fingerprint-1" if candidate else None,
        execution_run_id=run_id if candidate else None,
        execution_plan_id=plan_id if candidate else None,
        execution_action_id=action_id if candidate else None,
        decision_quote_id=quote_id if candidate else None,
        cost_contract_sha256=H2,
        outcome_reveal_not_before="2026-09-20T00:10:00Z",
        dependence_cluster_keys=("event:event-1", "league:league-1"),
    )


def _intake(workspace, rows: tuple[EvaluationRow, ...]) -> ObservationIntakeLedger:
    ledger = ObservationIntakeLedger(workspace, authority_id="intake-1")
    for index, item in enumerate(sorted(rows, key=lambda value: value.row_key), 1):
        ledger.append_cycle(
            session_id="session-1",
            source_id="source-1",
            campaign_id="campaign-1",
            research_protocol_id="protocol-1",
            protocol_sha256=H,
            universe_id="universe-1",
            cycle_index=index,
            committed_at=f"2026-09-20T00:04:{index:02d}Z",
            outcome_reveal_not_before=item.outcome_reveal_not_before,
            row_keys=(item.row_key,),
            source_state="PRE_RESULT_COMMITTED",
        )
    return ledger


def _universe(workspace, rows: tuple[EvaluationRow, ...]):
    intake = _intake(workspace, rows)
    frozen = build_frozen_universe(
        intake_ledger=intake,
        universe_id="universe-1",
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=H,
        frozen_at="2026-09-20T00:05:00Z",
        rows=rows,
    )
    return intake, frozen


def _paper_attempt(
    row: EvaluationRow,
    *,
    attempt_id: str = "attempt-1",
    outcome: PaperAttemptOutcome = PaperAttemptOutcome.ACCEPTED,
    run_id: str | None = None,
    plan_id: str | None = None,
    action_id: str | None = None,
    event_id: str | None = None,
    market_id: str | None = None,
    selection_id: str | None = None,
    quote_id: str | None = None,
    model_fingerprint: str | None = None,
) -> PaperLegAttempt:
    accepted = outcome in {PaperAttemptOutcome.ACCEPTED, PaperAttemptOutcome.PARTIAL}
    execution_stake = Decimal("10") if outcome is PaperAttemptOutcome.ACCEPTED else (
        Decimal("5") if outcome is PaperAttemptOutcome.PARTIAL else None
    )
    return PaperLegAttempt(
        attempt_id=attempt_id,
        run_id=run_id or row.execution_run_id,
        plan_id=plan_id or row.execution_plan_id,
        action_id=action_id or row.execution_action_id,
        sequence=0,
        bookmaker_id="provider-1",
        account_id="paper-account-1",
        event_id=event_id or row.event_id,
        market_id=market_id or row.market_id,
        selection_id=selection_id or row.selection_id,
        side="BACK",
        decision_quote_id=quote_id or row.decision_quote_id,
        decision_odds=Decimal("2.0"),
        requested_stake=Decimal("10"),
        decision_observed_at="2026-09-20T00:00:00Z",
        execution_observed_at="2026-09-20T00:05:05Z",
        delay_ms=100,
        quote_age_ms=305000,
        outcome=outcome,
        execution_odds=Decimal("1.99") if accepted else None,
        execution_stake=execution_stake,
        suspended=False,
        evidence_grade=EvidenceGrade.SYNTHETIC,
        evidence_source="deterministic-test",
        evidence_id=None,
        evidence_sha256=None,
        model_fingerprint=model_fingerprint or row.execution_model_id,
        reason="deterministic test attempt",
    )


def _resolver(workspace, attempt: PaperLegAttempt):
    paper = PaperExecutionLedger(workspace / "paper-execution.json")
    paper.record_attempt(attempt)
    event = next(
        item
        for item in paper.events()
        if item["event_type"] == "ATTEMPT_RECORDED"
    )
    return CanonicalPaperExecutionResolver(paper), event["event_sha256"]


def _attempted(row: EvaluationRow, attempt_id: str = "attempt-1") -> FunnelEvent:
    return FunnelEvent(
        row_id=row.row_id,
        stage=FunnelStage.ATTEMPTED,
        event_at="2026-09-20T00:05:01Z",
        execution_model_id=row.execution_model_id,
        execution_attempt_id=attempt_id,
    )


def _outcome(
    row: EvaluationRow,
    stage: FunnelStage,
    reality_sha256: str,
    attempt_id: str = "attempt-1",
) -> FunnelEvent:
    return FunnelEvent(
        row_id=row.row_id,
        stage=stage,
        event_at="2026-09-20T00:05:06Z",
        execution_attempt_id=attempt_id,
        execution_reality_sha256=reality_sha256,
    )


def test_exact_canonical_paper_attempt_can_enrich_frozen_row(tmp_path):
    row = _row()
    _, frozen = _universe(tmp_path / "intake", (row,))
    resolver, reality = _resolver(tmp_path / "paper", _paper_attempt(row))
    ledger = EvaluationUniverseLedger(frozen, paper_resolver=resolver)
    ledger = ledger.append(_attempted(row)).append(
        _outcome(row, FunnelStage.ACCEPTED, reality)
    ).append(
        FunnelEvent(
            row_id=row.row_id,
            stage=FunnelStage.RECONCILED,
            event_at="2026-09-20T00:06:00Z",
            reason_code="paper-reconciled",
        )
    ).append(
        FunnelEvent(
            row_id=row.row_id,
            stage=FunnelStage.SETTLED,
            event_at="2026-09-20T00:10:00Z",
            settlement_proof_id="settlement-proof-1",
        )
    )

    assert ledger.current_stage(row.row_id) is FunnelStage.SETTLED
    assert ledger.cohort().sample_count == 1


def test_invented_reality_digest_is_rejected(tmp_path):
    row = _row()
    _, frozen = _universe(tmp_path / "intake", (row,))
    resolver, _ = _resolver(tmp_path / "paper", _paper_attempt(row))
    ledger = EvaluationUniverseLedger(frozen, paper_resolver=resolver).append(
        _attempted(row)
    )

    with pytest.raises(EvaluationUniverseIntegrityError, match="canonical PAPER attempt event"):
        ledger.append(_outcome(row, FunnelStage.ACCEPTED, H3))


def test_foreign_paper_attempt_cannot_be_spliced_into_frozen_row(tmp_path):
    row = _row()
    _, frozen = _universe(tmp_path / "intake", (row,))
    foreign = _paper_attempt(
        row,
        attempt_id="attempt-foreign",
        run_id="foreign-run",
        plan_id="foreign-plan",
        action_id="foreign-action",
    )
    resolver, reality = _resolver(tmp_path / "paper", foreign)
    ledger = EvaluationUniverseLedger(frozen, paper_resolver=resolver).append(
        _attempted(row, "attempt-foreign")
    )

    with pytest.raises(EvaluationUniverseIntegrityError, match="does not match frozen row"):
        ledger.append(
            _outcome(
                row,
                FunnelStage.ACCEPTED,
                reality,
                attempt_id="attempt-foreign",
            )
        )


def test_funnel_outcome_must_equal_canonical_paper_outcome(tmp_path):
    row = _row()
    _, frozen = _universe(tmp_path / "intake", (row,))
    resolver, reality = _resolver(
        tmp_path / "paper",
        _paper_attempt(row, outcome=PaperAttemptOutcome.REJECTED),
    )
    ledger = EvaluationUniverseLedger(frozen, paper_resolver=resolver).append(
        _attempted(row)
    )

    with pytest.raises(EvaluationUniverseIntegrityError, match="disagrees"):
        ledger.append(_outcome(row, FunnelStage.ACCEPTED, reality))


def test_execution_model_and_local_attempt_identity_are_fail_closed(tmp_path):
    row = _row()
    _, frozen = _universe(tmp_path / "intake", (row,))
    with pytest.raises(EvaluationUniverseError, match="execution model"):
        EvaluationUniverseLedger(frozen).append(
            FunnelEvent(
                row_id=row.row_id,
                stage=FunnelStage.ATTEMPTED,
                event_at="2026-09-20T00:05:01Z",
                execution_model_id="other-model",
                execution_attempt_id="attempt-1",
            )
        )

    resolver, reality = _resolver(tmp_path / "paper", _paper_attempt(row))
    ledger = EvaluationUniverseLedger(frozen, paper_resolver=resolver).append(
        _attempted(row)
    )
    with pytest.raises(EvaluationUniverseError, match="exact attempted execution identity"):
        ledger.append(
            _outcome(row, FunnelStage.ACCEPTED, reality, attempt_id="attempt-2")
        )


def test_paper_outcome_cannot_precede_canonical_execution_observation(tmp_path):
    row = _row()
    _, frozen = _universe(tmp_path / "intake", (row,))
    resolver, reality = _resolver(tmp_path / "paper", _paper_attempt(row))
    ledger = EvaluationUniverseLedger(frozen, paper_resolver=resolver).append(
        _attempted(row)
    )
    early = FunnelEvent(
        row_id=row.row_id,
        stage=FunnelStage.ACCEPTED,
        event_at="2026-09-20T00:05:04Z",
        execution_attempt_id="attempt-1",
        execution_reality_sha256=reality,
    )
    with pytest.raises(EvaluationUniverseIntegrityError, match="predates canonical PAPER"):
        ledger.append(early)


def test_canonical_intake_rejects_rows_when_both_caller_manifest_and_rows_would_omit_slot(tmp_path):
    candidate = _row("candidate")
    no_quote = _row("no-quote", slot_state=SlotState.NO_QUOTE)
    intake = _intake(tmp_path, (candidate, no_quote))

    with pytest.raises(EvaluationUniverseError, match="canonical pre-result intake membership"):
        build_frozen_universe(
            intake_ledger=intake,
            universe_id="universe-1",
            campaign_id="campaign-1",
            research_protocol_id="protocol-1",
            protocol_sha256=H,
            frozen_at="2026-09-20T00:05:00Z",
            rows=(candidate,),
        )


def test_intake_snapshot_cannot_cherry_pick_prefix_before_durable_tip(tmp_path):
    candidate = _row("candidate")
    no_quote = _row("no-quote", slot_state=SlotState.NO_QUOTE)
    intake = _intake(tmp_path, (candidate, no_quote))

    with pytest.raises(EvaluationIntakeError, match="complete durable intake tip"):
        intake.snapshot(first_cycle=1, last_cycle=1)


def test_intake_cycle_retry_is_idempotent_but_conflict_fails(tmp_path):
    item = _row()
    intake = ObservationIntakeLedger(tmp_path, authority_id="intake-1")
    kwargs = dict(
        session_id="session-1",
        source_id="source-1",
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=H,
        universe_id="universe-1",
        cycle_index=1,
        committed_at="2026-09-20T00:04:01Z",
        outcome_reveal_not_before=item.outcome_reveal_not_before,
        row_keys=(item.row_key,),
        source_state="PRE_RESULT_COMMITTED",
    )
    first = intake.append_cycle(**kwargs)
    second = intake.append_cycle(**kwargs)
    assert first.record_sha256 == second.record_sha256

    with pytest.raises(EvaluationIntakeIntegrityError, match="retry conflicts"):
        intake.append_cycle(**{**kwargs, "source_state": "CHANGED"})

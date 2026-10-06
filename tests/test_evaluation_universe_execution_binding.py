from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

import autosport.provider_observation_authority as provider_module
from autosport._evaluation_universe_structural_gate import (
    _authorize_structural_intake_for_tests,
)
from autosport.evaluation_intake import (
    EvaluationIntakeError,
    EvaluationIntakeIntegrityError,
    ObservationEnumerationWitness,
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
from autosport.event_lifecycle import (
    CatalogEvent,
    CatalogPage,
    ContinuousEventLifecycle,
    EventPhase,
)
from autosport.provider_evaluation_universe import (
    build_frozen_universe_from_complete_game_board,
    complete_game_board_member_specs,
)
from autosport.provider_observation_authority import (
    CompleteGameBoardRequest,
    capture_parlay_complete_game_board,
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


PROVIDER_CAPTURED_AT = "2026-09-20T00:00:01Z"
PROVIDER_EVALUATION_AT = "2026-09-20T00:00:03Z"
PROVIDER_FROZEN_AT = "2026-09-20T00:05:00Z"
PROVIDER_REVEAL_AT = "2026-09-20T00:10:00Z"


def _provider_request() -> CompleteGameBoardRequest:
    return CompleteGameBoardRequest(
        sport_key="football",
        bookmakers=("bovada",),
        max_age_s=600,
    )


def _provider_frame() -> dict[str, object]:
    return {
        "type": "initial_state",
        "sport_key": "football",
        "snapshot_scope": "current_game_board",
        "snapshot_complete": True,
        "truncated": False,
        "resume_mode": "replace",
        "partial": False,
        "missing_books": [],
        "truncated_books": [],
        "snapshot_partial_reasons": [],
        "count": 1,
        "timestamp": 1789862401,
        "data": [
            {
                "event_id": "event-1",
                "commence_time_reported": True,
                "commence_time": PROVIDER_REVEAL_AT,
                "bookmaker": "bovada",
                "kind": "game",
                "market_key": "h2h",
                "home_ml": -110,
                "away_ml": 105,
                "last_update": "2026-09-20T00:00:00Z",
            }
        ],
    }


def _capture_provider_board():
    patcher = pytest.MonkeyPatch()
    patcher.setattr(
        provider_module,
        "_read_production_initial_state",
        lambda request_scope, *, api_key, timeout_seconds: _provider_frame(),
    )
    patcher.setattr(provider_module, "_default_clock", lambda: PROVIDER_CAPTURED_AT)
    try:
        return capture_parlay_complete_game_board(
            api_key="test-key",
            request=_provider_request(),
            timeout_seconds=1.0,
        )
    finally:
        patcher.undo()


def _provider_lifecycle(workspace) -> ContinuousEventLifecycle:
    workspace.mkdir(parents=True, exist_ok=True)
    request = _provider_request()
    lifecycle = ContinuousEventLifecycle(workspace / "event-lifecycle.json")
    lifecycle.apply_page(
        CatalogPage(
            source_id=request.source_id,
            stream_epoch="epoch-1",
            cursor="cursor-1",
            position=0,
            events=(
                CatalogEvent(
                    source_id=request.source_id,
                    sport=request.sport_key,
                    event_id="event-1",
                    phase=EventPhase.PRE_MATCH,
                    available_at="2026-09-20T00:00:00Z",
                    scheduled_start_at=PROVIDER_REVEAL_AT,
                ),
            ),
        ),
        discovered_at=PROVIDER_CAPTURED_AT,
    )
    return lifecycle


def _providerize_row(template: EvaluationRow, snapshot, member) -> EvaluationRow:
    return replace(
        template,
        row_key=member.row_key,
        sport=snapshot.request.sport_key,
        provider_id="parlayapi",
        source_id=snapshot.request.source_id,
        event_id=member.event_id,
        market_id=member.market_id,
        selection_id=member.selection_id,
        source_at=member.source_at,
        received_at=PROVIDER_CAPTURED_AT,
        committed_at="2026-09-20T00:00:02Z",
        detection_at=PROVIDER_EVALUATION_AT if template.detection_at is not None else None,
        decision_at="2026-09-20T00:00:04Z" if template.decision_at is not None else None,
        execution_run_id=(
            f"{template.execution_run_id}:{member.selection_id}"
            if template.execution_run_id is not None
            else None
        ),
        execution_plan_id=(
            f"{template.execution_plan_id}:{member.selection_id}"
            if template.execution_plan_id is not None
            else None
        ),
        execution_action_id=(
            f"{template.execution_action_id}:{member.selection_id}"
            if template.execution_action_id is not None
            else None
        ),
        decision_quote_id=(
            f"{template.decision_quote_id}:{member.selection_id}"
            if template.decision_quote_id is not None
            else None
        ),
        outcome_reveal_not_before=member.outcome_reveal_not_before,
    )


class _Resolver:
    def __init__(self, witnesses: tuple[ObservationEnumerationWitness, ...]) -> None:
        self._witnesses = {item.enumeration_id: item for item in witnesses}

    def resolve_enumeration(self, enumeration_id: str) -> ObservationEnumerationWitness:
        return self._witnesses[enumeration_id]

    def terminal_enumeration_id(self, **identity: str) -> str:
        del identity
        return max(self._witnesses.values(), key=lambda item: item.cycle_index).enumeration_id


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
    witnesses = tuple(
        _witness(index, item)
        for index, item in enumerate(sorted(rows, key=lambda value: value.row_key), 1)
    )
    ledger = ObservationIntakeLedger(
        workspace,
        authority_id="intake-1",
        enumeration_resolver=_Resolver(witnesses),
    )
    for witness in witnesses:
        ledger.append_cycle(enumeration_id=witness.enumeration_id)
    return ledger


def _universe(workspace, rows: tuple[EvaluationRow, ...]):
    if len(rows) != 1:
        raise ValueError("provider execution-binding fixture expects one template row")
    snapshot = _capture_provider_board()
    lifecycle = _provider_lifecycle(workspace)
    members = complete_game_board_member_specs(snapshot, event_lifecycle=lifecycle)
    provider_rows = tuple(_providerize_row(rows[0], snapshot, member) for member in members)
    frozen = build_frozen_universe_from_complete_game_board(
        snapshot=snapshot,
        event_lifecycle=lifecycle,
        authority_id="provider-intake-1",
        session_id="session-1",
        universe_id="universe-1",
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=H,
        evaluation_not_before=PROVIDER_EVALUATION_AT,
        frozen_at=PROVIDER_FROZEN_AT,
        rows=provider_rows,
    )
    return provider_rows[0], frozen


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
        bookmaker_id=row.provider_id,
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


class _ForgedCanonicalPaperExecutionResolver(CanonicalPaperExecutionResolver):
    def resolve(self, **kwargs):
        raise AssertionError("forged resolver must never become PAPER authority")


class _ForgedPaperExecutionLedger(PaperExecutionLedger):
    def events(self):
        return ()


def _resolver(workspace, attempt: PaperLegAttempt):
    paper = PaperExecutionLedger(workspace / "paper-execution.json")
    paper.record_attempt(attempt)
    event = next(
        item
        for item in paper.events()
        if item["event_type"] == "ATTEMPT_RECORDED"
    )
    return CanonicalPaperExecutionResolver(paper), event["event_sha256"]


def test_paper_resolver_subclass_is_rejected_before_authority_use(tmp_path):
    row, frozen = _universe(tmp_path / "intake", (_row(),))
    paper = PaperExecutionLedger(tmp_path / "paper-subclass.json")
    forged = _ForgedCanonicalPaperExecutionResolver(paper)

    with pytest.raises(
        EvaluationUniverseError,
        match="exact CanonicalPaperExecutionResolver",
    ):
        EvaluationUniverseLedger(frozen, paper_resolver=forged)


def test_paper_execution_ledger_subclass_is_rejected_before_event_reads(tmp_path):
    forged = _ForgedPaperExecutionLedger(tmp_path / "forged-paper-ledger.json")

    with pytest.raises(TypeError, match="exact PaperExecutionLedger"):
        CanonicalPaperExecutionResolver(forged)


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
    row, frozen = _universe(tmp_path / "intake", (_row(),))
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
    # H2H provider completeness freezes both home and away selections. Enriching one
    # canonical PAPER row must not shrink or rewrite the frozen denominator.
    assert ledger.cohort().sample_count == len(frozen.rows) == 2


def test_invented_reality_digest_is_rejected(tmp_path):
    row, frozen = _universe(tmp_path / "intake", (_row(),))
    resolver, _ = _resolver(tmp_path / "paper", _paper_attempt(row))
    ledger = EvaluationUniverseLedger(frozen, paper_resolver=resolver).append(
        _attempted(row)
    )

    with pytest.raises(EvaluationUniverseIntegrityError, match="canonical PAPER attempt event"):
        ledger.append(_outcome(row, FunnelStage.ACCEPTED, H3))


def test_foreign_paper_attempt_cannot_be_spliced_into_frozen_row(tmp_path):
    row, frozen = _universe(tmp_path / "intake", (_row(),))
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
    row, frozen = _universe(tmp_path / "intake", (_row(),))
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
    row, frozen = _universe(tmp_path / "intake", (_row(),))
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
    row, frozen = _universe(tmp_path / "intake", (_row(),))
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
    _authorize_structural_intake_for_tests(intake)

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


def test_intake_cycle_retry_is_idempotent_but_resolver_drift_fails(tmp_path):
    item = _row()
    witness = _witness(1, item)
    resolver = _Resolver((witness,))
    intake = ObservationIntakeLedger(
        tmp_path,
        authority_id="intake-1",
        enumeration_resolver=resolver,
    )
    first = intake.append_cycle(enumeration_id=witness.enumeration_id)
    second = intake.append_cycle(enumeration_id=witness.enumeration_id)
    assert first.record_sha256 == second.record_sha256

    resolver._witnesses[witness.enumeration_id] = replace(
        witness,
        acquisition_sha256=H2,
    )
    with pytest.raises(EvaluationIntakeIntegrityError, match="no longer matches"):
        intake.records()


def test_terminal_corrections_must_extend_the_unique_current_tip(tmp_path):
    row, frozen = _universe(tmp_path / "intake", (_row(),))
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
    )
    pending = FunnelEvent(
        row_id=row.row_id,
        stage=FunnelStage.PENDING,
        event_at="2026-09-20T00:10:00Z",
        settlement_proof_id="settlement-pending",
    )
    ledger = ledger.append(pending)
    settled = FunnelEvent(
        row_id=row.row_id,
        stage=FunnelStage.SETTLED,
        event_at="2026-09-20T00:10:01Z",
        settlement_proof_id="settlement-final",
        correction_of=pending.event_id,
    )
    ledger = ledger.append(settled)

    sibling = FunnelEvent(
        row_id=row.row_id,
        stage=FunnelStage.VOID,
        event_at="2026-09-20T00:10:02Z",
        settlement_proof_id="settlement-sibling",
        correction_of=pending.event_id,
    )
    with pytest.raises(EvaluationUniverseError, match="exact current terminal tip"):
        ledger.append(sibling)

    linear = FunnelEvent(
        row_id=row.row_id,
        stage=FunnelStage.VOID,
        event_at="2026-09-20T00:10:02Z",
        settlement_proof_id="settlement-linear",
        correction_of=settled.event_id,
    )
    advanced = ledger.append(linear)
    assert advanced.current_stage(row.row_id) is FunnelStage.VOID

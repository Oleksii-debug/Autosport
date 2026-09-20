from __future__ import annotations

import json
from dataclasses import replace

import pytest

from autosport.evaluation_universe import (
    AttritionReason,
    EvaluationUniverseError,
    EvaluationUniverseIntegrityError,
    EvaluationUniverseLedger,
    EvaluationUniverseStore,
    FunnelEvent,
    FunnelStage,
    SlotState,
    build_frozen_universe,
    EvaluationRow,
)


H = "a" * 64
H2 = "b" * 64
H3 = "c" * 64


def row(
    key: str,
    *,
    slot_state: SlotState = SlotState.CANDIDATE,
    stage: FunnelStage = FunnelStage.EXECUTION_MODEL_ELIGIBLE,
    reason: AttritionReason | None = None,
    event_id: str | None = "event-1",
    quote: str | None = H2,
    detection_at: str | None = "2026-09-20T00:00:03Z",
    decision_at: str | None = "2026-09-20T00:00:04Z",
    reveal_at: str | None = "2026-09-20T00:10:00Z",
) -> EvaluationRow:
    if slot_state is not SlotState.CANDIDATE:
        stage = FunnelStage.OBSERVED_SLOT
        reason = {
            SlotState.NO_EVENT: AttritionReason.NO_EVENT,
            SlotState.NO_QUOTE: AttritionReason.NO_QUOTE,
            SlotState.SOURCE_OUTAGE: AttritionReason.SOURCE_OUTAGE,
            SlotState.NO_CANDIDATE: AttritionReason.NO_CANDIDATE,
            SlotState.WAIT_ZERO: AttritionReason.WAIT_ZERO,
        }[slot_state]
        quote = None
        detection_at = None
        decision_at = None
        if slot_state is SlotState.NO_EVENT:
            event_id = None
    elif stage is not FunnelStage.EXECUTION_MODEL_ELIGIBLE and reason is None:
        reason = AttritionReason.RISK_REJECTED

    return EvaluationRow(
        row_key=key,
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=H,
        universe_id="universe-1",
        slot_state=slot_state,
        decision_stage=stage,
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
        detection_at=detection_at,
        decision_at=decision_at,
        quote_set_sha256=quote,
        freshness_policy_sha256=H3,
        strategy_version_id="strategy-1",
        model_version_id="model-1",
        config_sha256=H,
        portfolio_before_id="portfolio-1",
        economic_goal_id="goal-1",
        risk_policy_id="risk-1",
        terminal_space_proof_id="terminal-proof-1",
        settlement_proof_id="settlement-rule-proof-1",
        execution_model_id=(
            "paper-execution-model-1"
            if stage is FunnelStage.EXECUTION_MODEL_ELIGIBLE
            else None
        ),
        cost_contract_sha256=H2,
        outcome_reveal_not_before=reveal_at,
        dependence_cluster_keys=("event:event-1", "league:league-1"),
    )


def universe(*rows: EvaluationRow):
    return build_frozen_universe(
        universe_id="universe-1",
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=H,
        frozen_at="2026-09-20T00:05:00Z",
        rows=rows,
    )


def attempted(r: EvaluationRow, at: str = "2026-09-20T00:05:01Z") -> FunnelEvent:
    return FunnelEvent(
        row_id=r.row_id,
        stage=FunnelStage.ATTEMPTED,
        event_at=at,
        execution_model_id="paper-execution-model-1",
        execution_attempt_id="attempt-1",
    )


def attempt_outcome(r: EvaluationRow, stage: FunnelStage, at: str) -> FunnelEvent:
    return FunnelEvent(
        row_id=r.row_id,
        stage=stage,
        event_at=at,
        execution_attempt_id="attempt-1",
        execution_reality_sha256=H3,
    )


def reconciled(r: EvaluationRow, at: str = "2026-09-20T00:06:00Z") -> FunnelEvent:
    return FunnelEvent(
        row_id=r.row_id,
        stage=FunnelStage.RECONCILED,
        event_at=at,
        reason_code="paper-reconciled",
    )


def terminal(
    r: EvaluationRow,
    stage: FunnelStage,
    at: str = "2026-09-20T00:10:00Z",
    *,
    correction_of: str | None = None,
) -> FunnelEvent:
    return FunnelEvent(
        row_id=r.row_id,
        stage=stage,
        event_at=at,
        settlement_proof_id="settlement-proof-1",
        correction_of=correction_of,
    )


def full_ledger(r: EvaluationRow) -> EvaluationUniverseLedger:
    return EvaluationUniverseLedger(universe(r)).append(
        attempted(r)
    ).append(
        attempt_outcome(r, FunnelStage.ACCEPTED, "2026-09-20T00:05:10Z")
    ).append(
        reconciled(r)
    )


def test_zero_coverage_states_are_first_class_denominator_rows():
    rows = (
        row("candidate"),
        row("no-event", slot_state=SlotState.NO_EVENT),
        row("no-quote", slot_state=SlotState.NO_QUOTE),
        row("outage", slot_state=SlotState.SOURCE_OUTAGE),
        row("wait", slot_state=SlotState.WAIT_ZERO),
        row("none", slot_state=SlotState.NO_CANDIDATE),
    )
    ledger = EvaluationUniverseLedger(universe(*rows))
    cohort = ledger.cohort()

    assert cohort.sample_count == 6
    counts = dict(cohort.stage_counts)
    assert counts[FunnelStage.OBSERVED_SLOT.value] == 5
    assert counts[FunnelStage.EXECUTION_MODEL_ELIGIBLE.value] == 1
    attrition = dict(cohort.attrition_counts)
    assert attrition[AttritionReason.NO_EVENT.value] == 1
    assert attrition[AttritionReason.NO_QUOTE.value] == 1
    assert attrition[AttritionReason.SOURCE_OUTAGE.value] == 1
    assert attrition[AttritionReason.WAIT_ZERO.value] == 1


def test_duplicate_delivery_is_idempotent_but_conflicting_row_key_fails_closed():
    original = row("candidate")
    frozen = universe(original, original)
    assert len(frozen.rows) == 1

    conflicting = replace(original, quote_set_sha256=H3)
    with pytest.raises(EvaluationUniverseError, match="conflicting immutable row_key"):
        universe(original, conflicting)


def test_candidate_and_baseline_can_prove_exact_same_membership():
    candidate = EvaluationUniverseLedger(universe(row("a"), row("b"))).cohort()
    baseline = EvaluationUniverseLedger(universe(row("b"), row("a"))).cohort()

    candidate.assert_same_membership(baseline)
    assert candidate.membership_sha256 == baseline.membership_sha256
    assert candidate.row_ids == baseline.row_ids


def test_different_frozen_universe_membership_fails_comparison():
    candidate = EvaluationUniverseLedger(universe(row("a"), row("b"))).cohort()
    baseline = EvaluationUniverseLedger(universe(row("a"))).cohort()

    with pytest.raises(EvaluationUniverseError, match="exact frozen universe"):
        candidate.assert_same_membership(baseline)


def test_execution_evidence_enriches_existing_row_without_inflating_denominator():
    r = row("candidate")
    ledger = EvaluationUniverseLedger(universe(r))
    first = attempted(r)
    ledger = ledger.append(first).append(first)
    ledger = ledger.append(
        attempt_outcome(r, FunnelStage.PARTIAL, "2026-09-20T00:05:10Z")
    )
    ledger = ledger.append(reconciled(r))

    assert len(ledger.events) == 3
    assert ledger.cohort().sample_count == 1
    assert ledger.current_stage(r.row_id) is FunnelStage.RECONCILED


def test_terminal_outcome_cannot_attach_before_authoritative_reveal():
    r = row("candidate")
    ledger = full_ledger(r)
    with pytest.raises(EvaluationUniverseError, match="authoritative reveal time"):
        ledger.append(
            terminal(
                r,
                FunnelStage.SETTLED,
                at="2026-09-20T00:09:59Z",
            )
        )


def test_terminal_correction_is_append_only_and_preserves_original_event():
    r = row("candidate")
    ledger = full_ledger(r)
    pending = terminal(r, FunnelStage.PENDING)
    ledger = ledger.append(pending)
    settled = terminal(
        r,
        FunnelStage.SETTLED,
        at="2026-09-20T00:11:00Z",
        correction_of=pending.event_id,
    )
    corrected = ledger.append(settled)

    assert len(corrected.events) == len(ledger.events) + 1
    assert corrected.events[-2].stage is FunnelStage.PENDING
    assert corrected.events[-1].stage is FunnelStage.SETTLED
    assert corrected.current_stage(r.row_id) is FunnelStage.SETTLED


def test_stale_quote_is_explicit_pre_execution_attrition():
    r = row(
        "stale",
        stage=FunnelStage.DETECTED,
        reason=AttritionReason.STALE_QUOTE,
        decision_at=None,
    )
    cohort = EvaluationUniverseLedger(universe(r)).cohort()
    assert dict(cohort.attrition_counts)[AttritionReason.STALE_QUOTE.value] == 1
    assert dict(cohort.stage_counts)[FunnelStage.DETECTED.value] == 1


def test_future_membership_evidence_cannot_enter_already_frozen_universe():
    r = replace(
        row("late"),
        committed_at="2026-09-20T00:05:01Z",
        detection_at="2026-09-20T00:05:02Z",
        decision_at="2026-09-20T00:05:03Z",
    )
    with pytest.raises(EvaluationUniverseError, match="committed after frozen_at"):
        universe(r)


def test_store_round_trip_and_append_only_restart(tmp_path):
    r = row("candidate")
    first = EvaluationUniverseLedger(universe(r)).append(attempted(r))
    store = EvaluationUniverseStore(tmp_path)
    store.save(first)

    loaded = store.load()
    assert loaded is not None
    assert loaded.ledger_sha256 == first.ledger_sha256
    assert loaded.universe.membership_sha256 == first.universe.membership_sha256

    advanced = loaded.append(
        attempt_outcome(r, FunnelStage.REJECTED, "2026-09-20T00:05:10Z")
    )
    store.save(advanced)
    assert store.load().ledger_sha256 == advanced.ledger_sha256

    with pytest.raises(EvaluationUniverseIntegrityError, match="cannot shrink"):
        store.save(first)


def test_store_detects_payload_tamper(tmp_path):
    r = row("candidate")
    ledger = EvaluationUniverseLedger(universe(r))
    store = EvaluationUniverseStore(tmp_path)
    store.save(ledger)

    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["universe"]["rows"][0]["sport"] = "tampered"
    store.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvaluationUniverseIntegrityError):
        store.load()

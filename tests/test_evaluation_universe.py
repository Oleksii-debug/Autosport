from __future__ import annotations

import json
from dataclasses import replace
from tempfile import TemporaryDirectory

import pytest

from autosport.evaluation_intake import (
    EvaluationIntakeError,
    ObservationEnumerationWitness,
    ObservationIntakeLedger,
)
from autosport.evaluation_universe import (
    AttritionReason,
    EvaluationRow,
    EvaluationUniverseError,
    EvaluationUniverseIntegrityError,
    EvaluationUniverseLedger,
    EvaluationUniverseStore,
    FunnelEvent,
    FunnelStage,
    SlotState,
    build_frozen_universe,
)


H = "a" * 64
H2 = "b" * 64
H3 = "c" * 64


class _Resolver:
    def __init__(self, witnesses: tuple[ObservationEnumerationWitness, ...]) -> None:
        self._witnesses = {item.enumeration_id: item for item in witnesses}

    def resolve_enumeration(self, enumeration_id: str) -> ObservationEnumerationWitness:
        return self._witnesses[enumeration_id]


def _witness(
    index: int,
    item: "EvaluationRow",
    *,
    exhaustive: bool = True,
    gap_free: bool = True,
    row_keys: tuple[str, ...] | None = None,
) -> ObservationEnumerationWitness:
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
        start_cursor=f"cursor-{index}-start",
        end_cursor=f"cursor-{index}-end",
        acquisition_sha256=H3,
        row_keys=row_keys or (item.row_key,),
        exhaustive=exhaustive,
        gap_free=gap_free,
        committed_at=f"2026-09-20T00:04:{index:02d}Z",
        evaluation_not_before=f"2026-09-20T00:04:{index:02d}Z",
        outcome_reveal_not_before=item.outcome_reveal_not_before,
    )


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
    reveal_at: str = "2026-09-20T00:10:00Z",
) -> EvaluationRow:
    candidate = slot_state is SlotState.CANDIDATE
    if not candidate:
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

    executable = candidate and stage is FunnelStage.EXECUTION_MODEL_ELIGIBLE
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
        execution_model_id="paper-model-fingerprint-1" if executable else None,
        execution_run_id="paper-run-1" if executable else None,
        execution_plan_id="paper-plan-1" if executable else None,
        execution_action_id="paper-action-1" if executable else None,
        decision_quote_id="quote-1" if executable else None,
        cost_contract_sha256=H2,
        outcome_reveal_not_before=reveal_at,
        dependence_cluster_keys=("event:event-1", "league:league-1"),
    )


def intake(workspace, rows: tuple[EvaluationRow, ...]) -> ObservationIntakeLedger:
    unique = {item.row_key: item for item in rows}
    witnesses = tuple(
        _witness(index, item)
        for index, item in enumerate(sorted(unique.values(), key=lambda value: value.row_key), 1)
    )
    ledger = ObservationIntakeLedger(
        workspace,
        authority_id="intake-1",
        enumeration_resolver=_Resolver(witnesses),
    )
    for witness in witnesses:
        ledger.append_cycle(enumeration_id=witness.enumeration_id)
    return ledger


def universe(*rows: EvaluationRow):
    with TemporaryDirectory() as workspace:
        intake_ledger = intake(workspace, tuple(rows))
        return build_frozen_universe(
            intake_ledger=intake_ledger,
            universe_id="universe-1",
            campaign_id="campaign-1",
            research_protocol_id="protocol-1",
            protocol_sha256=H,
            frozen_at="2026-09-20T00:05:00Z",
            rows=rows,
        )


def attempted(r: EvaluationRow) -> FunnelEvent:
    return FunnelEvent(
        row_id=r.row_id,
        stage=FunnelStage.ATTEMPTED,
        event_at="2026-09-20T00:05:01Z",
        execution_model_id=r.execution_model_id,
        execution_attempt_id="attempt-1",
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
    cohort = EvaluationUniverseLedger(universe(*rows)).cohort()

    assert cohort.sample_count == 6
    assert dict(cohort.stage_counts)[FunnelStage.OBSERVED_SLOT.value] == 5
    assert dict(cohort.stage_counts)[FunnelStage.EXECUTION_MODEL_ELIGIBLE.value] == 1
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


def test_candidate_and_baseline_share_the_exact_same_frozen_universe():
    frozen = universe(row("a"), row("b"))
    candidate = EvaluationUniverseLedger(frozen).cohort()
    baseline = EvaluationUniverseLedger(frozen).cohort()

    candidate.assert_same_membership(baseline)
    assert candidate.membership_sha256 == baseline.membership_sha256
    assert candidate.row_ids == baseline.row_ids


def test_different_frozen_membership_fails_comparison():
    candidate = EvaluationUniverseLedger(universe(row("a"), row("b"))).cohort()
    baseline = EvaluationUniverseLedger(universe(row("a"))).cohort()

    with pytest.raises(EvaluationUniverseError, match="exact frozen universe"):
        candidate.assert_same_membership(baseline)


def test_stale_quote_is_explicit_pre_execution_attrition():
    stale = row(
        "stale",
        stage=FunnelStage.DETECTED,
        reason=AttritionReason.STALE_QUOTE,
        decision_at=None,
    )
    cohort = EvaluationUniverseLedger(universe(stale)).cohort()
    assert dict(cohort.attrition_counts)[AttritionReason.STALE_QUOTE.value] == 1
    assert dict(cohort.stage_counts)[FunnelStage.DETECTED.value] == 1


def test_intake_membership_committed_after_freeze_fails_closed(tmp_path):
    item = row("late", reveal_at="2026-09-20T00:11:00Z")
    witness = replace(
        _witness(1, item),
        committed_at="2026-09-20T00:05:01Z",
        evaluation_not_before="2026-09-20T00:05:01Z",
    )
    ledger = ObservationIntakeLedger(
        tmp_path,
        authority_id="intake-1",
        enumeration_resolver=_Resolver((witness,)),
    )
    ledger.append_cycle(enumeration_id=witness.enumeration_id)
    with pytest.raises(EvaluationUniverseError, match="after universe freeze"):
        build_frozen_universe(
            intake_ledger=ledger,
            universe_id="universe-1",
            campaign_id="campaign-1",
            research_protocol_id="protocol-1",
            protocol_sha256=H,
            frozen_at="2026-09-20T00:05:00Z",
            rows=(item,),
        )


def test_store_round_trip_and_append_only_restart(tmp_path):
    item = row("candidate")
    intake_ledger = intake(tmp_path, (item,))
    frozen = build_frozen_universe(
        intake_ledger=intake_ledger,
        universe_id="universe-1",
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=H,
        frozen_at="2026-09-20T00:05:00Z",
        rows=(item,),
    )
    first = EvaluationUniverseLedger(frozen).append(attempted(item))
    store = EvaluationUniverseStore(tmp_path, intake_ledger=intake_ledger)
    store.save(first)

    loaded = store.load()
    assert loaded is not None
    assert loaded.ledger_sha256 == first.ledger_sha256
    assert loaded.universe.membership_sha256 == first.universe.membership_sha256
    store.save(loaded)


def test_store_detects_payload_tamper(tmp_path):
    item = row("candidate")
    intake_ledger = intake(tmp_path, (item,))
    frozen = build_frozen_universe(
        intake_ledger=intake_ledger,
        universe_id="universe-1",
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=H,
        frozen_at="2026-09-20T00:05:00Z",
        rows=(item,),
    )
    store = EvaluationUniverseStore(tmp_path, intake_ledger=intake_ledger)
    store.save(EvaluationUniverseLedger(frozen))

    payload = json.loads(store.path.read_text(encoding="utf-8"))
    payload["universe"]["rows"][0]["sport"] = "tampered"
    store.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(EvaluationUniverseIntegrityError):
        store.load()


def test_row_reveal_boundary_must_equal_pre_result_intake(tmp_path):
    item = row("candidate")
    witness = replace(
        _witness(1, item),
        outcome_reveal_not_before="2026-09-20T00:11:00Z",
    )
    ledger = ObservationIntakeLedger(
        tmp_path,
        authority_id="intake-1",
        enumeration_resolver=_Resolver((witness,)),
    )
    ledger.append_cycle(enumeration_id=witness.enumeration_id)
    with pytest.raises(EvaluationUniverseError, match="reveal boundary"):
        build_frozen_universe(
            intake_ledger=ledger,
            universe_id="universe-1",
            campaign_id="campaign-1",
            research_protocol_id="protocol-1",
            protocol_sha256=H,
            frozen_at="2026-09-20T00:05:00Z",
            rows=(item,),
        )


def test_incomplete_or_gapped_enumeration_cannot_authorize_denominator(tmp_path):
    item = row("candidate")
    for witness in (
        _witness(1, item, exhaustive=False),
        _witness(1, item, gap_free=False),
    ):
        ledger = ObservationIntakeLedger(
            tmp_path / witness.enumeration_id,
            authority_id="intake-1",
            enumeration_resolver=_Resolver((witness,)),
        )
        with pytest.raises(EvaluationIntakeError, match="exhaustive gap-free"):
            ledger.append_cycle(enumeration_id=witness.enumeration_id)

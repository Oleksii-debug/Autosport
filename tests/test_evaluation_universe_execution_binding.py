from __future__ import annotations

from dataclasses import replace

import pytest

from autosport.evaluation_universe import (
    EvaluationRow,
    EvaluationUniverseError,
    EvaluationUniverseLedger,
    FunnelEvent,
    FunnelStage,
    ObservationManifest,
    SlotState,
    build_frozen_universe,
)


H = "a" * 64
H2 = "b" * 64
H3 = "c" * 64


def _row(key: str = "candidate", *, slot_state: SlotState = SlotState.CANDIDATE) -> EvaluationRow:
    is_candidate = slot_state is SlotState.CANDIDATE
    reason = None
    if not is_candidate:
        from autosport.evaluation_universe import AttritionReason
        reason = {
            SlotState.NO_EVENT: AttritionReason.NO_EVENT,
            SlotState.NO_QUOTE: AttritionReason.NO_QUOTE,
            SlotState.SOURCE_OUTAGE: AttritionReason.SOURCE_OUTAGE,
            SlotState.NO_CANDIDATE: AttritionReason.NO_CANDIDATE,
            SlotState.WAIT_ZERO: AttritionReason.WAIT_ZERO,
        }[slot_state]
    event_id = "event-1"
    if slot_state is SlotState.NO_EVENT:
        event_id = None
    return EvaluationRow(
        row_key=key,
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=H,
        universe_id="universe-1",
        slot_state=slot_state,
        decision_stage=(FunnelStage.EXECUTION_MODEL_ELIGIBLE if is_candidate else FunnelStage.OBSERVED_SLOT),
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
        detection_at="2026-09-20T00:00:03Z" if is_candidate else None,
        decision_at="2026-09-20T00:00:04Z" if is_candidate else None,
        quote_set_sha256=H2 if is_candidate else None,
        freshness_policy_sha256=H3,
        strategy_version_id="strategy-1",
        model_version_id="model-1",
        config_sha256=H,
        portfolio_before_id="portfolio-1",
        economic_goal_id="goal-1",
        risk_policy_id="risk-1",
        terminal_space_proof_id="terminal-proof-1",
        settlement_proof_id="settlement-rule-proof-1",
        execution_model_id="paper-execution-model-1" if is_candidate else None,
        cost_contract_sha256=H2,
        outcome_reveal_not_before="2026-09-20T00:10:00Z",
        dependence_cluster_keys=("event:event-1", "league:league-1"),
    )


def _manifest(*keys: str) -> ObservationManifest:
    return ObservationManifest(
        manifest_id="manifest-1",
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=H,
        universe_id="universe-1",
        enumeration_source_id="campaign-intake-1",
        source_range_start="cursor-0001",
        source_range_end="cursor-9999",
        committed_at="2026-09-20T00:04:59Z",
        expected_row_keys=tuple(sorted(keys)),
    )


def _ledger(row: EvaluationRow) -> EvaluationUniverseLedger:
    frozen = build_frozen_universe(
        manifest=_manifest(row.row_key),
        universe_id="universe-1",
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=H,
        frozen_at="2026-09-20T00:05:00Z",
        rows=(row,),
    )
    return EvaluationUniverseLedger(frozen)


def test_execution_events_bind_frozen_execution_model_and_attempt_identity():
    row = _row()
    ledger = _ledger(row)
    with pytest.raises(EvaluationUniverseError, match="execution model"):
        ledger.append(
            FunnelEvent(
                row_id=row.row_id,
                stage=FunnelStage.ATTEMPTED,
                event_at="2026-09-20T00:05:01Z",
                execution_model_id="different-model",
                execution_attempt_id="attempt-1",
            )
        )

    ledger = ledger.append(
        FunnelEvent(
            row_id=row.row_id,
            stage=FunnelStage.ATTEMPTED,
            event_at="2026-09-20T00:05:01Z",
            execution_model_id="paper-execution-model-1",
            execution_attempt_id="attempt-1",
        )
    )
    with pytest.raises(EvaluationUniverseError, match="exact attempted execution identity"):
        ledger.append(
            FunnelEvent(
                row_id=row.row_id,
                stage=FunnelStage.ACCEPTED,
                event_at="2026-09-20T00:05:10Z",
                execution_attempt_id="attempt-2",
                execution_reality_sha256=H3,
            )
        )


def test_post_freeze_enrichment_cannot_backdate_before_membership_freeze():
    row = _row()
    with pytest.raises(EvaluationUniverseError, match="cannot predate frozen_at"):
        _ledger(row).append(
            FunnelEvent(
                row_id=row.row_id,
                stage=FunnelStage.ATTEMPTED,
                event_at="2026-09-20T00:04:59Z",
                execution_model_id="paper-execution-model-1",
                execution_attempt_id="attempt-1",
            )
        )


def test_portfolio_before_identity_is_part_of_immutable_denominator_row():
    original = _row()
    conflicting = replace(original, portfolio_before_id="portfolio-other")
    with pytest.raises(EvaluationUniverseError, match="conflicting immutable row_key"):
        build_frozen_universe(
            manifest=_manifest("candidate"),
            universe_id="universe-1",
            campaign_id="campaign-1",
            research_protocol_id="protocol-1",
            protocol_sha256=H,
            frozen_at="2026-09-20T00:05:00Z",
            rows=(original, conflicting),
        )


def test_manifest_rejects_dropped_zero_coverage_or_candidate_rows():
    candidate = _row("candidate")
    no_quote = _row("no-quote", slot_state=SlotState.NO_QUOTE)
    outage = _row("outage", slot_state=SlotState.SOURCE_OUTAGE)
    manifest = _manifest("candidate", "no-quote", "outage")

    for incomplete in (
        (candidate, no_quote),
        (candidate, outage),
        (no_quote, outage),
        (candidate,),
    ):
        with pytest.raises(EvaluationUniverseError, match="canonical observation manifest"):
            build_frozen_universe(
                manifest=manifest,
                universe_id="universe-1",
                campaign_id="campaign-1",
                research_protocol_id="protocol-1",
                protocol_sha256=H,
                frozen_at="2026-09-20T00:05:00Z",
                rows=incomplete,
            )


def test_manifest_rejects_unenumerated_extra_row():
    candidate = _row("candidate")
    extra = _row("extra")
    with pytest.raises(EvaluationUniverseError, match="canonical observation manifest"):
        build_frozen_universe(
            manifest=_manifest("candidate"),
            universe_id="universe-1",
            campaign_id="campaign-1",
            research_protocol_id="protocol-1",
            protocol_sha256=H,
            frozen_at="2026-09-20T00:05:00Z",
            rows=(candidate, extra),
        )

from __future__ import annotations

from dataclasses import replace

import pytest

from autosport.evaluation_universe import (
    EvaluationRow,
    EvaluationUniverseError,
    EvaluationUniverseLedger,
    FunnelEvent,
    FunnelStage,
    SlotState,
    build_frozen_universe,
)


H = "a" * 64
H2 = "b" * 64
H3 = "c" * 64


def _row() -> EvaluationRow:
    return EvaluationRow(
        row_key="candidate",
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256=H,
        universe_id="universe-1",
        slot_state=SlotState.CANDIDATE,
        decision_stage=FunnelStage.EXECUTION_MODEL_ELIGIBLE,
        attrition_reason=None,
        sport="football",
        provider_id="provider-1",
        source_id="source-1",
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        source_at="2026-09-20T00:00:00Z",
        received_at="2026-09-20T00:00:01Z",
        committed_at="2026-09-20T00:00:02Z",
        detection_at="2026-09-20T00:00:03Z",
        decision_at="2026-09-20T00:00:04Z",
        quote_set_sha256=H2,
        freshness_policy_sha256=H3,
        strategy_version_id="strategy-1",
        model_version_id="model-1",
        config_sha256=H,
        portfolio_before_id="portfolio-1",
        economic_goal_id="goal-1",
        risk_policy_id="risk-1",
        terminal_space_proof_id="terminal-proof-1",
        settlement_proof_id="settlement-rule-proof-1",
        execution_model_id="paper-execution-model-1",
        cost_contract_sha256=H2,
        outcome_reveal_not_before="2026-09-20T00:10:00Z",
        dependence_cluster_keys=("event:event-1", "league:league-1"),
    )


def _ledger(row: EvaluationRow) -> EvaluationUniverseLedger:
    frozen = build_frozen_universe(
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
            universe_id="universe-1",
            campaign_id="campaign-1",
            research_protocol_id="protocol-1",
            protocol_sha256=H,
            frozen_at="2026-09-20T00:05:00Z",
            rows=(original, conflicting),
        )

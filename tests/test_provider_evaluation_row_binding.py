from __future__ import annotations

from dataclasses import replace

import pytest

from autosport.evaluation_universe import (
    AttritionReason,
    EvaluationRow,
    FunnelStage,
    SlotState,
)
from autosport.provider_evaluation_universe import (
    ProviderEvaluationUniverseError,
    _row_semantic_sha256,
)


class _ForgedEvaluationRow(EvaluationRow):
    pass


def _row() -> EvaluationRow:
    return EvaluationRow(
        row_key="row-1",
        campaign_id="campaign-1",
        research_protocol_id="protocol-1",
        protocol_sha256="a" * 64,
        universe_id="universe-1",
        slot_state=SlotState.CANDIDATE,
        decision_stage=FunnelStage.DETECTED,
        attrition_reason=AttritionReason.INCOMPLETE_EVIDENCE,
        sport="tennis_atp",
        provider_id="parlayapi",
        source_id="parlayapi",
        event_id="event-1",
        market_id="pinnacle:h2h",
        selection_id="pinnacle:h2h:home",
        source_at="2026-09-20T10:00:00Z",
        received_at="2026-09-20T10:00:00Z",
        committed_at="2026-09-20T10:00:00Z",
        detection_at="2026-09-20T10:01:00Z",
        decision_at=None,
        quote_set_sha256="b" * 64,
        freshness_policy_sha256="c" * 64,
        strategy_version_id="strategy-v1",
        model_version_id=None,
        config_sha256="d" * 64,
        portfolio_before_id="portfolio-1",
        economic_goal_id="goal-1",
        risk_policy_id="risk-v1",
        terminal_space_proof_id=None,
        settlement_proof_id=None,
        execution_model_id=None,
        execution_run_id=None,
        execution_plan_id=None,
        execution_action_id=None,
        decision_quote_id=None,
        cost_contract_sha256="e" * 64,
        outcome_reveal_not_before="2026-09-20T12:00:00Z",
        dependence_cluster_keys=("event:event-1",),
    )


def test_row_semantic_digest_rejects_subclasses_before_payload_reads() -> None:
    forged = _ForgedEvaluationRow.from_payload(_row().to_payload())

    with pytest.raises(
        ProviderEvaluationUniverseError,
        match="exact EvaluationRow",
    ):
        _row_semantic_sha256(forged)


def test_row_semantic_digest_is_deterministic() -> None:
    row = _row()

    assert _row_semantic_sha256(row) == _row_semantic_sha256(replace(row))


def test_row_semantic_digest_binds_risk_identity() -> None:
    row = _row()

    assert _row_semantic_sha256(row) != _row_semantic_sha256(
        replace(row, risk_policy_id="risk-v2")
    )


def test_row_semantic_digest_binds_configuration_identity() -> None:
    row = _row()

    assert _row_semantic_sha256(row) != _row_semantic_sha256(
        replace(row, config_sha256="f" * 64)
    )


def test_row_semantic_digest_binds_decision_classification() -> None:
    row = _row()

    assert _row_semantic_sha256(row) != _row_semantic_sha256(
        replace(
            row,
            decision_stage=FunnelStage.ELIGIBLE,
            decision_at="2026-09-20T10:02:00Z",
        )
    )

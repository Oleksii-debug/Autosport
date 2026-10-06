from dataclasses import replace

import pytest

from autosport._provider_evaluation_semantic_gate import (
    _validate_product_semantic_authority,
    _validate_row_against_semantic_slot,
)
from autosport.evaluation_universe import (
    AttritionReason,
    EvaluationRow,
    FunnelStage,
    SlotState,
)
from autosport.pre_evaluation_binding import BoundPreEvaluationSession
from autosport.pre_evaluation_product_origin import (
    ProductOwnedPreEvaluationSemanticSession,
)
from autosport.pre_evaluation_semantics import (
    PreEvaluationSlotSemanticEvidence,
    SemanticAttritionReason,
    SemanticFunnelStage,
    SemanticSlotState,
)
from autosport.provider_evaluation_universe import (
    ProviderEvaluationUniverseError,
    build_frozen_universe_from_complete_game_board,
)


class _ForgedProductOwnedSemanticSession(ProductOwnedPreEvaluationSemanticSession):
    @property
    def slots(self):
        return (_slot(),)


class _ForgedBoundPreEvaluationSession(BoundPreEvaluationSession):
    pass


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
        source_id="parlayapi:tennis_atp",
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
        model_version_id="model-v1",
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


def _slot() -> PreEvaluationSlotSemanticEvidence:
    return PreEvaluationSlotSemanticEvidence(
        row_key="row-1",
        member_sha256="1" * 64,
        provider_selection_sha256="2" * 64,
        source_slot_evidence_digest="3" * 64,
        slot_state=SemanticSlotState.CANDIDATE,
        decision_stage=SemanticFunnelStage.DETECTED,
        attrition_reason=SemanticAttritionReason.INCOMPLETE_EVIDENCE,
        detection_at="2026-09-20T10:01:00Z",
        decision_at=None,
        quote_set_sha256="b" * 64,
        freshness_policy_sha256="c" * 64,
        strategy_version_id="strategy-v1",
        model_version_id="model-v1",
        config_sha256="d" * 64,
        portfolio_before_id="portfolio-1",
        economic_goal_id="goal-1",
        risk_policy_id="risk-v1",
        cost_contract_sha256="e" * 64,
        dependence_cluster_keys=("event:event-1",),
        opportunity_intent_sha256="4" * 64,
        quote_identity_sha256="5" * 64,
    )


def _validation_kwargs() -> dict[str, object]:
    return {
        "snapshot": object(),
        "session_id": "session-1",
        "campaign_id": "campaign-1",
        "research_protocol_id": "protocol-1",
        "protocol_sha256": "a" * 64,
        "rows": (),
    }


def test_exact_product_semantic_row_is_accepted() -> None:
    _validate_row_against_semantic_slot(_row(), _slot())


def test_row_subclass_is_rejected_before_semantic_reads() -> None:
    forged = _ForgedEvaluationRow.from_payload(_row().to_payload())

    with pytest.raises(
        ProviderEvaluationUniverseError,
        match="exact EvaluationRow",
    ):
        _validate_row_against_semantic_slot(forged, _slot())


def test_product_semantic_gate_rejects_row_subclass_before_authority_reads() -> None:
    forged = _ForgedEvaluationRow.from_payload(_row().to_payload())
    kwargs = _validation_kwargs()
    kwargs["rows"] = (forged,)

    with pytest.raises(
        ProviderEvaluationUniverseError,
        match="exact EvaluationRow",
    ):
        _validate_product_semantic_authority(
            **kwargs,
            pre_evaluation_authority=object.__new__(
                ProductOwnedPreEvaluationSemanticSession
            ),
            pre_evaluation_bound=object.__new__(BoundPreEvaluationSession),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("risk_policy_id", "risk-v2"),
        ("config_sha256", "f" * 64),
        ("portfolio_before_id", "portfolio-2"),
        ("cost_contract_sha256", "6" * 64),
        ("freshness_policy_sha256", "7" * 64),
    ),
)
def test_changed_semantic_field_is_rejected(field: str, value: str) -> None:
    with pytest.raises(
        ProviderEvaluationUniverseError,
        match=field,
    ):
        _validate_row_against_semantic_slot(
            replace(_row(), **{field: value}),
            _slot(),
        )


def test_execution_identity_cannot_be_smuggled_through_pre_evaluation_authority() -> None:
    with pytest.raises(
        ProviderEvaluationUniverseError,
        match="cannot mint execution or terminal proof identity",
    ):
        _validate_row_against_semantic_slot(
            replace(_row(), terminal_space_proof_id="caller-proof"),
            _slot(),
        )


def test_product_semantic_authority_subclass_cannot_override_slots() -> None:
    forged = object.__new__(_ForgedProductOwnedSemanticSession)
    exact_bound = object.__new__(BoundPreEvaluationSession)

    with pytest.raises(
        ProviderEvaluationUniverseError,
        match="requires product-owned pre-evaluation semantic authority",
    ):
        _validate_product_semantic_authority(
            **_validation_kwargs(),
            pre_evaluation_authority=forged,
            pre_evaluation_bound=exact_bound,
        )


def test_bound_session_subclass_cannot_override_context_or_members() -> None:
    exact_authority = object.__new__(ProductOwnedPreEvaluationSemanticSession)
    forged_bound = object.__new__(_ForgedBoundPreEvaluationSession)

    with pytest.raises(
        ProviderEvaluationUniverseError,
        match="requires exact bound pre-evaluation session",
    ):
        _validate_product_semantic_authority(
            **_validation_kwargs(),
            pre_evaluation_authority=exact_authority,
            pre_evaluation_bound=forged_bound,
        )


def test_public_provider_builder_fails_closed_without_product_semantic_authority() -> None:
    with pytest.raises(
        ProviderEvaluationUniverseError,
        match="requires product-owned pre-evaluation semantic authority",
    ):
        build_frozen_universe_from_complete_game_board(rows=(_row(),))

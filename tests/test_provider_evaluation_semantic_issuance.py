from dataclasses import replace

import pytest

from autosport import _provider_evaluation_semantic_gate as semantic_gate
from autosport._provider_evaluation_semantic_issuance import (
    _assert_product_semantic_authority_issued,
    _remember_product_semantic_authority,
)
from autosport.pre_evaluation_binding import BoundPreEvaluationSession
from autosport import pre_evaluation_product_origin as product_origin
from autosport.pre_evaluation_product_origin import (
    PreEvaluationProductOrigin,
    ProductOwnedPreEvaluationSemanticSession,
)
from autosport.pre_evaluation_semantics import (
    PreEvaluationSemanticSession,
    PreEvaluationSlotSemanticEvidence,
    SemanticAttritionReason,
    SemanticFunnelStage,
    SemanticSlotState,
)
from autosport.provider_evaluation_universe import ProviderEvaluationUniverseError


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64
SHA_1 = "1" * 64
SHA_2 = "2" * 64
SHA_3 = "3" * 64


def _slot(*, risk_policy_id: str = "paper-risk-policy:canonical"):
    return PreEvaluationSlotSemanticEvidence(
        row_key="row-1",
        member_sha256=SHA_1,
        provider_selection_sha256=SHA_2,
        source_slot_evidence_digest=SHA_3,
        slot_state=SemanticSlotState.WAIT_ZERO,
        decision_stage=SemanticFunnelStage.OBSERVED_SLOT,
        attrition_reason=SemanticAttritionReason.WAIT_ZERO,
        detection_at=None,
        decision_at=None,
        quote_set_sha256=None,
        freshness_policy_sha256=SHA_A,
        strategy_version_id="strategy-v1",
        model_version_id=None,
        config_sha256=SHA_B,
        portfolio_before_id="paper-book:canonical",
        economic_goal_id="goal-1",
        risk_policy_id=risk_policy_id,
        cost_contract_sha256=SHA_F,
        dependence_cluster_keys=("event:event-1",),
        opportunity_intent_sha256=None,
        quote_identity_sha256=None,
    )


def _issued_authority() -> ProductOwnedPreEvaluationSemanticSession:
    origin = PreEvaluationProductOrigin(
        bound_authority_digest=SHA_A,
        denominator_context_digest=SHA_B,
        provider_evidence_sha256=SHA_C,
        provider_bindings_sha256=SHA_D,
        intent_sha256s=(),
        durable_decision_context_hash=SHA_E,
        risk_policy_sha256=SHA_C,
        portfolio_sha256=SHA_D,
        dependency_graph_sha256=SHA_E,
        cost_contract_sha256=SHA_F,
    )
    # Model the upstream #662 canonical resolver issuance without mutating #662 source.
    product_origin._remember(origin)
    session = PreEvaluationSemanticSession(
        bound_authority_digest=origin.bound_authority_digest,
        denominator_context_digest=origin.denominator_context_digest,
        portfolio_sha256=origin.portfolio_sha256,
        risk_policy_sha256=origin.risk_policy_sha256,
        economic_goal_identity="goal-1",
        cost_contract_sha256=origin.cost_contract_sha256,
        dependency_graph_sha256=origin.dependency_graph_sha256,
        provider_bindings_sha256=origin.provider_bindings_sha256,
        slots=(_slot(),),
    )
    authority = ProductOwnedPreEvaluationSemanticSession(
        session=session,
        origin=origin,
    )
    return _remember_product_semantic_authority(authority)


def test_exact_base_session_rewrap_cannot_reuse_genuine_origin() -> None:
    canonical = _issued_authority()
    _assert_product_semantic_authority_issued(canonical)

    forged_slot = replace(canonical.session.slots[0], risk_policy_id="forged-risk")
    forged_session = replace(canonical.session, slots=(forged_slot,))
    forged = ProductOwnedPreEvaluationSemanticSession(
        session=forged_session,
        origin=canonical.origin,
    )

    with pytest.raises(
        ValueError,
        match="not issued by canonical product derivation",
    ):
        _assert_product_semantic_authority_issued(forged)

    with pytest.raises(
        ProviderEvaluationUniverseError,
        match="semantic session was not issued by canonical product derivation",
    ):
        semantic_gate._validate_product_semantic_authority(
            snapshot=object(),
            session_id="session-1",
            campaign_id="campaign-1",
            research_protocol_id="protocol-1",
            protocol_sha256=SHA_A,
            rows=(),
            pre_evaluation_authority=forged,
            pre_evaluation_bound=object.__new__(BoundPreEvaluationSession),
        )

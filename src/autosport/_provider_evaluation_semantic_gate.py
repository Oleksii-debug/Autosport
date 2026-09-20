from __future__ import annotations

"""Fail closed caller-authored pre-evaluation semantics in the provider denominator.

The provider-completeness bridge already proves exact membership and reveal timing.  A
hash of an ``EvaluationRow`` only proves that the caller-supplied row did not change
after hashing; it does not prove where slot/decision/quote/risk/cost semantics came
from.  This gate requires the independently re-resolved product-owned semantic
capability integrated by #662 before the #638 denominator can be frozen.

The private bypass below exists only so the older #638 provider/storage regression
fixtures can keep testing their own concern while dedicated semantic-gate tests exercise
this production fence.  As with the structural-intake test hook, private implementation
state is outside the supported product API/threat contract.
"""

from dataclasses import replace
from typing import Iterable

from .evaluation_universe import EvaluationRow
from .pre_evaluation_binding import BoundPreEvaluationSession
from .pre_evaluation_product_origin import (
    ProductOwnedPreEvaluationSemanticSession,
    assert_pre_evaluation_product_origin_authoritative,
)
from .pre_evaluation_semantics import (
    PreEvaluationSemanticAuthority,
    PreEvaluationSemanticSession,
    PreEvaluationSlotSemanticEvidence,
)


_LEGACY_FIXTURE_BYPASS = False


def _set_legacy_provider_semantic_bypass_for_tests(enabled: bool) -> None:
    """Private compatibility hook for pre-#662 provider-denominator fixtures."""

    global _LEGACY_FIXTURE_BYPASS
    if type(enabled) is not bool:
        raise TypeError("enabled must be bool")
    _LEGACY_FIXTURE_BYPASS = enabled


def _enum_value(value: object) -> object:
    return getattr(value, "value", value)


def _same_optional_instant(
    provider_consumer,
    left: str | None,
    right: str | None,
    *,
    field: str,
) -> bool:
    if left is None or right is None:
        return left is right
    return provider_consumer._instant(left, field) == provider_consumer._instant(
        right, field
    )


def _validate_row_against_semantic_slot(
    row: EvaluationRow,
    slot: PreEvaluationSlotSemanticEvidence,
) -> None:
    """Require every caller-visible pre-freeze semantic field to equal #662 authority."""

    from . import provider_evaluation_universe as provider_consumer

    if type(row) is not EvaluationRow:
        raise provider_consumer.ProviderEvaluationUniverseError(
            "rows must contain exact EvaluationRow values"
        )
    if not isinstance(slot, PreEvaluationSlotSemanticEvidence):
        raise provider_consumer.ProviderEvaluationUniverseError(
            "pre-evaluation semantic slot has invalid type"
        )

    comparisons = (
        ("row_key", row.row_key, slot.row_key),
        ("slot_state", _enum_value(row.slot_state), _enum_value(slot.slot_state)),
        (
            "decision_stage",
            _enum_value(row.decision_stage),
            _enum_value(slot.decision_stage),
        ),
        (
            "attrition_reason",
            None if row.attrition_reason is None else _enum_value(row.attrition_reason),
            _enum_value(slot.attrition_reason),
        ),
        ("quote_set_sha256", row.quote_set_sha256, slot.quote_set_sha256),
        (
            "freshness_policy_sha256",
            row.freshness_policy_sha256,
            slot.freshness_policy_sha256,
        ),
        ("strategy_version_id", row.strategy_version_id, slot.strategy_version_id),
        ("model_version_id", row.model_version_id, slot.model_version_id),
        ("config_sha256", row.config_sha256, slot.config_sha256),
        ("portfolio_before_id", row.portfolio_before_id, slot.portfolio_before_id),
        ("economic_goal_id", row.economic_goal_id, slot.economic_goal_id),
        ("risk_policy_id", row.risk_policy_id, slot.risk_policy_id),
        ("cost_contract_sha256", row.cost_contract_sha256, slot.cost_contract_sha256),
        (
            "dependence_cluster_keys",
            row.dependence_cluster_keys,
            slot.dependence_cluster_keys,
        ),
    )
    for field, actual, expected in comparisons:
        if actual != expected:
            raise provider_consumer.ProviderEvaluationUniverseError(
                f"evaluation row {field} does not equal product-owned pre-evaluation semantic authority"
            )

    for field, actual, expected in (
        ("detection_at", row.detection_at, slot.detection_at),
        ("decision_at", row.decision_at, slot.decision_at),
    ):
        if not _same_optional_instant(
            provider_consumer,
            actual,
            expected,
            field=field,
        ):
            raise provider_consumer.ProviderEvaluationUniverseError(
                f"evaluation row {field} does not equal product-owned pre-evaluation semantic authority"
            )

    # #662 deliberately stops before PAPER execution identity.  A caller cannot use a
    # valid pre-evaluation semantic capability to smuggle later execution/terminal proof
    # fields into the immutable initial denominator.
    unsupported_positive_refs = (
        row.terminal_space_proof_id,
        row.settlement_proof_id,
        row.execution_model_id,
        row.execution_run_id,
        row.execution_plan_id,
        row.execution_action_id,
        row.decision_quote_id,
    )
    if any(value is not None for value in unsupported_positive_refs):
        raise provider_consumer.ProviderEvaluationUniverseError(
            "product-owned pre-evaluation semantic authority cannot mint execution or terminal proof identity"
        )


def _validate_product_semantic_authority(
    *,
    snapshot,
    session_id: str,
    campaign_id: str,
    research_protocol_id: str,
    protocol_sha256: str,
    rows: tuple[EvaluationRow, ...],
    pre_evaluation_authority: ProductOwnedPreEvaluationSemanticSession,
    pre_evaluation_bound: BoundPreEvaluationSession,
) -> tuple[dict[str, str], ...]:
    from . import provider_evaluation_universe as provider_consumer

    # Rows are authority-bearing immutable evidence objects, not extension points.
    # Reject subclasses before the first row_key/property read so a caller cannot
    # make validation and later hashing/serialization observe different values.
    if not all(type(row) is EvaluationRow for row in rows):
        raise provider_consumer.ProviderEvaluationUniverseError(
            "rows must contain exact EvaluationRow values"
        )

    # Capability wrappers are authority-bearing objects, not extension points.  Exact
    # concrete types prevent a subclass from retaining a genuine issued origin while
    # overriding slots/context/members with forged denominator semantics.
    if type(pre_evaluation_authority) is not ProductOwnedPreEvaluationSemanticSession:
        raise provider_consumer.ProviderEvaluationUniverseError(
            "complete-board denominator requires product-owned pre-evaluation semantic authority"
        )
    if type(pre_evaluation_bound) is not BoundPreEvaluationSession:
        raise provider_consumer.ProviderEvaluationUniverseError(
            "complete-board denominator requires exact bound pre-evaluation session"
        )
    # The product-owned wrapper constructor intentionally accepts the semantic session
    # through isinstance().  A caller can therefore otherwise place a subclass inside
    # an exact wrapper and override slots/semantic properties while retaining a genuine
    # issued product origin.  The nested capability is authority-bearing too: require
    # its exact concrete type only after the outer and bound capability fences pass.
    if type(pre_evaluation_authority.session) is not PreEvaluationSemanticSession:
        raise provider_consumer.ProviderEvaluationUniverseError(
            "complete-board denominator requires exact pre-evaluation semantic session"
        )
    try:
        assert_pre_evaluation_product_origin_authoritative(
            pre_evaluation_authority.origin
        )
    except Exception as exc:
        raise provider_consumer.ProviderEvaluationUniverseError(
            "pre-evaluation semantic origin is not live product-owned authority"
        ) from exc

    context = pre_evaluation_bound.context
    expected_context = (
        provider_consumer._text(session_id, "session_id"),
        provider_consumer._text(campaign_id, "campaign_id"),
        provider_consumer._text(research_protocol_id, "research_protocol_id"),
        provider_consumer._sha(protocol_sha256, "protocol_sha256"),
        snapshot.evidence_sha256,
    )
    actual_context = (
        context.session_id,
        context.campaign_id,
        context.research_protocol_id,
        context.protocol_sha256,
        context.provider_evidence_sha256,
    )
    if actual_context != expected_context:
        raise provider_consumer.ProviderEvaluationUniverseError(
            "pre-evaluation denominator context does not equal exact provider universe context"
        )

    expected_bound_digest = PreEvaluationSemanticAuthority._semantic_bound_authority_digest(
        pre_evaluation_bound
    )
    authority = pre_evaluation_authority
    if (
        authority.origin.bound_authority_digest != expected_bound_digest
        or authority.session.bound_authority_digest != expected_bound_digest
        or authority.origin.denominator_context_digest != context.digest
        or authority.session.denominator_context_digest != context.digest
        or authority.origin.provider_evidence_sha256 != snapshot.evidence_sha256
    ):
        raise provider_consumer.ProviderEvaluationUniverseError(
            "pre-evaluation semantic authority does not bind the supplied denominator origin"
        )

    row_by_key = {row.row_key: row for row in rows}
    if len(row_by_key) != len(rows):
        raise provider_consumer.ProviderEvaluationUniverseError(
            "provider evaluation row_key values must be unique"
        )
    semantic_slots = pre_evaluation_authority.session.slots
    slot_by_key = {slot.row_key: slot for slot in semantic_slots}
    if len(slot_by_key) != len(semantic_slots):
        raise provider_consumer.ProviderEvaluationUniverseError(
            "product-owned semantic authority contains duplicate row keys"
        )
    bound_members = {
        member.row_key: member.member_sha256 for member in pre_evaluation_bound.members
    }
    if set(row_by_key) != set(slot_by_key) or set(slot_by_key) != set(bound_members):
        raise provider_consumer.ProviderEvaluationUniverseError(
            "evaluation rows must exactly equal product-owned pre-evaluation semantic membership"
        )

    evidence: list[dict[str, str]] = []
    for row_key in sorted(row_by_key):
        slot = slot_by_key[row_key]
        if slot.member_sha256 != bound_members[row_key]:
            raise provider_consumer.ProviderEvaluationUniverseError(
                "pre-evaluation semantic member digest does not equal denominator-bound provider member"
            )
        _validate_row_against_semantic_slot(row_by_key[row_key], slot)
        evidence.append(
            {
                "row_key": row_key,
                "member_sha256": slot.member_sha256,
                "provider_selection_sha256": slot.provider_selection_sha256,
                "source_slot_evidence_digest": slot.source_slot_evidence_digest,
                "semantic_evidence_digest": slot.evidence_digest,
            }
        )
    return tuple(evidence)


def _bind_product_semantic_root(
    universe,
    *,
    authority_id: str,
    source_id: str,
    pre_evaluation_authority: ProductOwnedPreEvaluationSemanticSession,
    semantic_evidence: Iterable[dict[str, str]],
):
    from . import provider_evaluation_universe as provider_consumer

    root_sha256 = provider_consumer._digest(
        {
            "kind": "parlay-complete-game-board-evaluation-v3-product-semantic",
            "provider_denominator_root_sha256": universe.intake_snapshot.root_sha256,
            "product_pre_evaluation_authority_id": pre_evaluation_authority.authority_id,
            "product_pre_evaluation_authority_digest": (
                pre_evaluation_authority.authority_digest
            ),
            "product_pre_evaluation_origin_digest": (
                pre_evaluation_authority.origin.origin_digest
            ),
            "product_pre_evaluation_semantic_authority_digest": (
                pre_evaluation_authority.session.authority_digest
            ),
            "denominator_context_digest": (
                pre_evaluation_authority.session.denominator_context_digest
            ),
            "slot_semantic_evidence": list(semantic_evidence),
        }
    )
    intake_snapshot = replace(
        universe.intake_snapshot,
        root_sha256=root_sha256,
    )
    rebound = provider_consumer.EvaluationUniverse._construct(
        intake_snapshot=intake_snapshot,
        universe_id=universe.universe_id,
        campaign_id=universe.campaign_id,
        research_protocol_id=universe.research_protocol_id,
        protocol_sha256=universe.protocol_sha256,
        frozen_at=universe.frozen_at,
        rows=universe.rows,
    )

    # Preserve #638's existing first-save live-issuance fence, but move it from the
    # intermediate provider-only universe to the final semantically-authorized one.
    provider_consumer._ISSUED_UNIVERSES.pop(id(universe), None)
    provider_consumer._remember_issued(
        rebound,
        authority_id=authority_id,
        source_id=source_id,
    )
    return rebound


def _install_gate() -> None:
    from . import provider_evaluation_universe as provider_consumer

    original_build = provider_consumer.build_frozen_universe_from_complete_game_board
    if getattr(original_build, "_product_semantic_origin_required", False):
        return

    def build_frozen_universe_from_complete_game_board(
        *,
        pre_evaluation_authority=None,
        pre_evaluation_bound=None,
        **kwargs,
    ):
        materialized = tuple(kwargs.get("rows", ()))
        kwargs["rows"] = materialized

        if _LEGACY_FIXTURE_BYPASS:
            return original_build(**kwargs)

        if pre_evaluation_authority is None or pre_evaluation_bound is None:
            raise provider_consumer.ProviderEvaluationUniverseError(
                "complete-board denominator requires product-owned pre-evaluation semantic authority"
            )

        snapshot = kwargs.get("snapshot")
        if snapshot is None:
            raise provider_consumer.ProviderEvaluationUniverseError(
                "complete-board denominator requires canonical provider snapshot"
            )

        semantic_evidence = _validate_product_semantic_authority(
            snapshot=snapshot,
            session_id=kwargs.get("session_id"),
            campaign_id=kwargs.get("campaign_id"),
            research_protocol_id=kwargs.get("research_protocol_id"),
            protocol_sha256=kwargs.get("protocol_sha256"),
            rows=materialized,
            pre_evaluation_authority=pre_evaluation_authority,
            pre_evaluation_bound=pre_evaluation_bound,
        )
        universe = original_build(**kwargs)
        return _bind_product_semantic_root(
            universe,
            authority_id=kwargs["authority_id"],
            source_id=snapshot.request.source_id,
            pre_evaluation_authority=pre_evaluation_authority,
            semantic_evidence=semantic_evidence,
        )

    setattr(
        build_frozen_universe_from_complete_game_board,
        "_product_semantic_origin_required",
        True,
    )
    provider_consumer.build_frozen_universe_from_complete_game_board = (
        build_frozen_universe_from_complete_game_board
    )


_install_gate()
del _install_gate

__all__: list[str] = []

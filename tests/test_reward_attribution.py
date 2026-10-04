from __future__ import annotations

from dataclasses import replace

import pytest

import autosport.reward_attribution as reward_attribution_module
from autosport.reward_attribution import (
    AttributionAuthorityRef,
    AttributionTruth,
    REQUIRED_COMPONENTS,
    RewardAttributionComponent,
    RewardAttributionError,
    RewardAttributionEvidence,
    RewardComponentAttribution,
    unknown_reward_attribution,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64


def _ref(
    family: str = "decision-ledger",
    evidence_id: str = "record-1",
    sha: str = SHA_A,
) -> AttributionAuthorityRef:
    return AttributionAuthorityRef(family, evidence_id, sha)


def _unknown() -> RewardAttributionEvidence:
    return unknown_reward_attribution(
        environment_id=SHA_A,
        episode_id=SHA_B,
        action_id=SHA_C,
        outcome_id=SHA_D,
        reward_id=SHA_E,
        transition_id=SHA_F,
    )


def test_unknown_baseline_is_complete_deterministic_and_non_authoritative() -> None:
    evidence = _unknown()

    assert tuple(item.component for item in evidence.components) == REQUIRED_COMPONENTS
    assert all(item.truth is AttributionTruth.UNKNOWN for item in evidence.components)
    assert evidence.source_resolved is False
    assert evidence.policy_update_eligible is False
    assert RewardAttributionEvidence.from_dict(evidence.to_dict()) == evidence
    assert (
        RewardAttributionEvidence.from_dict(evidence.to_dict()).evidence_id
        == evidence.evidence_id
    )

    with pytest.raises(RewardAttributionError, match="contract-only"):
        evidence.require_policy_update_eligible()


def test_unknown_is_not_zero_and_cannot_carry_positive_authority_assertions() -> None:
    with pytest.raises(RewardAttributionError, match="UNKNOWN attribution"):
        RewardComponentAttribution(
            RewardAttributionComponent.EXECUTION,
            AttributionTruth.UNKNOWN,
            authority_refs=(_ref(),),
        )

    with pytest.raises(RewardAttributionError, match="UNKNOWN attribution"):
        RewardComponentAttribution(
            RewardAttributionComponent.DATA_QUALITY,
            AttributionTruth.UNKNOWN,
            counterfactual_ref=_ref("counterfactual"),
        )


def test_observed_and_simulated_claims_require_explicit_evidence_boundaries() -> None:
    with pytest.raises(
        RewardAttributionError,
        match="requires explicit authority_refs",
    ):
        RewardComponentAttribution(
            RewardAttributionComponent.EXECUTION,
            AttributionTruth.OBSERVED,
        )

    observed = RewardComponentAttribution(
        RewardAttributionComponent.EXECUTION,
        AttributionTruth.OBSERVED,
        authority_refs=(_ref("execution", "receipt-1", SHA_B),),
    )
    assert observed.truth is AttributionTruth.OBSERVED

    with pytest.raises(RewardAttributionError, match="counterfactual authority"):
        RewardComponentAttribution(
            RewardAttributionComponent.FORECAST,
            AttributionTruth.SIMULATED,
            authority_refs=(_ref("forecast", "evaluation-1", SHA_C),),
        )

    simulated = RewardComponentAttribution(
        RewardAttributionComponent.FORECAST,
        AttributionTruth.SIMULATED,
        authority_refs=(_ref("forecast", "evaluation-1", SHA_C),),
        counterfactual_ref=_ref("counterfactual", "baseline-1", SHA_D),
    )
    assert simulated.truth is AttributionTruth.SIMULATED

    with pytest.raises(RewardAttributionError, match="OBSERVED attribution"):
        replace(
            observed,
            counterfactual_ref=_ref("counterfactual", "forged", SHA_E),
        )


def test_all_five_components_are_required_once_in_canonical_order() -> None:
    baseline = _unknown()

    with pytest.raises(RewardAttributionError, match="exactly FORECAST"):
        replace(baseline, components=baseline.components[:-1])

    with pytest.raises(RewardAttributionError, match="exactly FORECAST"):
        replace(
            baseline,
            components=(
                baseline.components[1],
                baseline.components[0],
                *baseline.components[2:],
            ),
        )

    with pytest.raises(RewardAttributionError, match="exactly FORECAST"):
        replace(
            baseline,
            components=(
                baseline.components[0],
                baseline.components[0],
                *baseline.components[2:],
            ),
        )


def test_component_lookup_is_typed_and_unknown_remains_explicit() -> None:
    evidence = _unknown()
    execution = evidence.component(RewardAttributionComponent.EXECUTION)
    assert execution.component is RewardAttributionComponent.EXECUTION
    assert execution.truth is AttributionTruth.UNKNOWN

    with pytest.raises(
        RewardAttributionError,
        match="exact RewardAttributionComponent",
    ):
        evidence.component("EXECUTION")  # type: ignore[arg-type]


def test_authority_refs_are_exact_sorted_unique_and_not_source_resolution() -> None:
    first = _ref("a-family", "a", SHA_A)
    second = _ref("b-family", "b", SHA_B)

    with pytest.raises(RewardAttributionError, match="must be sorted"):
        RewardComponentAttribution(
            RewardAttributionComponent.SELECTION,
            AttributionTruth.OBSERVED,
            authority_refs=(second, first),
        )

    with pytest.raises(RewardAttributionError, match="must be unique"):
        RewardComponentAttribution(
            RewardAttributionComponent.SELECTION,
            AttributionTruth.OBSERVED,
            authority_refs=(first, first),
        )

    class ForgedRef(AttributionAuthorityRef):
        pass

    forged = ForgedRef("a-family", "a", SHA_A)
    with pytest.raises(
        RewardAttributionError,
        match="exact AttributionAuthorityRef",
    ):
        RewardComponentAttribution(
            RewardAttributionComponent.SELECTION,
            AttributionTruth.OBSERVED,
            authority_refs=(forged,),
        )


def test_authority_identity_cannot_bind_to_multiple_digests() -> None:
    first = _ref("same-family", "same-id", SHA_A)
    changed_digest = _ref("same-family", "same-id", SHA_B)

    with pytest.raises(
        RewardAttributionError,
        match="one authority identity to multiple digests",
    ):
        RewardComponentAttribution(
            RewardAttributionComponent.FORECAST,
            AttributionTruth.OBSERVED,
            authority_refs=(first, changed_digest),
        )


def test_contract_has_no_monetary_decomposition_surface() -> None:
    raw = _unknown().to_dict()
    raw["components"][0]["monetary_contribution"] = "12.34"

    with pytest.raises(RewardAttributionError, match="keys mismatch"):
        RewardAttributionEvidence.from_dict(raw)


def test_schema_v1_rejects_forged_positive_resolution_or_policy_eligibility() -> None:
    for field in ("source_resolved", "policy_update_eligible"):
        raw = _unknown().to_dict()
        raw[field] = True
        with pytest.raises(RewardAttributionError, match="cannot carry positive"):
            RewardAttributionEvidence.from_dict(raw)


def test_same_causal_reward_has_stable_semantic_key_but_attribution_drift_changes_evidence_id() -> None:
    baseline = _unknown()
    observed_execution = RewardComponentAttribution(
        RewardAttributionComponent.EXECUTION,
        AttributionTruth.OBSERVED,
        authority_refs=(_ref("execution", "paper-ticket-1", SHA_A),),
    )
    changed_components = tuple(
        observed_execution
        if item.component is RewardAttributionComponent.EXECUTION
        else item
        for item in baseline.components
    )
    changed = replace(baseline, components=changed_components)

    assert changed.semantic_key == baseline.semantic_key
    assert changed.evidence_id != baseline.evidence_id
    assert changed.source_resolved is False
    assert changed.policy_update_eligible is False


def test_schema_requires_exact_builtin_string_not_subclass() -> None:
    class ForgedSchema(str):
        pass

    with pytest.raises(
        RewardAttributionError,
        match="unsupported reward attribution schema",
    ):
        replace(_unknown(), schema=ForgedSchema("autosport.reward_component_attribution"))

    raw = _unknown().to_dict()
    raw["schema"] = ForgedSchema("autosport.reward_component_attribution")
    with pytest.raises(
        RewardAttributionError,
        match="unsupported reward attribution schema",
    ):
        RewardAttributionEvidence.from_dict(raw)


def test_schema_version_requires_exact_integer_not_bool() -> None:
    raw = _unknown().to_dict()
    raw["schema_version"] = True
    with pytest.raises(
        RewardAttributionError,
        match="unsupported reward attribution schema",
    ):
        RewardAttributionEvidence.from_dict(raw)

    with pytest.raises(
        RewardAttributionError,
        match="unsupported reward attribution schema",
    ):
        replace(_unknown(), schema_version=True)


def test_digest_and_identity_tampering_fail_closed() -> None:
    raw = _unknown().to_dict()
    raw["semantic_key"] = SHA_A
    with pytest.raises(RewardAttributionError, match="semantic key mismatch"):
        RewardAttributionEvidence.from_dict(raw)

    raw = _unknown().to_dict()
    raw["evidence_id"] = SHA_A
    with pytest.raises(RewardAttributionError, match="evidence digest mismatch"):
        RewardAttributionEvidence.from_dict(raw)

    with pytest.raises(RewardAttributionError, match="lowercase SHA-256"):
        replace(_unknown(), reward_id="not-a-sha")


def test_envelope_rejects_conflicting_digest_for_same_authority_across_components() -> None:
    baseline = _unknown()
    forecast = RewardComponentAttribution(
        RewardAttributionComponent.FORECAST,
        AttributionTruth.OBSERVED,
        authority_refs=(_ref("shared", "e-1", SHA_A),),
    )
    execution = RewardComponentAttribution(
        RewardAttributionComponent.EXECUTION,
        AttributionTruth.OBSERVED,
        authority_refs=(_ref("shared", "e-1", SHA_B),),
    )
    components = tuple(
        forecast
        if item.component is RewardAttributionComponent.FORECAST
        else execution
        if item.component is RewardAttributionComponent.EXECUTION
        else item
        for item in baseline.components
    )

    with pytest.raises(
        RewardAttributionError,
        match="envelope cannot bind one authority identity to multiple digests",
    ):
        replace(baseline, components=components)


def test_envelope_rejects_conflict_between_factual_and_counterfactual_ref() -> None:
    baseline = _unknown()
    simulated = RewardComponentAttribution(
        RewardAttributionComponent.FORECAST,
        AttributionTruth.SIMULATED,
        authority_refs=(_ref("shared", "e-1", SHA_A),),
        counterfactual_ref=_ref("shared", "e-1", SHA_B),
    )
    components = tuple(
        simulated if item.component is RewardAttributionComponent.FORECAST else item
        for item in baseline.components
    )

    with pytest.raises(
        RewardAttributionError,
        match="envelope cannot bind one authority identity to multiple digests",
    ):
        replace(baseline, components=components)


def test_envelope_allows_same_authority_identity_with_same_digest_reuse() -> None:
    baseline = _unknown()
    shared = _ref("shared", "e-1", SHA_A)
    forecast = RewardComponentAttribution(
        RewardAttributionComponent.FORECAST,
        AttributionTruth.OBSERVED,
        authority_refs=(shared,),
    )
    execution = RewardComponentAttribution(
        RewardAttributionComponent.EXECUTION,
        AttributionTruth.OBSERVED,
        authority_refs=(shared,),
    )
    components = tuple(
        forecast
        if item.component is RewardAttributionComponent.FORECAST
        else execution
        if item.component is RewardAttributionComponent.EXECUTION
        else item
        for item in baseline.components
    )

    evidence = replace(baseline, components=components)
    assert evidence.component(RewardAttributionComponent.FORECAST).authority_refs == (shared,)
    assert evidence.component(RewardAttributionComponent.EXECUTION).authority_refs == (shared,)
    assert evidence.source_resolved is False
    assert evidence.policy_update_eligible is False

def test_module_global_type_rebind_cannot_redefine_canonical_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical_ref = _ref("canonical", "evidence-1", SHA_A)

    class ForgedRef:
        def __init__(self) -> None:
            self.family = "forged"
            self.evidence_id = "forged"
            self.sha256 = SHA_A

    monkeypatch.setattr(
        reward_attribution_module,
        "AttributionAuthorityRef",
        ForgedRef,
    )
    with pytest.raises(
        RewardAttributionError,
        match="exact AttributionAuthorityRef",
    ):
        RewardComponentAttribution(
            RewardAttributionComponent.EXECUTION,
            AttributionTruth.OBSERVED,
            authority_refs=(ForgedRef(),),  # type: ignore[arg-type]
        )

    class ForgedComponent:
        pass

    monkeypatch.setattr(
        reward_attribution_module,
        "RewardAttributionComponent",
        ForgedComponent,
    )
    with pytest.raises(
        RewardAttributionError,
        match="exact RewardAttributionComponent",
    ):
        RewardComponentAttribution(
            ForgedComponent(),  # type: ignore[arg-type]
            AttributionTruth.UNKNOWN,
        )

    accepted = RewardComponentAttribution(
        RewardAttributionComponent.EXECUTION,
        AttributionTruth.OBSERVED,
        authority_refs=(canonical_ref,),
    )
    assert accepted.authority_refs == (canonical_ref,)


def test_module_digest_rebind_cannot_redirect_canonical_evidence_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    evidence = _unknown()
    semantic_key = evidence.semantic_key
    evidence_id = evidence.evidence_id

    monkeypatch.setattr(
        reward_attribution_module,
        "_digest",
        lambda payload: SHA_A,
    )

    class ForgedJson:
        @staticmethod
        def dumps(*args: object, **kwargs: object) -> str:
            return "forged"

    class ForgedHashlib:
        @staticmethod
        def sha256(payload: bytes) -> object:
            raise AssertionError("forged sha256 must not execute")

    monkeypatch.setattr(reward_attribution_module, "json", ForgedJson)
    monkeypatch.setattr(reward_attribution_module, "hashlib", ForgedHashlib)

    assert evidence.semantic_key == semantic_key
    assert evidence.evidence_id == evidence_id
    assert _unknown().semantic_key == semantic_key
    assert _unknown().evidence_id == evidence_id


def test_parser_helper_rebind_cannot_bypass_exact_schema_or_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canonical = _unknown()
    raw = canonical.to_dict()
    raw["unexpected"] = "forged"

    monkeypatch.setattr(
        reward_attribution_module,
        "_exact_keys",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        reward_attribution_module,
        "_sha256",
        lambda value, name: (
            canonical.semantic_key if name == "semantic_key" else canonical.evidence_id
        ),
    )

    with pytest.raises(RewardAttributionError, match="keys mismatch"):
        RewardAttributionEvidence.from_dict(raw)

    raw = canonical.to_dict()
    raw["semantic_key"] = "0" * 64
    with pytest.raises(RewardAttributionError, match="semantic key mismatch"):
        RewardAttributionEvidence.from_dict(raw)

    component_raw = canonical.components[0].to_dict()
    component_raw["unexpected"] = "forged"
    with pytest.raises(RewardAttributionError, match="keys mismatch"):
        RewardComponentAttribution.from_dict(component_raw)

def test_serialization_dispatch_rebind_cannot_change_immutable_evidence_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = RewardComponentAttribution(
        RewardAttributionComponent.EXECUTION,
        AttributionTruth.OBSERVED,
        authority_refs=(_ref("execution", "receipt-1", SHA_A),),
    )
    baseline = _unknown()
    evidence = replace(
        baseline,
        components=tuple(
            observed
            if item.component is RewardAttributionComponent.EXECUTION
            else item
            for item in baseline.components
        ),
    )
    canonical_payload = evidence.payload()
    canonical_semantic_key = evidence.semantic_key
    canonical_evidence_id = evidence.evidence_id
    canonical_dict = evidence.to_dict()

    monkeypatch.setattr(
        AttributionAuthorityRef,
        "to_dict",
        lambda self: {"forged": True},
    )
    monkeypatch.setattr(
        RewardComponentAttribution,
        "to_dict",
        lambda self: {"forged": True},
    )
    monkeypatch.setattr(
        RewardAttributionEvidence,
        "payload",
        lambda self: {"forged": True},
    )
    monkeypatch.setattr(
        reward_attribution_module,
        "_reward_attribution_semantic_payload",
        lambda evidence: {"forged": True},
    )
    monkeypatch.setattr(
        reward_attribution_module,
        "_reward_attribution_payload",
        lambda evidence: {"forged": True},
    )

    assert evidence.semantic_key == canonical_semantic_key
    assert evidence.evidence_id == canonical_evidence_id
    assert evidence.to_dict() == canonical_dict
    assert canonical_payload["components"][3]["authority_refs"][0] == {
        "family": "execution",
        "evidence_id": "receipt-1",
        "sha256": SHA_A,
    }


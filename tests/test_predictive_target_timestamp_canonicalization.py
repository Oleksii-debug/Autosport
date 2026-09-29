from __future__ import annotations

from autosport.predictive_target_semantics import (
    PredictiveTargetContract,
    _ordered_points,
    _target_population_digest,
)
from autosport.strategy_model_factory import (
    TrainingPoint,
    training_points_manifest_sha256,
)


SHA_A = "a" * 64
SHA_B = "b" * 64


def _contract(*, frozen_at: str) -> PredictiveTargetContract:
    return PredictiveTargetContract(
        contract_id="binary-settlement-target-v1",
        research_protocol_id="protocol-target-v1",
        target_semantics_id="selection-settlement-win-v1",
        positive_outcome_definition_sha256=SHA_A,
        negative_outcome_definition_sha256=SHA_B,
        frozen_at=frozen_at,
    )


def _points(*, offset_aliases: bool) -> tuple[TrainingPoint, ...]:
    if offset_aliases:
        return (
            TrainingPoint(
                "2026-01-01T19:00:00-05:00",
                0.50,
                1.0,
                target_available_at="2026-01-02T00:00:00Z",
                evidence_sha256s=(SHA_A,),
            ),
            TrainingPoint(
                "2026-01-02T19:00:00-05:00",
                0.40,
                0.0,
                target_available_at="2026-01-02T20:00:00-05:00",
                evidence_sha256s=(SHA_B,),
            ),
        )
    return (
        TrainingPoint(
            "2026-01-02T00:00:00+00:00",
            0.50,
            1.0,
            target_available_at="2026-01-02T00:00:00+00:00",
            evidence_sha256s=(SHA_A,),
        ),
        TrainingPoint(
            "2026-01-03T00:00:00+00:00",
            0.40,
            0.0,
            target_available_at="2026-01-03T01:00:00+00:00",
            evidence_sha256s=(SHA_B,),
        ),
    )


def test_contract_digest_uses_semantic_freeze_instant_identity() -> None:
    canonical = _contract(frozen_at="2026-01-01T00:00:00+00:00")
    zulu = _contract(frozen_at="2026-01-01T00:00:00Z")
    offset = _contract(frozen_at="2025-12-31T19:00:00-05:00")

    assert zulu.contract_sha256 == canonical.contract_sha256
    assert offset.contract_sha256 == canonical.contract_sha256
    assert zulu.preregistration_token == canonical.preregistration_token
    assert offset.preregistration_token == canonical.preregistration_token


def test_target_population_digest_uses_same_instant_identity_as_training_manifest() -> None:
    canonical = _points(offset_aliases=False)
    aliases = _points(offset_aliases=True)

    canonical_manifest = training_points_manifest_sha256(canonical)
    alias_manifest = training_points_manifest_sha256(aliases)
    assert alias_manifest == canonical_manifest

    contract = _contract(frozen_at="2026-01-01T00:00:00+00:00")

    canonical_target = _target_population_digest(
        contract_sha256=contract.contract_sha256,
        training_manifest_sha256=canonical_manifest,
        ordered_points=_ordered_points(canonical),
    )
    alias_target = _target_population_digest(
        contract_sha256=contract.contract_sha256,
        training_manifest_sha256=alias_manifest,
        ordered_points=_ordered_points(aliases),
    )

    assert alias_target == canonical_target

from __future__ import annotations

import pytest

from autosport.scientific_registry import (
    EvaluationBundleRef,
    PromotionAction,
    PromotionDecision,
    PromotionEvidenceError,
    ResearchOutcome,
    ScientificRegistry,
)
from test_scientific_registry import T3, _experiment, _foundation, _promotion_evidence


def _sha(char: str) -> str:
    return char * 64


def _bundle(*, promotion_shaped: bool) -> EvaluationBundleRef:
    kwargs: dict[str, object] = {}
    if promotion_shaped:
        kwargs.update(
            effect_interval_low="0.1",
            effect_interval_high="0.3",
            practical_improvement="0.2",
        )
    return EvaluationBundleRef(
        evaluation_bundle_id=(
            "evaluation-ungrounded-effective-sample"
            if promotion_shaped
            else "evaluation-diagnostic-effective-sample"
        ),
        bundle_sha256=_sha("a"),
        evaluator_source_sha256=_sha("b"),
        dataset_snapshot_id="dataset-confirmation",
        protocol_sha256=_sha("c"),
        artifact_hashes=(_sha("d"),),
        created_at="2026-09-21T00:00:00Z",
        evaluated_strategy_version_id="strategy-challenger",
        evaluated_model_version_id="model-challenger",
        effective_sample_size=1_000_000,
        **kwargs,
    )


def test_promotion_effective_sample_requires_canonical_scientific_lineage(tmp_path) -> None:
    """Absorb #1318's pristine-registry falsifier without minting ESS authority."""

    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )

    with pytest.raises(ValueError, match="canonical scientific lineage"):
        registry.append(_bundle(promotion_shaped=True))


def test_diagnostic_effective_sample_without_promotion_interval_remains_compatible(
    tmp_path,
) -> None:
    """Do not turn the promotion fence into a ban on descriptive ESS metadata."""

    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )
    bundle = _bundle(promotion_shaped=False)

    record_sha256 = registry.append(bundle)
    durable = registry.get("EvaluationBundle", bundle.evaluation_bundle_id)

    assert durable is not None
    assert durable.record_sha256 == record_sha256
    assert durable.payload["effective_sample_size"] == 1_000_000
    assert "effect_interval_low" not in durable.payload


def test_private_append_path_cannot_bypass_promotion_lineage(tmp_path) -> None:
    """The invariant belongs to ScientificRegistry, not one public wrapper."""

    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry-private-append.json"
    )

    with pytest.raises(ValueError, match="canonical scientific lineage"):
        registry._append(_bundle(promotion_shaped=True))


def test_valid_looking_caller_effective_sample_cannot_promote(tmp_path) -> None:
    """Durable matching scalars are not product-issued dependence/ESS authority."""

    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry-positive-promotion.json"
    )
    foundation = _foundation(registry)
    registry.append(_experiment(outcome=ResearchOutcome.POSITIVE))
    evidence = _promotion_evidence(
        experiment_id="experiment-1",
        strategy_id="strategy-1",
        model_id="model-1",
        bundle_id="eval-1",
        dataset_id="dataset-1",
        protocol_id="protocol-1",
        bundle_sha=foundation["bundle"].bundle_sha256,
        evidence_id="caller-ess-positive-authority",
        rollback_identity="NONE",
    )
    registry.append(evidence)
    decision = PromotionDecision(
        "promotion-caller-ess-authority",
        PromotionAction.PROMOTE,
        "strategy-1",
        "protocol-1",
        foundation["protocol"].protocol_sha256,
        "eval-1",
        foundation["bundle"].bundle_sha256,
        T3,
        candidate_model_version_id="model-1",
        promotion_evidence_id=evidence.promotion_evidence_id,
    )

    with pytest.raises(
        PromotionEvidenceError,
        match="product-issued effective-sample/dependence authority",
    ):
        registry.record_promotion(decision)

    assert registry.get("PromotionDecision", decision.promotion_decision_id) is None
    assert registry.champion_strategy(as_of=T3) is None

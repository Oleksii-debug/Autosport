from __future__ import annotations

import pytest

from autosport.scientific_registry import EvaluationBundleRef, ScientificRegistry


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


def test_promotion_effective_sample_requires_dependence_evidence_authority(tmp_path) -> None:
    """Absorb #1318: caller-minted promotion ESS must not become durable truth."""

    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )

    with pytest.raises(
        ValueError,
        match="raw-sample/dependence/cluster derivation authority",
    ):
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

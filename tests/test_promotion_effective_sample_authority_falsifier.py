from __future__ import annotations

import pytest

from autosport.scientific_registry import EvaluationBundleRef, ScientificRegistry


def _sha(char: str) -> str:
    return char * 64


def test_promotion_effective_sample_requires_dependence_evidence_authority(tmp_path) -> None:
    """A caller integer must not become durable promotion-grade effective sample truth.

    #367 requires promotion evidence to retain raw/effective sample evidence,
    cluster membership and the assumptions used to derive effective N.  The
    current EvaluationBundleRef surface accepts a positive effective_sample_size
    without any corresponding product-owned dependence/cluster authority.

    The safe contract may reject construction or durable publication until that
    authority is supplied.  This falsifier deliberately does not prescribe a
    particular clustering estimator or a second registry.
    """

    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )

    def persist_ungrounded_effective_sample() -> None:
        bundle = EvaluationBundleRef(
            evaluation_bundle_id="evaluation-ungrounded-effective-sample",
            bundle_sha256=_sha("a"),
            evaluator_source_sha256=_sha("b"),
            dataset_snapshot_id="dataset-confirmation",
            protocol_sha256=_sha("c"),
            artifact_hashes=(_sha("d"),),
            created_at="2026-09-21T00:00:00Z",
            evaluated_strategy_version_id="strategy-challenger",
            evaluated_model_version_id="model-challenger",
            effective_sample_size=1_000_000,
            effect_interval_low="0.1",
            effect_interval_high="0.3",
            practical_improvement="0.2",
        )
        registry.append(bundle)

    with pytest.raises((TypeError, ValueError)):
        persist_ungrounded_effective_sample()

"""Fail closed on promotion-shaped effective-sample assertions without authority.

ScientificRegistry/EvaluationBundleRef are general reproducibility surfaces.  A
caller-provided effective_sample_size can therefore remain useful descriptive
metadata, but it must not become promotion-grade evidence merely because the same
bundle also carries an effect interval and practical improvement.

#367 requires product-owned raw/effective sample evidence, cluster membership and
the assumptions used to derive effective N.  No canonical product-owned derivation
authority for that contract exists yet.  Until it does, promotion-shaped bundles
fail closed at durable publication while ordinary evaluation bundles retain their
existing compatibility.

This guard reuses the canonical ScientificRegistry and does not create a second
registry, estimator, promotion engine, or financial authority.
"""

from __future__ import annotations

from . import scientific_registry as _registry

_ORIGINAL_APPEND = _registry.ScientificRegistry.append


def _is_promotion_shaped_bundle(record: object) -> bool:
    if type(record) is not _registry.EvaluationBundleRef:
        return False
    return (
        record.effective_sample_size is not None
        and record.effect_interval_low is not None
        and record.effect_interval_high is not None
        and record.practical_improvement is not None
    )


def _append_with_promotion_effective_sample_authority(
    self: _registry.ScientificRegistry,
    record: _registry.ScientificRecord,
    *,
    allow_repeat_experiment: bool = False,
) -> str:
    if _is_promotion_shaped_bundle(record):
        raise _registry.PromotionEvidenceError(
            "promotion-shaped EvaluationBundle effective sample lacks canonical "
            "raw-sample/dependence/cluster derivation authority"
        )
    return _ORIGINAL_APPEND(
        self,
        record,
        allow_repeat_experiment=allow_repeat_experiment,
    )


_registry.ScientificRegistry.append = _append_with_promotion_effective_sample_authority

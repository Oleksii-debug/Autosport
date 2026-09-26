"""Fail closed on orphan promotion-shaped effective-sample assertions.

EvaluationBundleRef is a general reproducibility surface. Existing canonical
scientific flows durably retain effective-sample and interval metadata there, so
this guard must not pretend that persistence itself is positive promotion authority.

The immediate #1318 gap is narrower: a caller can otherwise publish a promotion-
shaped bundle into a pristine registry without even the durable dataset/protocol/
strategy/model lineage that the bundle claims to summarize. Require that canonical,
causally prior lineage before publication. This is a prerequisite only: #367's
product-owned raw/effective-sample, dependence/cluster membership and derivation-
assumption authority remains intentionally unimplemented and therefore cannot be
claimed from this guard.

This reuses ScientificRegistry; it creates no second registry, estimator, promotion
engine, or financial authority.
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


def _require_canonical_scientific_lineage(
    registry: _registry.ScientificRegistry,
    bundle: _registry.EvaluationBundleRef,
) -> None:
    bundle_at = _registry._instant(bundle.created_at, "EvaluationBundle.created_at")

    dataset = registry.get("DatasetSnapshot", bundle.dataset_snapshot_id)
    if dataset is None:
        raise ValueError(
            "promotion-shaped EvaluationBundle lacks canonical scientific lineage: "
            "DatasetSnapshot is missing"
        )
    if _registry._instant(dataset.available_at, "DatasetSnapshot.available_at") > bundle_at:
        raise ValueError(
            "promotion-shaped EvaluationBundle predates its DatasetSnapshot lineage"
        )

    protocols = tuple(
        entry
        for entry in registry.causal_records("ResearchProtocol", as_of=bundle.created_at)
        if entry.payload.get("protocol_sha256") == bundle.protocol_sha256.lower()
    )
    if len(protocols) != 1:
        raise ValueError(
            "promotion-shaped EvaluationBundle lacks one canonical causal ResearchProtocol"
        )

    if bundle.evaluated_strategy_version_id is not None:
        strategy = registry.get("StrategyVersion", bundle.evaluated_strategy_version_id)
        if strategy is None:
            raise ValueError(
                "promotion-shaped EvaluationBundle lacks canonical StrategyVersion lineage"
            )
        if _registry._instant(strategy.available_at, "StrategyVersion.available_at") > bundle_at:
            raise ValueError(
                "promotion-shaped EvaluationBundle predates its StrategyVersion lineage"
            )
        if (
            bundle.evaluated_model_version_id is not None
            and strategy.payload.get("model_version_id") != bundle.evaluated_model_version_id
        ):
            raise ValueError(
                "promotion-shaped EvaluationBundle strategy/model lineage mismatch"
            )

    if bundle.evaluated_model_version_id is not None:
        model = registry.get("ModelVersion", bundle.evaluated_model_version_id)
        if model is None:
            raise ValueError(
                "promotion-shaped EvaluationBundle lacks canonical ModelVersion lineage"
            )
        if _registry._instant(model.available_at, "ModelVersion.available_at") > bundle_at:
            raise ValueError(
                "promotion-shaped EvaluationBundle predates its ModelVersion lineage"
            )
        if model.payload.get("dataset_snapshot_id") != bundle.dataset_snapshot_id:
            raise ValueError(
                "promotion-shaped EvaluationBundle model/dataset lineage mismatch"
            )
        if model.payload.get("research_protocol_id") != protocols[0].record_id:
            raise ValueError(
                "promotion-shaped EvaluationBundle model/protocol lineage mismatch"
            )


def _append_with_promotion_effective_sample_authority(
    self: _registry.ScientificRegistry,
    record: _registry.ScientificRecord,
    *,
    allow_repeat_experiment: bool = False,
) -> str:
    if _is_promotion_shaped_bundle(record):
        _require_canonical_scientific_lineage(self, record)
    return _ORIGINAL_APPEND(
        self,
        record,
        allow_repeat_experiment=allow_repeat_experiment,
    )


_registry.ScientificRegistry.append = _append_with_promotion_effective_sample_authority

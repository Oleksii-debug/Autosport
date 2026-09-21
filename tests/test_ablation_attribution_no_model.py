from __future__ import annotations

import hashlib

from autosport.ablation_attribution import (
    AblationAttributionStatus,
    AblationAuthoritySnapshot,
    AblationComponent,
    derive_scientific_core_authorities,
    evaluate_ablation_attribution,
)
from autosport.scientific_registry import RegistryEntry


def h(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class FakeRegistry:
    def __init__(self, entries: list[RegistryEntry]):
        self.entries = {(entry.record_type, entry.record_id): entry for entry in entries}

    def get(self, record_type: str, record_id: str):
        return self.entries.get((record_type, record_id))


def e(kind: str, identity: str, payload: dict) -> RegistryEntry:
    return RegistryEntry(kind, identity, "2026-09-21T08:00:00Z", payload, h(f"{kind}:{identity}"))


def test_no_model_identity_is_stable_across_data_ablation():
    protocol_sha = h("protocol")
    strategy_payload = {
        "canonical_strategy_id": "rule-strategy",
        "source_sha256": h("strategy-source"),
        "environment_sha256": h("strategy-env"),
        "config_sha256": h("strategy-config"),
        "model_version_id": None,
    }
    registry = FakeRegistry(
        [
            e("ResearchProtocol", "rp", {"protocol_sha256": protocol_sha, "binding": {"hypothesis_id": "h"}}),
            e("Hypothesis", "h", {"primary_metric": "net_profit"}),
            e("DatasetSnapshot", "eval-a", {"manifest_sha256": h("a")}),
            e("DatasetSnapshot", "eval-b", {"manifest_sha256": h("b")}),
            e("FeatureSet", "f", {"definition_sha256": h("f")}),
            e("StrategyVersion", "s", strategy_payload),
            e("Experiment", "a", {"research_protocol_id": "rp", "dataset_snapshot_id": "eval-a", "feature_set_id": "f", "strategy_version_id": "s", "evaluation_bundle_id": "ea", "model_version_id": None, "seed": 7}),
            e("Experiment", "b", {"research_protocol_id": "rp", "dataset_snapshot_id": "eval-b", "feature_set_id": "f", "strategy_version_id": "s", "evaluation_bundle_id": "eb", "model_version_id": None, "seed": 7}),
            e("EvaluationBundle", "ea", {"protocol_sha256": protocol_sha, "dataset_snapshot_id": "eval-a", "evaluated_strategy_version_id": "s", "evaluated_model_version_id": None, "evaluator_source_sha256": h("evaluator")}),
            e("EvaluationBundle", "eb", {"protocol_sha256": protocol_sha, "dataset_snapshot_id": "eval-b", "evaluated_strategy_version_id": "s", "evaluated_model_version_id": None, "evaluator_source_sha256": h("evaluator")}),
        ]
    )
    ca = derive_scientific_core_authorities(registry=registry, experiment_id="a")
    cb = derive_scientific_core_authorities(registry=registry, experiment_id="b")
    assert ca is not None and cb is not None
    assert ca.model_sha256 == cb.model_sha256

    def snapshot(core, label):
        return AblationAuthoritySnapshot(
            data_sha256=core.data_sha256,
            model_sha256=core.model_sha256,
            threshold_sha256=h("threshold"),
            sizing_sha256=h("sizing"),
            execution_sha256=h("execution"),
            source_evidence_sha256=h(label),
        )

    result = evaluate_ablation_attribution(
        registry=registry,
        control_experiment_id="a",
        treatment_experiment_id="b",
        declared_component=AblationComponent.DATA,
        control_authorities=snapshot(ca, "a"),
        treatment_authorities=snapshot(cb, "b"),
    )
    assert result.status is AblationAttributionStatus.ATTRIBUTABLE
    assert result.changed_components == (AblationComponent.DATA,)

from __future__ import annotations

import hashlib

import pytest

from autosport.ablation_attribution import (
    AblationAttributionStatus,
    AblationAuthoritySnapshot,
    AblationComponent,
    derive_scientific_core_authorities,
    evaluate_ablation_attribution,
)
from autosport.scientific_registry import RegistryEntry


def h(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def r(kind: str, ident: str, payload: dict) -> RegistryEntry:
    return RegistryEntry(kind, ident, "2026-09-21T08:00:00Z", payload, h(f"{kind}:{ident}"))


class Registry:
    def __init__(self, rows: list[RegistryEntry]):
        self.rows = {(x.record_type, x.record_id): x for x in rows}

    def get(self, kind: str, ident: str):
        return self.rows.get((kind, ident))


def pair(
    *,
    eval_dataset: str = "eval-a",
    treatment_model: str | None = "m2",
    treatment_strategy: str = "s2",
    strategy_config: str = "strategy-config",
    experiment_config: str = "experiment-config",
    evaluator: str = "evaluator",
    treatment_evaluator: str | None = None,
) -> Registry:
    psha = h("protocol")
    treatment_evaluator = treatment_evaluator or evaluator
    rows = [
        r("ResearchProtocol", "rp", {"protocol_sha256": psha, "binding": {"hypothesis_id": "hyp"}}),
        r("Hypothesis", "hyp", {"primary_metric": "net_profit"}),
        r("DatasetSnapshot", "eval-a", {"manifest_sha256": h("eval-a")}),
        r("DatasetSnapshot", "train-a", {"manifest_sha256": h("train-a")}),
        r("DatasetSnapshot", "train-b", {"manifest_sha256": h("train-b")}),
        r("FeatureSet", "f", {"definition_sha256": h("f")}),
        r("ModelVersion", "m1", {"artifact_sha256": h("m1"), "dataset_snapshot_id": "train-a", "feature_set_id": "f", "research_protocol_id": "rp"}),
        r("StrategyVersion", "s1", {"canonical_strategy_id": "strategy", "source_sha256": h("source"), "environment_sha256": h("env"), "config_sha256": h("strategy-config"), "model_version_id": "m1"}),
        r("Experiment", "control", {"research_protocol_id": "rp", "dataset_snapshot_id": "eval-a", "feature_set_id": "f", "strategy_version_id": "s1", "evaluation_bundle_id": "e1", "model_version_id": "m1", "seed": 7, "config_sha256": h("experiment-config")}),
        r("EvaluationBundle", "e1", {"protocol_sha256": psha, "dataset_snapshot_id": "eval-a", "evaluated_strategy_version_id": "s1", "evaluated_model_version_id": "m1", "evaluator_source_sha256": h(evaluator)}),
    ]
    if eval_dataset != "eval-a":
        rows.append(r("DatasetSnapshot", eval_dataset, {"manifest_sha256": h(eval_dataset)}))
    if treatment_model is not None and treatment_model != "m1":
        rows.append(r("ModelVersion", treatment_model, {"artifact_sha256": h(treatment_model), "dataset_snapshot_id": "train-b", "feature_set_id": "f", "research_protocol_id": "rp"}))
    if treatment_strategy != "s1":
        rows.append(r("StrategyVersion", treatment_strategy, {"canonical_strategy_id": "strategy", "source_sha256": h("source"), "environment_sha256": h("env"), "config_sha256": h(strategy_config), "model_version_id": treatment_model}))
    rows.extend([
        r("Experiment", "treatment", {"research_protocol_id": "rp", "dataset_snapshot_id": eval_dataset, "feature_set_id": "f", "strategy_version_id": treatment_strategy, "evaluation_bundle_id": "e2", "model_version_id": treatment_model, "seed": 7, "config_sha256": h(experiment_config)}),
        r("EvaluationBundle", "e2", {"protocol_sha256": psha, "dataset_snapshot_id": eval_dataset, "evaluated_strategy_version_id": treatment_strategy, "evaluated_model_version_id": treatment_model, "evaluator_source_sha256": h(treatment_evaluator)}),
    ])
    return Registry(rows)


def snap(registry: Registry, experiment: str, *, threshold="t", sizing="s", execution="e", missing=None):
    core = derive_scientific_core_authorities(registry=registry, experiment_id=experiment)
    assert core is not None
    values = dict(
        data_sha256=core.data_sha256,
        model_sha256=core.model_sha256,
        threshold_sha256=h(threshold),
        sizing_sha256=h(sizing),
        execution_sha256=h(execution),
        source_evidence_sha256=h("source:" + experiment),
    )
    if missing:
        values[missing + "_sha256"] = None
    return AblationAuthoritySnapshot(**values)


def evaluate(registry: Registry, component=AblationComponent.MODEL, left=None, right=None):
    return evaluate_ablation_attribution(
        registry=registry,
        control_experiment_id="control",
        treatment_experiment_id="treatment",
        declared_component=component,
        control_authorities=left or snap(registry, "control"),
        treatment_authorities=right or snap(registry, "treatment"),
    )


def test_exact_model_only_delta_is_attributable_but_never_authorizes_actions():
    registry = pair()
    result = evaluate(registry)
    assert result.status is AblationAttributionStatus.ATTRIBUTABLE
    assert result.changed_components == (AblationComponent.MODEL,)
    assert result.reason_codes == ("EXACT_ONE_FACTOR_DELTA",)
    assert result.estimand == "net_profit"
    assert not result.promotion_authorized
    assert not result.learning_update_authorized
    assert not result.execution_authorized
    assert result.attribution_id == evaluate(registry).attribution_id


def test_evaluation_data_can_change_without_changing_model_training_lineage():
    registry = pair(eval_dataset="eval-b", treatment_model="m1", treatment_strategy="s1")
    result = evaluate(registry, AblationComponent.DATA)
    assert result.status is AblationAttributionStatus.ATTRIBUTABLE
    assert result.changed_components == (AblationComponent.DATA,)


def test_experiment_config_is_part_of_one_factor_reproducibility_identity():
    registry = pair(experiment_config="changed")
    result = evaluate(registry)
    assert result.status is AblationAttributionStatus.UNATTRIBUTED
    assert "EXPERIMENT_CONFIG_MISMATCH" in result.reason_codes


def test_non_model_strategy_change_blocks_model_attribution():
    registry = pair(strategy_config="changed")
    result = evaluate(registry)
    assert result.status is AblationAttributionStatus.UNATTRIBUTED
    assert "NON_MODEL_STRATEGY_AUTHORITY_MISMATCH" in result.reason_codes


def test_evaluator_identity_mismatch_blocks_attribution():
    registry = pair(treatment_evaluator="other")
    result = evaluate(registry)
    assert result.status is AblationAttributionStatus.UNATTRIBUTED
    assert "EVALUATOR_IDENTITY_MISMATCH" in result.reason_codes


def test_caller_cannot_forge_registry_owned_model_fingerprint():
    registry = pair()
    left = snap(registry, "control")
    forged = AblationAuthoritySnapshot(
        data_sha256=left.data_sha256,
        model_sha256=h("forged"),
        threshold_sha256=left.threshold_sha256,
        sizing_sha256=left.sizing_sha256,
        execution_sha256=left.execution_sha256,
        source_evidence_sha256=h("forged-source"),
    )
    result = evaluate(registry, left=forged)
    assert result.status is AblationAttributionStatus.UNATTRIBUTED
    assert "CONTROL_MODEL_AUTHORITY_MISMATCH" in result.reason_codes


@pytest.mark.parametrize("component,field", [
    (AblationComponent.THRESHOLD, "threshold"),
    (AblationComponent.SIZING, "sizing"),
    (AblationComponent.EXECUTION, "execution"),
])
def test_caller_hashes_cannot_mint_non_model_subauthority(component, field):
    registry = pair(treatment_model="m1", treatment_strategy="s1")
    kwargs = {field: "changed"}
    result = evaluate(registry, component, right=snap(registry, "treatment", **kwargs))
    assert result.status is AblationAttributionStatus.UNATTRIBUTED
    assert result.changed_components == (component,)
    assert "COMPONENT_SUBAUTHORITY_UNRESOLVED" in result.reason_codes


def test_multiple_component_delta_is_not_one_factor_attribution():
    registry = pair()
    result = evaluate(registry, right=snap(registry, "treatment", sizing="changed"))
    assert result.status is AblationAttributionStatus.UNATTRIBUTED
    assert result.changed_components == (AblationComponent.MODEL, AblationComponent.SIZING)
    assert "NOT_EXACT_ONE_FACTOR_DELTA" in result.reason_codes


def test_missing_component_witness_fails_closed():
    registry = pair()
    result = evaluate(registry, left=snap(registry, "control", missing="execution"))
    assert result.status is AblationAttributionStatus.UNATTRIBUTED
    assert "COMPONENT_AUTHORITY_MISSING" in result.reason_codes


def test_invalid_model_training_reference_makes_core_authority_unresolvable():
    registry = pair()
    model = registry.rows[("ModelVersion", "m2")]
    registry.rows[("ModelVersion", "m2")] = RegistryEntry(
        model.record_type, model.record_id, model.available_at,
        {**model.payload, "dataset_snapshot_id": "missing-training-data"},
        model.record_sha256,
    )
    assert derive_scientific_core_authorities(registry=registry, experiment_id="treatment") is None


def test_no_model_identity_is_stable_for_rule_strategy_data_ablation():
    registry = pair(eval_dataset="eval-b", treatment_model="m1", treatment_strategy="s1")
    for kind, ident, field in (
        ("StrategyVersion", "s1", "model_version_id"),
        ("Experiment", "control", "model_version_id"),
        ("Experiment", "treatment", "model_version_id"),
        ("EvaluationBundle", "e1", "evaluated_model_version_id"),
        ("EvaluationBundle", "e2", "evaluated_model_version_id"),
    ):
        row = registry.rows[(kind, ident)]
        registry.rows[(kind, ident)] = RegistryEntry(
            row.record_type, row.record_id, row.available_at,
            {**row.payload, field: None}, row.record_sha256,
        )
    left, right = snap(registry, "control"), snap(registry, "treatment")
    assert left.model_sha256 == right.model_sha256
    result = evaluate(registry, AblationComponent.DATA, left, right)
    assert result.status is AblationAttributionStatus.ATTRIBUTABLE
    assert result.changed_components == (AblationComponent.DATA,)

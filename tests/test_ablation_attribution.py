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
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class FakeRegistry:
    def __init__(self, entries: list[RegistryEntry]):
        self._entries = {(entry.record_type, entry.record_id): entry for entry in entries}

    def get(self, record_type: str, record_id: str):
        return self._entries.get((record_type, record_id))


def entry(kind: str, identity: str, payload: dict) -> RegistryEntry:
    return RegistryEntry(
        kind,
        identity,
        "2026-09-21T08:00:00Z",
        payload,
        h(f"{kind}:{identity}"),
    )


def registry_pair(
    *,
    evaluator_control: str | None = None,
    evaluator_treatment: str | None = None,
    treatment_protocol_id: str = "rp1",
    treatment_dataset: str = "ds1",
    treatment_model: str = "m2",
    treatment_strategy: str = "s2",
    treatment_eval_dataset: str | None = None,
    treatment_strategy_config: str = "strategy-config",
    treatment_strategy_source: str = "strategy-source",
    treatment_strategy_environment: str = "strategy-env",
) -> FakeRegistry:
    protocol_sha = h("protocol")
    evaluator_control = evaluator_control or h("evaluator")
    evaluator_treatment = evaluator_treatment or h("evaluator")
    treatment_eval_dataset = treatment_eval_dataset or treatment_dataset
    entries = [
        entry(
            "ResearchProtocol",
            "rp1",
            {"protocol_sha256": protocol_sha, "binding": {"hypothesis_id": "h1"}},
        ),
        entry("Hypothesis", "h1", {"primary_metric": "net_profit"}),
        entry("DatasetSnapshot", "ds1", {"manifest_sha256": h("ds1")}),
        entry("DatasetSnapshot", "train-m1", {"manifest_sha256": h("train-m1")}),
        entry("DatasetSnapshot", "train-m2", {"manifest_sha256": h("train-m2")}),
        entry("FeatureSet", "f1", {"definition_sha256": h("f1")}),
        entry(
            "ModelVersion",
            "m1",
            {
                "artifact_sha256": h("m1"),
                "dataset_snapshot_id": "train-m1",
                "feature_set_id": "f1",
                "research_protocol_id": "rp1",
            },
        ),
        entry(
            "StrategyVersion",
            "s1",
            {
                "canonical_strategy_id": "strategy-main",
                "source_sha256": h("strategy-source"),
                "environment_sha256": h("strategy-env"),
                "config_sha256": h("strategy-config"),
                "model_version_id": "m1",
            },
        ),
        entry(
            "Experiment",
            "control",
            {
                "research_protocol_id": "rp1",
                "dataset_snapshot_id": "ds1",
                "feature_set_id": "f1",
                "strategy_version_id": "s1",
                "evaluation_bundle_id": "e1",
                "model_version_id": "m1",
                "seed": 7,
            },
        ),
        entry(
            "Experiment",
            "treatment",
            {
                "research_protocol_id": treatment_protocol_id,
                "dataset_snapshot_id": treatment_dataset,
                "feature_set_id": "f1",
                "strategy_version_id": treatment_strategy,
                "evaluation_bundle_id": "e2",
                "model_version_id": treatment_model,
                "seed": 7,
            },
        ),
        entry(
            "EvaluationBundle",
            "e1",
            {
                "protocol_sha256": protocol_sha,
                "dataset_snapshot_id": "ds1",
                "evaluated_strategy_version_id": "s1",
                "evaluated_model_version_id": "m1",
                "evaluator_source_sha256": evaluator_control,
            },
        ),
        entry(
            "EvaluationBundle",
            "e2",
            {
                "protocol_sha256": protocol_sha,
                "dataset_snapshot_id": treatment_eval_dataset,
                "evaluated_strategy_version_id": treatment_strategy,
                "evaluated_model_version_id": treatment_model,
                "evaluator_source_sha256": evaluator_treatment,
            },
        ),
    ]
    if treatment_dataset != "ds1":
        entries.append(
            entry("DatasetSnapshot", treatment_dataset, {"manifest_sha256": h(treatment_dataset)})
        )
    if treatment_model != "m1":
        entries.append(
            entry(
                "ModelVersion",
                treatment_model,
                {
                    "artifact_sha256": h(treatment_model),
                    "dataset_snapshot_id": "train-m2",
                    "feature_set_id": "f1",
                    "research_protocol_id": treatment_protocol_id,
                },
            )
        )
    if treatment_strategy != "s1":
        entries.append(
            entry(
                "StrategyVersion",
                treatment_strategy,
                {
                    "canonical_strategy_id": "strategy-main",
                    "source_sha256": h(treatment_strategy_source),
                    "environment_sha256": h(treatment_strategy_environment),
                    "config_sha256": h(treatment_strategy_config),
                    "model_version_id": treatment_model,
                },
            )
        )
    if treatment_protocol_id != "rp1":
        entries.extend(
            [
                entry(
                    "ResearchProtocol",
                    treatment_protocol_id,
                    {
                        "protocol_sha256": h("protocol2"),
                        "binding": {"hypothesis_id": "h2"},
                    },
                ),
                entry("Hypothesis", "h2", {"primary_metric": "net_profit"}),
            ]
        )
    return FakeRegistry(entries)


def snapshot(
    registry: FakeRegistry,
    experiment_id: str,
    *,
    threshold: str = "threshold",
    sizing: str = "sizing",
    execution: str = "execution",
    missing: str | None = None,
) -> AblationAuthoritySnapshot:
    core = derive_scientific_core_authorities(registry=registry, experiment_id=experiment_id)
    assert core is not None
    values = {
        "data_sha256": core.data_sha256,
        "model_sha256": core.model_sha256,
        "threshold_sha256": h(threshold),
        "sizing_sha256": h(sizing),
        "execution_sha256": h(execution),
        "source_evidence_sha256": h("source:" + experiment_id + sizing),
    }
    if missing is not None:
        values[f"{missing}_sha256"] = None
    return AblationAuthoritySnapshot(**values)


def test_exact_model_only_delta_is_attributable_and_non_authorizing():
    registry = registry_pair()
    result = evaluate_ablation_attribution(
        registry=registry,
        control_experiment_id="control",
        treatment_experiment_id="treatment",
        declared_component=AblationComponent.MODEL,
        control_authorities=snapshot(registry, "control"),
        treatment_authorities=snapshot(registry, "treatment"),
    )
    assert result.status is AblationAttributionStatus.ATTRIBUTABLE
    assert result.changed_components == (AblationComponent.MODEL,)
    assert result.estimand == "net_profit"
    assert result.reason_codes == ("EXACT_ONE_FACTOR_DELTA",)
    assert result.promotion_authorized is False
    assert result.learning_update_authorized is False
    assert result.execution_authorized is False
    assert len(result.attribution_id) == 64


def test_model_plus_sizing_change_is_unattributed():
    registry = registry_pair()
    result = evaluate_ablation_attribution(
        registry=registry,
        control_experiment_id="control",
        treatment_experiment_id="treatment",
        declared_component=AblationComponent.MODEL,
        control_authorities=snapshot(registry, "control", sizing="size1"),
        treatment_authorities=snapshot(registry, "treatment", sizing="size2"),
    )
    assert result.status is AblationAttributionStatus.UNATTRIBUTED
    assert "NOT_EXACT_ONE_FACTOR_DELTA" in result.reason_codes
    assert result.changed_components == (AblationComponent.MODEL, AblationComponent.SIZING)


def test_declared_model_with_data_change_is_unattributed():
    registry = registry_pair(treatment_dataset="ds2")
    result = evaluate_ablation_attribution(
        registry=registry,
        control_experiment_id="control",
        treatment_experiment_id="treatment",
        declared_component=AblationComponent.MODEL,
        control_authorities=snapshot(registry, "control"),
        treatment_authorities=snapshot(registry, "treatment"),
    )
    assert result.status is AblationAttributionStatus.UNATTRIBUTED
    assert "NOT_EXACT_ONE_FACTOR_DELTA" in result.reason_codes
    assert result.changed_components == (AblationComponent.DATA, AblationComponent.MODEL)


def test_missing_component_authority_fails_closed():
    registry = registry_pair()
    result = evaluate_ablation_attribution(
        registry=registry,
        control_experiment_id="control",
        treatment_experiment_id="treatment",
        declared_component=AblationComponent.MODEL,
        control_authorities=snapshot(registry, "control", missing="execution"),
        treatment_authorities=snapshot(registry, "treatment", missing="execution"),
    )
    assert result.status is AblationAttributionStatus.UNATTRIBUTED
    assert "COMPONENT_AUTHORITY_MISSING" in result.reason_codes


def test_evaluator_source_mismatch_fails_closed_even_for_one_factor_delta():
    registry = registry_pair(evaluator_treatment=h("other-evaluator"))
    result = evaluate_ablation_attribution(
        registry=registry,
        control_experiment_id="control",
        treatment_experiment_id="treatment",
        declared_component=AblationComponent.MODEL,
        control_authorities=snapshot(registry, "control"),
        treatment_authorities=snapshot(registry, "treatment"),
    )
    assert result.status is AblationAttributionStatus.UNATTRIBUTED
    assert "EVALUATOR_IDENTITY_MISMATCH" in result.reason_codes


def test_research_protocol_mismatch_fails_closed():
    registry = registry_pair(treatment_protocol_id="rp2")
    result = evaluate_ablation_attribution(
        registry=registry,
        control_experiment_id="control",
        treatment_experiment_id="treatment",
        declared_component=AblationComponent.MODEL,
        control_authorities=snapshot(registry, "control"),
        treatment_authorities=snapshot(registry, "treatment"),
    )
    assert result.status is AblationAttributionStatus.UNATTRIBUTED
    assert "RESEARCH_PROTOCOL_MISMATCH" in result.reason_codes


def test_evaluation_dataset_must_match_its_experiment():
    registry = registry_pair(treatment_dataset="ds2", treatment_eval_dataset="wrong")
    result = evaluate_ablation_attribution(
        registry=registry,
        control_experiment_id="control",
        treatment_experiment_id="treatment",
        declared_component=AblationComponent.DATA,
        control_authorities=snapshot(registry, "control"),
        treatment_authorities=snapshot(registry, "treatment"),
    )
    assert result.status is AblationAttributionStatus.UNATTRIBUTED
    assert "TREATMENT_EVALUATION_DATASET_MISMATCH" in result.reason_codes


def test_no_delta_is_never_attributed():
    registry = registry_pair(treatment_model="m1", treatment_strategy="s2")
    result = evaluate_ablation_attribution(
        registry=registry,
        control_experiment_id="control",
        treatment_experiment_id="treatment",
        declared_component=AblationComponent.MODEL,
        control_authorities=snapshot(registry, "control"),
        treatment_authorities=snapshot(registry, "treatment"),
    )
    assert result.status is AblationAttributionStatus.UNATTRIBUTED
    assert "NO_COMPONENT_CHANGED" in result.reason_codes


def test_missing_control_experiment_returns_unattributed_not_zero_effect():
    registry = registry_pair()
    result = evaluate_ablation_attribution(
        registry=registry,
        control_experiment_id="missing",
        treatment_experiment_id="treatment",
        declared_component=AblationComponent.MODEL,
        control_authorities=snapshot(registry, "control"),
        treatment_authorities=snapshot(registry, "treatment"),
    )
    assert result.status is AblationAttributionStatus.UNATTRIBUTED
    assert "CONTROL_EXPERIMENT_MISSING" in result.reason_codes
    assert result.estimand is None


def test_caller_cannot_relabel_model_fingerprint_away_from_registry_lineage():
    registry = registry_pair()
    control = snapshot(registry, "control")
    forged = AblationAuthoritySnapshot(
        data_sha256=control.data_sha256,
        model_sha256=h("forged-model"),
        threshold_sha256=control.threshold_sha256,
        sizing_sha256=control.sizing_sha256,
        execution_sha256=control.execution_sha256,
        source_evidence_sha256=h("forged-source"),
    )
    result = evaluate_ablation_attribution(
        registry=registry,
        control_experiment_id="control",
        treatment_experiment_id="treatment",
        declared_component=AblationComponent.MODEL,
        control_authorities=forged,
        treatment_authorities=snapshot(registry, "treatment"),
    )
    assert result.status is AblationAttributionStatus.UNATTRIBUTED
    assert "CONTROL_MODEL_AUTHORITY_MISMATCH" in result.reason_codes


@pytest.mark.parametrize(
    ("component", "field"),
    [
        (AblationComponent.THRESHOLD, "threshold"),
        (AblationComponent.SIZING, "sizing"),
        (AblationComponent.EXECUTION, "execution"),
    ],
)
def test_non_model_subcomponent_delta_fails_closed_without_durable_subauthority(
    component, field
):
    registry = registry_pair(treatment_model="m1", treatment_strategy="s1")
    control = snapshot(registry, "control")
    kwargs = {field: f"changed-{field}"}
    treatment = snapshot(registry, "treatment", **kwargs)
    result = evaluate_ablation_attribution(
        registry=registry,
        control_experiment_id="control",
        treatment_experiment_id="treatment",
        declared_component=component,
        control_authorities=control,
        treatment_authorities=treatment,
    )
    assert result.status is AblationAttributionStatus.UNATTRIBUTED
    assert "COMPONENT_SUBAUTHORITY_UNRESOLVED" in result.reason_codes
    assert result.changed_components == (component,)


def test_model_registry_lineage_mismatch_fails_closed():
    registry = registry_pair()
    bad_model = registry._entries[("ModelVersion", "m2")]
    registry._entries[("ModelVersion", "m2")] = RegistryEntry(
        bad_model.record_type,
        bad_model.record_id,
        bad_model.available_at,
        {**bad_model.payload, "dataset_snapshot_id": "wrong-dataset"},
        bad_model.record_sha256,
    )
    assert derive_scientific_core_authorities(
        registry=registry, experiment_id="treatment"
    ) is None
    control = snapshot(registry, "control")
    treatment = AblationAuthoritySnapshot(
        data_sha256=control.data_sha256,
        model_sha256=h("m2-untrusted"),
        threshold_sha256=control.threshold_sha256,
        sizing_sha256=control.sizing_sha256,
        execution_sha256=control.execution_sha256,
        source_evidence_sha256=h("untrusted"),
    )
    result = evaluate_ablation_attribution(
        registry=registry,
        control_experiment_id="control",
        treatment_experiment_id="treatment",
        declared_component=AblationComponent.MODEL,
        control_authorities=control,
        treatment_authorities=treatment,
    )
    assert result.status is AblationAttributionStatus.UNATTRIBUTED
    assert "TREATMENT_CORE_AUTHORITY_UNRESOLVED" in result.reason_codes


def test_evaluation_dataset_can_differ_from_model_training_dataset():
    registry = registry_pair(
        treatment_dataset="ds2",
        treatment_model="m1",
        treatment_strategy="s1",
    )
    core = derive_scientific_core_authorities(
        registry=registry, experiment_id="treatment"
    )
    assert core is not None
    result = evaluate_ablation_attribution(
        registry=registry,
        control_experiment_id="control",
        treatment_experiment_id="treatment",
        declared_component=AblationComponent.DATA,
        control_authorities=snapshot(registry, "control"),
        treatment_authorities=snapshot(registry, "treatment"),
    )
    assert result.status is AblationAttributionStatus.ATTRIBUTABLE
    assert result.changed_components == (AblationComponent.DATA,)


def test_non_model_strategy_config_change_blocks_model_attribution_even_if_witnesses_lie():
    registry = registry_pair(treatment_strategy_config="changed-config")
    result = evaluate_ablation_attribution(
        registry=registry,
        control_experiment_id="control",
        treatment_experiment_id="treatment",
        declared_component=AblationComponent.MODEL,
        control_authorities=snapshot(registry, "control"),
        treatment_authorities=snapshot(registry, "treatment"),
    )
    assert result.status is AblationAttributionStatus.UNATTRIBUTED
    assert "NON_MODEL_STRATEGY_AUTHORITY_MISMATCH" in result.reason_codes


def test_strategy_model_binding_must_match_experiment_model():
    registry = registry_pair()
    strategy = registry._entries[("StrategyVersion", "s2")]
    registry._entries[("StrategyVersion", "s2")] = RegistryEntry(
        strategy.record_type,
        strategy.record_id,
        strategy.available_at,
        {**strategy.payload, "model_version_id": "m1"},
        strategy.record_sha256,
    )
    assert derive_scientific_core_authorities(
        registry=registry, experiment_id="treatment"
    ) is None


def test_invalid_evaluator_sha_fails_closed():
    registry = registry_pair(evaluator_treatment="not-a-sha")
    result = evaluate_ablation_attribution(
        registry=registry,
        control_experiment_id="control",
        treatment_experiment_id="treatment",
        declared_component=AblationComponent.MODEL,
        control_authorities=snapshot(registry, "control"),
        treatment_authorities=snapshot(registry, "treatment"),
    )
    assert result.status is AblationAttributionStatus.UNATTRIBUTED
    assert "EVALUATOR_IDENTITY_MISSING" in result.reason_codes


def test_attribution_identity_is_deterministic():
    registry = registry_pair()
    kwargs = dict(
        registry=registry,
        control_experiment_id="control",
        treatment_experiment_id="treatment",
        declared_component=AblationComponent.MODEL,
        control_authorities=snapshot(registry, "control"),
        treatment_authorities=snapshot(registry, "treatment"),
    )
    left = evaluate_ablation_attribution(**kwargs)
    right = evaluate_ablation_attribution(**kwargs)
    assert left.to_payload() == right.to_payload()
    assert left.attribution_id == right.attribution_id

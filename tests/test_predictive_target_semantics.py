from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from autosport.predictive_target_semantics import (
    PREDICTIVE_TARGET_ARTIFACT_PREFIX,
    PredictiveTargetContract,
    PredictiveTargetSemanticsError,
    resolve_binary_predictive_target,
)
from autosport.reproducibility_manifest import (
    FactoryReproducibilityManifest,
    WalkForwardSplit,
)
from autosport.scientific_registry import (
    Hypothesis,
    ResearchProtocol,
    ResearchQuestion,
    ScientificRegistry,
)
from autosport.strategy_experiment import ScientificProtocolBinding
from autosport.strategy_model_factory import (
    TrainingPoint,
    training_points_manifest_sha256,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
T0 = "2026-01-01T00:00:00+00:00"
T0A = "2026-01-01T00:01:00+00:00"
T1 = "2026-01-02T00:00:00+00:00"
T2 = "2026-01-03T00:00:00+00:00"
T3 = "2026-01-04T00:00:00+00:00"
T4 = "2026-01-05T00:00:00+00:00"


def _payload_sha(record) -> str:
    encoded = json.dumps(
        record.to_payload(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _contract(
    *,
    contract_id: str = "binary-settlement-target-v1",
    target_semantics_id: str = "selection-settlement-win-v1",
    frozen_at: str = T0,
) -> PredictiveTargetContract:
    return PredictiveTargetContract(
        contract_id=contract_id,
        research_protocol_id="protocol-target-v1",
        target_semantics_id=target_semantics_id,
        positive_outcome_definition_sha256=SHA_A,
        negative_outcome_definition_sha256=SHA_B,
        frozen_at=frozen_at,
    )


def _points(
    *,
    middle_target: float = 0.0,
    middle_evidence: tuple[str, ...] = (SHA_B,),
    last_target_available_at: str = T3,
) -> tuple[TrainingPoint, ...]:
    return (
        TrainingPoint(
            T1,
            0.50,
            1.0,
            target_available_at=T1,
            evidence_sha256s=(SHA_A,),
        ),
        TrainingPoint(
            T2,
            0.40,
            middle_target,
            target_available_at=T2,
            evidence_sha256s=middle_evidence,
        ),
        TrainingPoint(
            T3,
            0.60,
            1.0,
            target_available_at=last_target_available_at,
            evidence_sha256s=(SHA_C,),
        ),
    )


def _binding(
    contract: PredictiveTargetContract,
    *,
    target_tokens: tuple[str, ...] | None = None,
    frozen_at: str = T0,
) -> ScientificProtocolBinding:
    question = ResearchQuestion(
        "question-target-v1",
        "Can the frozen binary target support a predictive model?",
        SHA_A,
        T0,
    )
    hypothesis = Hypothesis(
        "hypothesis-target-v1",
        question.question_id,
        "Candidate improves the frozen evaluation metric.",
        "frozen challenger metric improves",
        "no improvement or guardrail failure",
        "roi",
        (),
        T0,
    )
    promotion_rule = json.dumps(
        {
            "kind": "autosport-promotion-rule-v1",
            "primary_metric": "roi",
            "minimum_improvement": 0.01,
            "minimum_effective_sample_size": 2,
            "protective_metric_maxima": [],
            "metric_direction": "lower_is_better",
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    artifacts = (
        target_tokens
        if target_tokens is not None
        else (contract.preregistration_token, "evaluation-bundle")
    )
    return ScientificProtocolBinding(
        research_protocol_id=contract.research_protocol_id,
        research_question_id=question.question_id,
        research_question_sha256=_payload_sha(question),
        hypothesis_id=hypothesis.hypothesis_id,
        hypothesis_sha256=_payload_sha(hypothesis),
        inclusion_criteria="predeclared lawful observations",
        exclusion_criteria="invalid provenance",
        lawful_source_requirements="retained lawful source evidence",
        causal_cutoff=T1,
        evaluation_design="sealed causal evaluation",
        feature_set_version="v1",
        uncertainty_method="paired min/max interval",
        multiple_comparison_control="single frozen primary metric",
        robustness_checks=("time split",),
        random_seed_policy="fixed before evaluation",
        stopping_rule="one final evaluation",
        promotion_rule=promotion_rule,
        expected_artifacts=artifacts,
        code_config_sha256=SHA_C,
        frozen_at_utc=frozen_at,
    )


def _registry_and_protocol(
    tmp_path,
    contract: PredictiveTargetContract,
    *,
    target_tokens: tuple[str, ...] | None = None,
    protocol_available_at: str = T0A,
    frozen_at: str = T0,
):
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific.json"
    )
    binding = _binding(
        contract,
        target_tokens=target_tokens,
        frozen_at=frozen_at,
    )
    question = ResearchQuestion(
        "question-target-v1",
        "Can the frozen binary target support a predictive model?",
        SHA_A,
        T0,
    )
    hypothesis = Hypothesis(
        "hypothesis-target-v1",
        question.question_id,
        "Candidate improves the frozen evaluation metric.",
        "frozen challenger metric improves",
        "no improvement or guardrail failure",
        "roi",
        (),
        T0,
    )
    protocol = ResearchProtocol(
        binding,
        SHA_C,
        SHA_D,
        SHA_A,
        protocol_available_at,
    )
    registry.append(question)
    registry.append(hypothesis)
    registry.append(protocol)
    return registry, protocol


def _manifest(
    points: tuple[TrainingPoint, ...],
    protocol: ResearchProtocol,
) -> FactoryReproducibilityManifest:
    manifest_sha = training_points_manifest_sha256(points)
    return FactoryReproducibilityManifest(
        experiment_id="experiment-target-v1",
        evaluation_bundle_id="evaluation-target-v1",
        dataset_snapshot_id="dataset-target-v1",
        dataset_manifest_sha256=manifest_sha,
        training_points_manifest_sha256=manifest_sha,
        dataset_source_identity="lawful-provider:fixture",
        dataset_license_identity="license-evidence:v1",
        splits=(
            WalkForwardSplit(
                fold_id="fold-1",
                training_indices=(0, 1),
                evaluation_index=2,
                training_cutoff=T2,
                evaluation_at=T3,
            ),
        ),
        model_version_id="model-target-v1",
        model_artifact_sha256=SHA_A,
        learner_state_sha256=SHA_B,
        model_config_sha256=SHA_C,
        research_protocol_id=protocol.record_id,
        protocol_sha256=protocol.protocol_sha256,
        evaluator_config_sha256=SHA_D,
        source_sha256=SHA_A,
        evaluator_source_sha256=SHA_B,
        environment_sha256=SHA_C,
        seed=7,
    )


def test_binary_target_resolution_requires_exact_preregistered_population(
    tmp_path,
) -> None:
    contract = _contract()
    points = _points()
    registry, protocol = _registry_and_protocol(tmp_path, contract)
    manifest = _manifest(points, protocol)

    resolved = resolve_binary_predictive_target(
        registry,
        manifest,
        contract,
        tuple(reversed(points)),
        as_of=T4,
    )

    assert resolved.contract_sha256 == contract.contract_sha256
    assert resolved.training_points_manifest_sha256 == (
        training_points_manifest_sha256(points)
    )
    assert resolved.input_count == 3
    assert resolved.positive_count == 2
    assert resolved.negative_count == 1
    assert len(resolved.target_population_sha256) == 64
    assert resolved.to_dict()["truth"] == {
        "binary_target_semantics_resolved": True,
        "model_output_probability_authority": False,
        "calibration_authority": False,
        "forecast_authority": False,
        "promotion_authority": False,
        "real_money_execution": False,
    }


def test_generic_numeric_factory_target_cannot_be_relabelled_binary(
    tmp_path,
) -> None:
    contract = _contract()
    points = _points(middle_target=0.4)
    registry, protocol = _registry_and_protocol(tmp_path, contract)
    manifest = _manifest(points, protocol)

    with pytest.raises(
        PredictiveTargetSemanticsError,
        match="binary target values must be exactly 0 or 1",
    ):
        resolve_binary_predictive_target(
            registry, manifest, contract, points, as_of=T4
        )


def test_binary_label_without_causal_evidence_fails_closed(tmp_path) -> None:
    contract = _contract()
    points = _points(middle_evidence=())
    registry, protocol = _registry_and_protocol(tmp_path, contract)
    manifest = _manifest(points, protocol)

    with pytest.raises(
        PredictiveTargetSemanticsError,
        match="binary target labels require causal evidence digests",
    ):
        resolve_binary_predictive_target(
            registry, manifest, contract, points, as_of=T4
        )


def test_substituted_target_contract_digest_fails_closed(tmp_path) -> None:
    contract = _contract()
    points = _points()
    registry, protocol = _registry_and_protocol(tmp_path, contract)
    manifest = _manifest(points, protocol)
    substituted = _contract(
        contract_id="binary-settlement-target-v2",
        target_semantics_id="different-settlement-target-v2",
    )

    with pytest.raises(
        PredictiveTargetSemanticsError,
        match="digest does not match preregistration",
    ):
        resolve_binary_predictive_target(
            registry, manifest, substituted, points, as_of=T4
        )


def test_ambiguous_multiple_target_contract_tokens_fail_closed(
    tmp_path,
) -> None:
    contract = _contract()
    other = _contract(
        contract_id="binary-settlement-target-v2",
        target_semantics_id="different-settlement-target-v2",
    )
    tokens = (contract.preregistration_token, other.preregistration_token)
    points = _points()
    registry, protocol = _registry_and_protocol(
        tmp_path,
        contract,
        target_tokens=tokens,
    )
    manifest = _manifest(points, protocol)

    with pytest.raises(
        PredictiveTargetSemanticsError,
        match="preregister exactly one predictive target contract",
    ):
        resolve_binary_predictive_target(
            registry, manifest, contract, points, as_of=T4
        )


def test_protocol_must_be_available_before_training_population(tmp_path) -> None:
    contract = _contract()
    points = _points()
    registry, protocol = _registry_and_protocol(
        tmp_path,
        contract,
        protocol_available_at="2026-01-02T00:00:01+00:00",
    )
    manifest = _manifest(points, protocol)

    with pytest.raises(
        PredictiveTargetSemanticsError,
        match="not durably available before training population",
    ):
        resolve_binary_predictive_target(
            registry, manifest, contract, points, as_of=T4
        )


def test_contract_must_be_frozen_by_protocol_freeze(tmp_path) -> None:
    contract = _contract(frozen_at="2026-01-01T00:00:01+00:00")
    points = _points()
    registry, protocol = _registry_and_protocol(
        tmp_path,
        contract,
        frozen_at=T0,
    )
    manifest = _manifest(points, protocol)

    with pytest.raises(
        PredictiveTargetSemanticsError,
        match="target contract was not frozen by protocol freeze",
    ):
        resolve_binary_predictive_target(
            registry, manifest, contract, points, as_of=T4
        )


def test_training_population_substitution_fails_before_semantic_resolution(
    tmp_path,
) -> None:
    contract = _contract()
    original = _points()
    registry, protocol = _registry_and_protocol(tmp_path, contract)
    manifest = _manifest(original, protocol)
    substituted = list(original)
    substituted[1] = replace(substituted[1], feature=0.41)

    with pytest.raises(
        PredictiveTargetSemanticsError,
        match="do not match reproducibility manifest",
    ):
        resolve_binary_predictive_target(
            registry,
            manifest,
            contract,
            tuple(substituted),
            as_of=T4,
        )


def test_future_label_is_not_causally_available_for_resolution(tmp_path) -> None:
    contract = _contract()
    points = _points(last_target_available_at=T4)
    registry, protocol = _registry_and_protocol(tmp_path, contract)
    manifest = _manifest(points, protocol)

    with pytest.raises(
        PredictiveTargetSemanticsError,
        match="target label was not causally available at resolution time",
    ):
        resolve_binary_predictive_target(
            registry,
            manifest,
            contract,
            points,
            as_of="2026-01-04T12:00:00+00:00",
        )


def test_contract_token_is_hash_stable_and_namespaced() -> None:
    contract = _contract()
    assert contract.contract_sha256 == _contract().contract_sha256
    assert contract.preregistration_token == (
        PREDICTIVE_TARGET_ARTIFACT_PREFIX + contract.contract_sha256
    )
    assert len(contract.contract_sha256) == 64

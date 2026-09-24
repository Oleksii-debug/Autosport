from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from autosport._strategy_model_factory_impl import MeanBaselineModel
from autosport.registered_strategy_model_runtime import (
    RegisteredStrategyModelRuntimeError,
    resolve_registered_strategy_model,
)
from autosport.strategy_model_factory import FactoryArtifactStore


A = "a" * 64
B = "b" * 64
C = "c" * 64
D = "d" * 64
E = "e" * 64
F = "f" * 64


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _entry(
    record_type: str,
    record_id: str,
    available_at: str,
    payload: dict[str, object],
) -> dict[str, object]:
    envelope: dict[str, object] = {
        "record_type": record_type,
        "record_id": record_id,
        "available_at": available_at,
        "payload": payload,
    }
    envelope["record_sha256"] = _digest(envelope)
    return envelope


def _model_artifact(
    *,
    model_id: str = "model-v1",
    family: str = "mean-baseline-v1",
    mean_target: float = 0.61,
) -> dict[str, object]:
    model = MeanBaselineModel(
        model_id=model_id,
        training_cutoff="2026-01-01T00:00:00Z",
        mean_target=mean_target,
        training_count=3,
    )
    return {
        "schema_version": 1,
        "family": family,
        "model_id": model_id,
        "training_cutoff": model.training_cutoff,
        "mean_target": mean_target,
        "training_count": 3,
        "identity_sha256": model.identity_sha256,
        "model_version_id": model_id,
        "research_protocol_id": "protocol-v1",
        "dataset_snapshot_id": "dataset-v1",
        "feature_set_id": "feature-v1",
        "config_sha256": A,
        "evaluator_config_sha256": B,
        "training_points_manifest_sha256": C,
        "seed": 7,
    }


def _write_workspace(
    workspace: Path,
    *,
    artifact_family: str = "mean-baseline-v1",
    registry_model_family: str = "mean-baseline-v1",
    mean_target: float = 0.61,
    feature_after_model: bool = False,
    second_champion: bool = False,
) -> tuple[str, Path]:
    store = FactoryArtifactStore(workspace / "factory-artifacts")
    artifact = _model_artifact(
        family=artifact_family,
        mean_target=mean_target,
    )
    model_artifact_sha256 = store.write("model", "model-v1", artifact)

    protocol = _entry(
        "ResearchProtocol",
        "protocol-v1",
        "2026-01-01T00:00:01Z",
        {
            "research_protocol_id": "protocol-v1",
            "protocol_sha256": D,
            "binding": {},
            "source_sha256": A,
            "environment_sha256": E,
            "dataset_manifest_sha256": F,
            "available_at": "2026-01-01T00:00:01Z",
        },
    )
    dataset = _entry(
        "DatasetSnapshot",
        "dataset-v1",
        "2026-01-01T00:00:02Z",
        {
            "dataset_snapshot_id": "dataset-v1",
            "manifest_sha256": F,
            "source_identity": "source",
            "license_identity": "license",
            "causal_cutoff": "2026-01-01T00:00:00Z",
            "available_at": "2026-01-01T00:00:02Z",
            "outcome_reveal_after": None,
        },
    )
    feature = _entry(
        "FeatureSet",
        "feature-v1",
        "2026-01-01T00:00:03Z",
        {
            "feature_set_id": "feature-v1",
            "version": "1",
            "definition_sha256": B,
            "source_sha256": C,
            "available_at": "2026-01-01T00:00:03Z",
        },
    )
    model = _entry(
        "ModelVersion",
        "model-v1",
        "2026-01-01T00:00:04Z",
        {
            "model_version_id": "model-v1",
            "model_family": registry_model_family,
            "artifact_sha256": model_artifact_sha256,
            "source_sha256": E,
            "environment_sha256": D,
            "dataset_snapshot_id": "dataset-v1",
            "feature_set_id": "feature-v1",
            "research_protocol_id": "protocol-v1",
            "seed": 7,
            "config_sha256": A,
            "created_at": "2026-01-01T00:00:04Z",
            "predecessor_model_version_id": None,
        },
    )
    strategy = _entry(
        "StrategyVersion",
        "strategy-v1",
        "2026-01-01T00:00:05Z",
        {
            "strategy_version_id": "strategy-v1",
            "canonical_strategy_id": "registered-strategy",
            "source_sha256": E,
            "environment_sha256": D,
            "config_sha256": A,
            "created_at": "2026-01-01T00:00:05Z",
            "model_version_id": "model-v1",
            "predecessor_strategy_version_id": None,
        },
    )
    evaluation_bundle_sha256 = B
    evaluation = _entry(
        "EvaluationBundle",
        "evaluation-v1",
        "2026-01-01T00:00:06Z",
        {
            "evaluation_bundle_id": "evaluation-v1",
            "bundle_sha256": evaluation_bundle_sha256,
            "evaluator_source_sha256": C,
            "dataset_snapshot_id": "dataset-v1",
            "protocol_sha256": D,
            "artifact_hashes": [model_artifact_sha256],
            "created_at": "2026-01-01T00:00:06Z",
            "evaluated_strategy_version_id": "strategy-v1",
            "evaluated_model_version_id": "model-v1",
        },
    )
    experiment = _entry(
        "Experiment",
        "experiment-v1",
        "2026-01-01T00:00:07Z",
        {
            "experiment_id": "experiment-v1",
            "research_protocol_id": "protocol-v1",
            "dataset_snapshot_id": "dataset-v1",
            "feature_set_id": "feature-v1",
            "model_version_id": "model-v1",
            "strategy_version_id": "strategy-v1",
            "evaluation_bundle_id": "evaluation-v1",
            "seed": 7,
            "config_sha256": A,
            "outcome": "POSITIVE",
            "created_at": "2026-01-01T00:00:04Z",
            "completed_at": "2026-01-01T00:00:07Z",
            "fingerprint": F,
            "notes": "",
        },
    )
    promotion = _entry(
        "PromotionDecision",
        "promotion-v1",
        "2026-01-01T00:00:08Z",
        {
            "promotion_decision_id": "promotion-v1",
            "action": "PROMOTE",
            "candidate_strategy_version_id": "strategy-v1",
            "candidate_model_version_id": "model-v1",
            "research_protocol_id": "protocol-v1",
            "protocol_sha256": D,
            "evaluation_bundle_id": "evaluation-v1",
            "evaluation_bundle_sha256": evaluation_bundle_sha256,
            "predecessor_strategy_version_id": None,
            "rollback_to_strategy_version_id": None,
            "promotion_evidence_id": "promotion-evidence-v1",
            "reason": "test",
            "decided_at": "2026-01-01T00:00:08Z",
        },
    )

    records: list[dict[str, object]] = [protocol, dataset]
    if feature_after_model:
        records.extend((model, feature))
    else:
        records.extend((feature, model))
    records.extend((strategy, evaluation, experiment, promotion))

    if second_champion:
        second_artifact = _model_artifact(
            model_id="model-v2",
            mean_target=0.55,
        )
        second_artifact_sha256 = store.write(
            "model",
            "model-v2",
            second_artifact,
        )
        records.extend(
            (
                _entry(
                    "ModelVersion",
                    "model-v2",
                    "2026-01-01T00:00:09Z",
                    {
                        "model_version_id": "model-v2",
                        "model_family": "mean-baseline-v1",
                        "artifact_sha256": second_artifact_sha256,
                        "source_sha256": E,
                        "environment_sha256": D,
                        "dataset_snapshot_id": "dataset-v1",
                        "feature_set_id": "feature-v1",
                        "research_protocol_id": "protocol-v1",
                        "seed": 7,
                        "config_sha256": A,
                        "created_at": "2026-01-01T00:00:09Z",
                        "predecessor_model_version_id": "model-v1",
                    },
                ),
                _entry(
                    "StrategyVersion",
                    "strategy-v2",
                    "2026-01-01T00:00:10Z",
                    {
                        "strategy_version_id": "strategy-v2",
                        "canonical_strategy_id": "registered-strategy",
                        "source_sha256": E,
                        "environment_sha256": D,
                        "config_sha256": A,
                        "created_at": "2026-01-01T00:00:10Z",
                        "model_version_id": "model-v2",
                        "predecessor_strategy_version_id": "strategy-v1",
                    },
                ),
                _entry(
                    "PromotionDecision",
                    "promotion-v2",
                    "2026-01-01T00:00:11Z",
                    {
                        "promotion_decision_id": "promotion-v2",
                        "action": "PROMOTE",
                        "candidate_strategy_version_id": "strategy-v2",
                        "candidate_model_version_id": "model-v2",
                        "research_protocol_id": "protocol-v1",
                        "protocol_sha256": D,
                        "evaluation_bundle_id": "evaluation-v2",
                        "evaluation_bundle_sha256": C,
                        "predecessor_strategy_version_id": "strategy-v1",
                        "rollback_to_strategy_version_id": None,
                        "promotion_evidence_id": "promotion-evidence-v2",
                        "reason": "test",
                        "decided_at": "2026-01-01T00:00:11Z",
                    },
                ),
            )
        )

    registry_path = workspace / "scientific_registry.json"
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text(
        json.dumps(
            {"schema_version": 1, "records": records},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    return model_artifact_sha256, store.path_for_testing("model", "model-v1")


def test_resolves_exact_promoted_model_and_predicts_without_caller_callable(
    tmp_path: Path,
) -> None:
    _write_workspace(tmp_path)

    runtime = resolve_registered_strategy_model(
        tmp_path,
        strategy_version_id="strategy-v1",
        as_of="2026-01-01T00:01:00Z",
    )

    assert runtime.strategy_version_id == "strategy-v1"
    assert runtime.model_version_id == "model-v1"
    assert runtime.feature_set_id == "feature-v1"
    assert runtime.predict_probability(
        feature=0.52,
        observed_at="2026-01-01T00:00:30Z",
        decision_at="2026-01-01T00:01:00Z",
    ) == Decimal("0.61")


def test_rejects_activation_of_nonchampion_strategy(tmp_path: Path) -> None:
    _write_workspace(tmp_path, second_champion=True)

    with pytest.raises(
        RegisteredStrategyModelRuntimeError,
        match="not the durable champion",
    ):
        resolve_registered_strategy_model(
            tmp_path,
            strategy_version_id="strategy-v1",
            as_of="2026-01-01T00:02:00Z",
        )


def test_rejects_model_artifact_tampering(tmp_path: Path) -> None:
    _digest_value, artifact_path = _write_workspace(tmp_path)
    artifact_path.write_text('{"tampered":true}', encoding="utf-8")

    with pytest.raises(
        RegisteredStrategyModelRuntimeError,
        match="artifact is unavailable or hash-invalid",
    ):
        resolve_registered_strategy_model(
            tmp_path,
            strategy_version_id="strategy-v1",
            as_of="2026-01-01T00:01:00Z",
        )


@pytest.mark.parametrize(
    ("artifact_family", "registry_family"),
    [
        ("caller-model-v1", "caller-model-v1"),
        ("mean-baseline-v1", "caller-model-v1"),
    ],
)
def test_rejects_unsupported_or_cross_signed_model_family(
    tmp_path: Path,
    artifact_family: str,
    registry_family: str,
) -> None:
    _write_workspace(
        tmp_path,
        artifact_family=artifact_family,
        registry_model_family=registry_family,
    )

    with pytest.raises(
        RegisteredStrategyModelRuntimeError,
        match="model family is not supported",
    ):
        resolve_registered_strategy_model(
            tmp_path,
            strategy_version_id="strategy-v1",
            as_of="2026-01-01T00:01:00Z",
        )


def test_rejects_backfilled_feature_contract_that_did_not_precede_model(
    tmp_path: Path,
) -> None:
    _write_workspace(tmp_path, feature_after_model=True)

    with pytest.raises(
        RegisteredStrategyModelRuntimeError,
        match="FeatureSet -> ModelVersion.*append-order causality",
    ):
        resolve_registered_strategy_model(
            tmp_path,
            strategy_version_id="strategy-v1",
            as_of="2026-01-01T00:01:00Z",
        )


def test_rejects_model_artifact_probability_outside_predictive_domain(
    tmp_path: Path,
) -> None:
    _write_workspace(tmp_path, mean_target=1.2)

    with pytest.raises(
        RegisteredStrategyModelRuntimeError,
        match="mean_target must be a probability",
    ):
        resolve_registered_strategy_model(
            tmp_path,
            strategy_version_id="strategy-v1",
            as_of="2026-01-01T00:01:00Z",
        )

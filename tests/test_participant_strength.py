from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest

from autosport.opponent_intelligence import RatingSnapshot, SnapshotState
from autosport.participant_identity import IdentityView
from autosport.participant_strength import (
    HistogramCalibratedStrengthFactory,
    HistogramCalibratedStrengthModel,
    ParticipantStrengthError,
    RatingDifferenceBaselineFactory,
    StrengthSnapshotPair,
    emit_registered_strength_forecast,
)
from autosport.scientific_registry import (
    DatasetSnapshot,
    FeatureSet,
    Hypothesis,
    ModelVersion,
    ResearchProtocol,
    ResearchQuestion,
    ScientificRegistry,
    StrategyVersion,
)
from autosport.strategy_experiment import ScientificProtocolBinding
from autosport.strategy_model_factory import (
    FactoryArtifactStore,
    TrainingPoint,
    WalkForwardRunner,
    training_points_manifest_sha256,
)


T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-01T01:00:00+00:00"
T2 = "2026-01-01T02:00:00+00:00"
T3 = "2026-01-01T03:00:00+00:00"
T4 = "2026-01-01T04:00:00+00:00"
T5 = "2026-01-01T05:00:00+00:00"
T6 = "2026-01-01T06:00:00+00:00"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64


def _snapshot(
    participant: str,
    *,
    snapshot_sha: str,
    rating: str,
    uncertainty: str = "0.2",
    cutoff: str = T0,
    published: str = T0,
    state: SnapshotState = SnapshotState.SUPPORTED,
    view: IdentityView = IdentityView.AS_KNOWN_AT_DECISION,
) -> RatingSnapshot:
    return RatingSnapshot(
        snapshot_id=snapshot_sha,
        participant_entity_id=participant,
        sport_id="table-tennis",
        league_id="league-a",
        market_context_id="match-winner",
        view=view,
        causal_cutoff=cutoff,
        published_at=published,
        algorithm_family="bounded-mean-score",
        algorithm_version="mean-score-v1",
        config_sha256=SHA_C,
        min_support=2,
        max_age_seconds=86400,
        code_sha256=SHA_D,
        dependency_sha256=SHA_E,
        predecessor_snapshot_ids=(),
        input_performance_ids=(SHA_F,),
        input_digest=SHA_F,
        support=4,
        effective_sample=4,
        opponent_count=4,
        rating=rating if state is SnapshotState.SUPPORTED else None,
        uncertainty=uncertainty if state is SnapshotState.SUPPORTED else None,
        state=state,
    )


def _pair(
    subject_rating: str = "0.7",
    opponent_rating: str = "0.4",
    *,
    decision_at: str = T1,
    subject_sha: str = SHA_A,
    opponent_sha: str = SHA_B,
    subject_published: str = T0,
    opponent_published: str = T0,
) -> StrengthSnapshotPair:
    return StrengthSnapshotPair(
        _snapshot(
            "participant-a",
            snapshot_sha=subject_sha,
            rating=subject_rating,
            published=subject_published,
        ),
        _snapshot(
            "participant-b",
            snapshot_sha=opponent_sha,
            rating=opponent_rating,
            published=opponent_published,
        ),
        decision_at,
    )


def _point(index: int, feature: float, target: float) -> TrainingPoint:
    observed_hour = index
    return TrainingPoint(
        observed_at=f"2026-01-01T{observed_hour:02d}:00:00+00:00",
        feature=feature,
        target=target,
        target_available_at=(
            f"2026-01-01T{observed_hour:02d}:20:00+00:00"
        ),
        evidence_sha256s=(f"{index + 1:064x}",),
    )


def _payload_sha(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _append_preregistration(
    registry: ScientificRegistry,
    *,
    question_id: str,
    hypothesis_id: str,
    question_at: str = T0,
    hypothesis_at: str = T0,
) -> tuple[str, str]:
    question = ResearchQuestion(
        question_id=question_id,
        statement="Does causal participant-strength evidence improve forecast calibration?",
        source_sha256=SHA_A,
        created_at=question_at,
    )
    hypothesis = Hypothesis(
        hypothesis_id=hypothesis_id,
        research_question_id=question_id,
        statement="The registered participant-strength model is causally evaluable.",
        falsifiable_prediction="Frozen causal evaluation can reject the challenger.",
        failure_criteria="Missing lineage, future evidence, or failed calibration guardrail.",
        primary_metric="mse",
        protective_metrics=(),
        created_at=hypothesis_at,
    )
    registry.append(question)
    registry.append(hypothesis)
    return _payload_sha(question.to_payload()), _payload_sha(hypothesis.to_payload())


def _registered_strength_lineage(
    tmp_path,
    *,
    dataset_cutoff: str,
    dataset_available_at: str,
    model_created_at: str,
    strategy_created_at: str,
    include_preregistration: bool = True,
    protocol_cutoff: str | None = None,
    protocol_feature_version: str = "1",
    protocol_frozen_at: str = T1,
    protocol_available_at: str = T1,
    question_at: str = T0,
    hypothesis_at: str = T0,
):
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )
    artifacts = FactoryArtifactStore(tmp_path / "factory-artifacts")
    points = (_point(0, 0.2, 1.0), _point(1, -0.2, 0.0))
    model = RatingDifferenceBaselineFactory().fit(
        "model-lineage", points, training_cutoff=dataset_cutoff
    )
    payload = model.to_payload()
    payload.update(
        {
            "model_version_id": "model-lineage",
            "research_protocol_id": "protocol-lineage",
            "dataset_snapshot_id": "dataset-lineage",
            "feature_set_id": "features-lineage",
            "config_sha256": SHA_C,
            "training_points_manifest_sha256": model.training_manifest_sha256,
            "seed": 7,
        }
    )
    artifact_sha = artifacts.write("model", "model-lineage", payload)
    if include_preregistration:
        question_sha, hypothesis_sha = _append_preregistration(
            registry,
            question_id="question-lineage",
            hypothesis_id="hypothesis-lineage",
            question_at=question_at,
            hypothesis_at=hypothesis_at,
        )
    else:
        question_sha, hypothesis_sha = SHA_D, SHA_E
    registry.append(
        FeatureSet(
            feature_set_id="features-lineage",
            version="1",
            definition_sha256=SHA_D,
            source_sha256=SHA_A,
            available_at_utc=T1,
        )
    )
    registry.append(
        ResearchProtocol(
            binding=ScientificProtocolBinding(
                research_protocol_id="protocol-lineage",
                research_question_id="question-lineage",
                research_question_sha256=question_sha,
                hypothesis_id="hypothesis-lineage",
                hypothesis_sha256=hypothesis_sha,
                inclusion_criteria="causal rating snapshot pairs",
                exclusion_criteria="future or insufficient evidence",
                lawful_source_requirements="test fixture",
                causal_cutoff=protocol_cutoff or dataset_cutoff,
                evaluation_design="fixed causal fixture",
                feature_set_version=protocol_feature_version,
                uncertainty_method="descriptive rating radius",
                multiple_comparison_control="single challenger",
                robustness_checks=("future-leakage",),
                random_seed_policy="deterministic seed 7",
                stopping_rule="fixed fixture",
                promotion_rule="no promotion",
                expected_artifacts=("model",),
                code_config_sha256=SHA_C,
                frozen_at_utc=protocol_frozen_at,
            ),
            source_sha256=SHA_A,
            environment_sha256=SHA_B,
            dataset_manifest_sha256=model.training_manifest_sha256,
            available_at_utc=protocol_available_at,
        )
    )
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id="dataset-lineage",
            manifest_sha256=model.training_manifest_sha256,
            source_identity="test-causal-rating-snapshots",
            license_identity="test-fixture",
            causal_cutoff=dataset_cutoff,
            available_at_utc=dataset_available_at,
        )
    )
    registry.append(
        ModelVersion(
            model_version_id="model-lineage",
            model_family=model.model_family,
            artifact_sha256=artifact_sha,
            source_sha256=SHA_A,
            environment_sha256=SHA_B,
            dataset_snapshot_id="dataset-lineage",
            feature_set_id="features-lineage",
            research_protocol_id="protocol-lineage",
            seed=7,
            config_sha256=SHA_C,
            created_at=model_created_at,
        )
    )
    registry.append(
        StrategyVersion(
            strategy_version_id="strategy-lineage",
            canonical_strategy_id="participant-strength",
            source_sha256=SHA_A,
            environment_sha256=SHA_B,
            config_sha256=SHA_C,
            created_at=strategy_created_at,
            model_version_id="model-lineage",
        )
    )
    return registry, artifacts


def test_training_point_evidence_binds_snapshot_identity_without_changing_legacy_manifest():
    pair = _pair()
    point = pair.training_point(subject_won=True, target_available_at=T2)
    assert point.evidence_sha256s == (SHA_A, SHA_B)

    changed = StrengthSnapshotPair(
        _snapshot("participant-a", snapshot_sha=SHA_C, rating="0.7"),
        _snapshot("participant-b", snapshot_sha=SHA_B, rating="0.4"),
        T1,
    ).training_point(subject_won=True, target_available_at=T2)
    assert training_points_manifest_sha256((point,)) != training_points_manifest_sha256(
        (changed,)
    )

    legacy = TrainingPoint(
        observed_at=T0,
        feature=0.25,
        target=1.0,
        target_available_at=T1,
    )
    legacy_payload = {
        "schema_version": 1,
        "kind": "autosport-factory-training-points-v1",
        "points": [
            {
                "observed_at": T0,
                "feature": 0.25,
                "target": 1.0,
                "target_available_at": T1,
            }
        ],
    }
    expected = hashlib.sha256(
        json.dumps(
            legacy_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    assert training_points_manifest_sha256((legacy,)) == expected


def test_strength_snapshot_pair_rejects_unavailable_or_insufficient_evidence():
    with pytest.raises(ParticipantStrengthError, match="not published"):
        _pair(subject_published=T2, decision_at=T1)

    with pytest.raises(ParticipantStrengthError, match="supported rating"):
        StrengthSnapshotPair(
            _snapshot(
                "participant-a",
                snapshot_sha=SHA_A,
                rating="0.7",
                state=SnapshotState.INSUFFICIENT,
            ),
            _snapshot("participant-b", snapshot_sha=SHA_B, rating="0.4"),
            T1,
        )

    with pytest.raises(ParticipantStrengthError, match="AS_KNOWN_AT_DECISION"):
        StrengthSnapshotPair(
            _snapshot(
                "participant-a",
                snapshot_sha=SHA_A,
                rating="0.7",
                view=IdentityView.RESTATED_RESEARCH,
            ),
            _snapshot(
                "participant-b",
                snapshot_sha=SHA_B,
                rating="0.4",
                view=IdentityView.RESTATED_RESEARCH,
            ),
            T1,
        )


def test_rating_difference_baseline_is_transparent_deterministic_and_causal():
    points = (_point(0, 0.2, 1.0), _point(1, -0.2, 0.0))
    model = RatingDifferenceBaselineFactory().fit(
        "baseline-v1", points, training_cutoff=T2
    )
    assert model.training_count == 2
    assert model.predict_feature(Decimal("0.3"), decision_at=T3) == pytest.approx(0.65)
    assert model.predict_feature(Decimal("-1"), decision_at=T3) == 0.0
    assert model.predict_feature(Decimal("1"), decision_at=T3) == 1.0

    with pytest.raises(ParticipantStrengthError, match="training cutoff exceeds"):
        model.predict_feature(Decimal("0"), decision_at=T1)


def test_calibrated_challenger_uses_only_revealed_training_evidence_and_roundtrips():
    points = (
        _point(0, 0.6, 0.0),
        _point(1, 0.6, 0.0),
        _point(2, 0.6, 1.0),
        _point(3, -0.6, 0.0),
        TrainingPoint(
            observed_at=T4,
            feature=0.6,
            target=1.0,
            target_available_at=T6,
            evidence_sha256s=(SHA_F,),
        ),
    )
    model = HistogramCalibratedStrengthFactory(bin_count=4, prior_weight="2").fit(
        "challenger-v1", points, training_cutoff=T5
    )
    assert model.training_count == 4
    assert sum(model.bin_counts) == 4
    assert model.predict_feature(0.6, decision_at=T5) == pytest.approx(0.52)
    assert 0.0 <= model.predict_feature(-0.2, decision_at=T5) <= 1.0

    restored = HistogramCalibratedStrengthModel.from_payload(model.to_payload())
    assert restored == model
    assert restored.identity_sha256 == model.identity_sha256

    with pytest.raises(ParticipantStrengthError, match="causally revealed"):
        HistogramCalibratedStrengthFactory().fit(
            "future-only",
            (
                TrainingPoint(
                    observed_at=T4,
                    feature=0.2,
                    target=1.0,
                    target_available_at=T6,
                    evidence_sha256s=(SHA_A,),
                ),
            ),
            training_cutoff=T5,
        )


def test_calibrated_factory_runs_through_existing_causal_walk_forward_contract():
    points = (
        _point(0, -0.5, 0.0),
        _point(1, -0.2, 0.0),
        _point(2, 0.1, 1.0),
        _point(3, 0.4, 1.0),
        _point(4, 0.7, 1.0),
    )
    result = WalkForwardRunner.run(
        points,
        minimum_train_size=2,
        model_factory=HistogramCalibratedStrengthFactory(
            bin_count=4, prior_weight="2"
        ),
    )
    assert result.model_family == "participant-rating-histogram-calibrated-v1"
    assert result.primary_metric == "mse"
    assert result.folds
    assert all(
        fold.training_cutoff < fold.evaluation_at for fold in result.folds
    )
    assert all(
        fold.causal_training_count >= 2 for fold in result.folds
    )


def test_registered_forecast_reloads_hash_bound_factory_artifact_and_registry(tmp_path):
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )
    artifacts = FactoryArtifactStore(tmp_path / "factory-artifacts")
    points = (
        _point(0, 0.6, 0.0),
        _point(1, 0.6, 1.0),
        _point(2, -0.6, 0.0),
    )
    model = HistogramCalibratedStrengthFactory(
        bin_count=4, prior_weight="2"
    ).fit("model-strength-v1", points, training_cutoff=T3)
    config_sha = SHA_C
    payload = model.to_payload()
    payload.update(
        {
            "model_version_id": "model-strength-v1",
            "research_protocol_id": "protocol-strength-v1",
            "dataset_snapshot_id": "dataset-strength-v1",
            "feature_set_id": "features-strength-v1",
            "config_sha256": config_sha,
            "evaluator_config_sha256": SHA_D,
            "training_points_manifest_sha256": model.training_manifest_sha256,
            "seed": 7,
        }
    )
    artifact_sha = artifacts.write("model", "model-strength-v1", payload)
    registry.append(
        FeatureSet(
            feature_set_id="features-strength-v1",
            version="1",
            definition_sha256=SHA_D,
            source_sha256=SHA_A,
            available_at_utc=T1,
        )
    )
    question_sha, hypothesis_sha = _append_preregistration(
        registry,
        question_id="question-strength-v1",
        hypothesis_id="hypothesis-strength-v1",
    )
    binding = ScientificProtocolBinding(
        research_protocol_id="protocol-strength-v1",
        research_question_id="question-strength-v1",
        research_question_sha256=question_sha,
        hypothesis_id="hypothesis-strength-v1",
        hypothesis_sha256=hypothesis_sha,
        inclusion_criteria="causal canonical rating snapshot pairs only",
        exclusion_criteria="insufficient or future-contaminated evidence",
        lawful_source_requirements="canonical Autosport dataset entitlement",
        causal_cutoff=T3,
        evaluation_design="frozen causal walk-forward test fixture",
        feature_set_version="1",
        uncertainty_method="descriptive snapshot radius plus calibration evidence",
        multiple_comparison_control="single challenger",
        robustness_checks=("restart", "future-leakage"),
        random_seed_policy="deterministic seed 7",
        stopping_rule="fixed fixture",
        promotion_rule="no promotion in this forecast-emission test",
        expected_artifacts=("model",),
        code_config_sha256=config_sha,
        frozen_at_utc=T1,
    )
    registry.append(
        ResearchProtocol(
            binding=binding,
            source_sha256=SHA_A,
            environment_sha256=SHA_B,
            dataset_manifest_sha256=model.training_manifest_sha256,
            available_at_utc=T1,
        )
    )
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id="dataset-strength-v1",
            manifest_sha256=model.training_manifest_sha256,
            source_identity="test-causal-rating-snapshots",
            license_identity="test-fixture",
            causal_cutoff=T3,
            available_at_utc=T3,
        )
    )
    registry.append(
        ModelVersion(
            model_version_id="model-strength-v1",
            model_family=model.model_family,
            artifact_sha256=artifact_sha,
            source_sha256=SHA_A,
            environment_sha256=SHA_B,
            dataset_snapshot_id="dataset-strength-v1",
            feature_set_id="features-strength-v1",
            research_protocol_id="protocol-strength-v1",
            seed=7,
            config_sha256=config_sha,
            created_at=T3,
        )
    )
    registry.append(
        StrategyVersion(
            strategy_version_id="strategy-strength-v1",
            canonical_strategy_id="participant-strength",
            source_sha256=SHA_A,
            environment_sha256=SHA_B,
            config_sha256=config_sha,
            created_at=T3,
            model_version_id="model-strength-v1",
        )
    )

    evidence = _pair(decision_at=T4)
    forecast = emit_registered_strength_forecast(
        registry=registry,
        artifact_store=artifacts,
        evidence=evidence,
        model_version_id="model-strength-v1",
        strategy_version_id="strategy-strength-v1",
        quote_key="event-1:match-winner:participant-a",
    )
    assert Decimal("0") <= forecast.probability <= Decimal("1")
    assert forecast.model_training_cutoff_ts == T3
    assert forecast.evidence_hashes[:2] == (SHA_A, SHA_B)
    assert len(forecast.evidence_hashes) == 10
    assert forecast.provenance["dataset_snapshot_id"] == "dataset-strength-v1"
    assert forecast.provenance["feature_set_id"] == "features-strength-v1"
    assert forecast.provenance["research_protocol_id"] == "protocol-strength-v1"
    assert (
        forecast.provenance["training_manifest_sha256"]
        == model.training_manifest_sha256
    )
    assert forecast.provenance["calibration_evidence"]["training_count"] == 3

    reopened_registry = ScientificRegistry(tmp_path / "scientific-registry.json")
    reopened_artifacts = FactoryArtifactStore(tmp_path / "factory-artifacts")
    replay = emit_registered_strength_forecast(
        registry=reopened_registry,
        artifact_store=reopened_artifacts,
        evidence=evidence,
        model_version_id="model-strength-v1",
        strategy_version_id="strategy-strength-v1",
        quote_key="event-1:match-winner:participant-a",
    )
    assert replay.canonical_hash == forecast.canonical_hash


def test_registered_forecast_rejects_future_model_registry_availability(tmp_path):
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )
    artifacts = FactoryArtifactStore(tmp_path / "factory-artifacts")
    points = (_point(0, 0.2, 1.0), _point(1, -0.2, 0.0))
    model = RatingDifferenceBaselineFactory().fit(
        "model-future", points, training_cutoff=T2
    )
    payload = model.to_payload()
    payload.update(
        {
            "model_version_id": "model-future",
            "config_sha256": SHA_C,
        }
    )
    artifact_sha = artifacts.write("model", "model-future", payload)
    registry.append(
        ModelVersion(
            model_version_id="model-future",
            model_family=model.model_family,
            artifact_sha256=artifact_sha,
            source_sha256=SHA_A,
            environment_sha256=SHA_B,
            dataset_snapshot_id="dataset-v1",
            feature_set_id="features-v1",
            research_protocol_id="protocol-v1",
            seed=1,
            config_sha256=SHA_C,
            created_at=T4,
        )
    )
    registry.append(
        StrategyVersion(
            strategy_version_id="strategy-future",
            canonical_strategy_id="participant-strength",
            source_sha256=SHA_A,
            environment_sha256=SHA_B,
            config_sha256=SHA_C,
            created_at=T4,
            model_version_id="model-future",
        )
    )

    with pytest.raises(
        ParticipantStrengthError, match="model version was not available"
    ):
        emit_registered_strength_forecast(
            registry=registry,
            artifact_store=artifacts,
            evidence=_pair(decision_at=T3),
            model_version_id="model-future",
            strategy_version_id="strategy-future",
            quote_key="event-1:match-winner:participant-a",
        )


def test_registered_forecast_rejects_missing_scientific_foundation(tmp_path):
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )
    artifacts = FactoryArtifactStore(tmp_path / "factory-artifacts")
    points = (_point(0, 0.2, 1.0), _point(1, -0.2, 0.0))
    model = RatingDifferenceBaselineFactory().fit(
        "model-orphan", points, training_cutoff=T2
    )
    payload = model.to_payload()
    payload.update(
        {
            "model_version_id": "model-orphan",
            "research_protocol_id": "protocol-missing",
            "dataset_snapshot_id": "dataset-missing",
            "feature_set_id": "features-missing",
            "config_sha256": SHA_C,
            "training_points_manifest_sha256": model.training_manifest_sha256,
        }
    )
    artifact_sha = artifacts.write("model", "model-orphan", payload)
    registry.append(
        ModelVersion(
            model_version_id="model-orphan",
            model_family=model.model_family,
            artifact_sha256=artifact_sha,
            source_sha256=SHA_A,
            environment_sha256=SHA_B,
            dataset_snapshot_id="dataset-missing",
            feature_set_id="features-missing",
            research_protocol_id="protocol-missing",
            seed=1,
            config_sha256=SHA_C,
            created_at=T2,
        )
    )
    registry.append(
        StrategyVersion(
            strategy_version_id="strategy-orphan",
            canonical_strategy_id="participant-strength",
            source_sha256=SHA_A,
            environment_sha256=SHA_B,
            config_sha256=SHA_C,
            created_at=T2,
            model_version_id="model-orphan",
        )
    )

    with pytest.raises(
        ParticipantStrengthError,
        match="lacks DatasetSnapshot/FeatureSet/ResearchProtocol foundation",
    ):
        emit_registered_strength_forecast(
            registry=registry,
            artifact_store=artifacts,
            evidence=_pair(decision_at=T3),
            model_version_id="model-orphan",
            strategy_version_id="strategy-orphan",
            quote_key="event-1:match-winner:participant-a",
        )

def test_registered_forecast_rejects_training_cutoff_after_model_creation(tmp_path):
    registry, artifacts = _registered_strength_lineage(
        tmp_path,
        dataset_cutoff=T4,
        dataset_available_at=T4,
        model_created_at=T2,
        strategy_created_at=T2,
    )

    with pytest.raises(
        ParticipantStrengthError,
        match="DatasetSnapshot causal cutoff exceeds model version availability",
    ):
        emit_registered_strength_forecast(
            registry=registry,
            artifact_store=artifacts,
            evidence=_pair(decision_at=T5),
            model_version_id="model-lineage",
            strategy_version_id="strategy-lineage",
            quote_key="event-1:match-winner:participant-a",
        )


def test_registered_forecast_rejects_strategy_that_predates_bound_model(tmp_path):
    registry, artifacts = _registered_strength_lineage(
        tmp_path,
        dataset_cutoff=T2,
        dataset_available_at=T2,
        model_created_at=T4,
        strategy_created_at=T3,
    )

    with pytest.raises(
        ParticipantStrengthError,
        match="model version was not available when strategy version was created",
    ):
        emit_registered_strength_forecast(
            registry=registry,
            artifact_store=artifacts,
            evidence=_pair(decision_at=T5),
            model_version_id="model-lineage",
            strategy_version_id="strategy-lineage",
            quote_key="event-1:match-winner:participant-a",
        )

def test_registered_forecast_rejects_missing_frozen_preregistration(tmp_path):
    registry, artifacts = _registered_strength_lineage(
        tmp_path,
        dataset_cutoff=T2,
        dataset_available_at=T2,
        model_created_at=T2,
        strategy_created_at=T2,
        include_preregistration=False,
    )

    with pytest.raises(
        ParticipantStrengthError,
        match="lacks frozen ResearchQuestion/Hypothesis",
    ):
        emit_registered_strength_forecast(
            registry=registry,
            artifact_store=artifacts,
            evidence=_pair(decision_at=T3),
            model_version_id="model-lineage",
            strategy_version_id="strategy-lineage",
            quote_key="event-1:match-winner:participant-a",
        )


def test_registered_forecast_rejects_protocol_dataset_cutoff_mismatch(tmp_path):
    registry, artifacts = _registered_strength_lineage(
        tmp_path,
        dataset_cutoff=T2,
        dataset_available_at=T2,
        model_created_at=T2,
        strategy_created_at=T2,
        protocol_cutoff=T1,
    )

    with pytest.raises(
        ParticipantStrengthError,
        match="ResearchProtocol/DatasetSnapshot causal cutoff mismatch",
    ):
        emit_registered_strength_forecast(
            registry=registry,
            artifact_store=artifacts,
            evidence=_pair(decision_at=T3),
            model_version_id="model-lineage",
            strategy_version_id="strategy-lineage",
            quote_key="event-1:match-winner:participant-a",
        )


def test_registered_forecast_rejects_protocol_feature_version_mismatch(tmp_path):
    registry, artifacts = _registered_strength_lineage(
        tmp_path,
        dataset_cutoff=T2,
        dataset_available_at=T2,
        model_created_at=T2,
        strategy_created_at=T2,
        protocol_feature_version="2",
    )

    with pytest.raises(
        ParticipantStrengthError,
        match="FeatureSet version does not match ResearchProtocol",
    ):
        emit_registered_strength_forecast(
            registry=registry,
            artifact_store=artifacts,
            evidence=_pair(decision_at=T3),
            model_version_id="model-lineage",
            strategy_version_id="strategy-lineage",
            quote_key="event-1:match-winner:participant-a",
        )


def test_registered_forecast_rejects_hypothesis_frozen_after_protocol(tmp_path):
    registry, artifacts = _registered_strength_lineage(
        tmp_path,
        dataset_cutoff=T3,
        dataset_available_at=T3,
        model_created_at=T3,
        strategy_created_at=T3,
        hypothesis_at=T2,
        protocol_frozen_at=T1,
    )

    with pytest.raises(
        ParticipantStrengthError,
        match="Hypothesis must precede ResearchProtocol freeze",
    ):
        emit_registered_strength_forecast(
            registry=registry,
            artifact_store=artifacts,
            evidence=_pair(decision_at=T4),
            model_version_id="model-lineage",
            strategy_version_id="strategy-lineage",
            quote_key="event-1:match-winner:participant-a",
        )

def test_registered_forecast_rejects_dataset_cutoff_after_snapshot_availability(tmp_path):
    registry, artifacts = _registered_strength_lineage(
        tmp_path,
        dataset_cutoff=T2,
        dataset_available_at=T1,
        model_created_at=T3,
        strategy_created_at=T3,
    )

    with pytest.raises(
        ParticipantStrengthError,
        match="DatasetSnapshot causal cutoff exceeds snapshot availability",
    ):
        emit_registered_strength_forecast(
            registry=registry,
            artifact_store=artifacts,
            evidence=_pair(decision_at=T4),
            model_version_id="model-lineage",
            strategy_version_id="strategy-lineage",
            quote_key="event-1:match-winner:participant-a",
        )


from autosport.reproducibility_manifest import (
    FactoryReproducibilityManifest,
    derive_walk_forward_splits,
)
from autosport.strategy_model_factory import (
    MeanBaselineModelFactory,
    TrainingPoint,
    WalkForwardRunner,
    training_points_manifest_sha256,
)


T0 = "2026-01-01T00:00:00+00:00"
T1 = "2026-01-02T00:00:00+00:00"
T2 = "2026-01-03T00:00:00+00:00"
T3 = "2026-01-04T00:00:00+00:00"
T4 = "2026-01-05T00:00:00+00:00"
SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64


def test_manifest_split_authority_matches_canonical_walk_forward_runner():
    points = (
        TrainingPoint(T0, 1.0, 1.0, T0),
        TrainingPoint(T1, 2.0, 2.0, T4),
        TrainingPoint(T2, 3.0, 3.0, T2),
        TrainingPoint(T3, 4.0, 4.0, T3),
    )
    walk_forward = WalkForwardRunner.run(points, minimum_train_size=1)
    splits = derive_walk_forward_splits(
        points,
        walk_forward.folds,
        minimum_train_size=1,
    )

    assert tuple(
        (split.fold_id, split.training_indices, split.evaluation_index)
        for split in splits
    ) == (
        ("fold-1", (0,), 1),
        ("fold-2", (0,), 2),
        ("fold-3", (0, 2), 3),
    )
    assert tuple(split.training_cutoff for split in splits) == (T0, T1, T2)
    assert tuple(split.evaluation_at for split in splits) == (T1, T2, T3)

    input_manifest_sha256 = training_points_manifest_sha256(points)
    final_model = MeanBaselineModelFactory().fit(
        "model-1",
        points,
        training_cutoff=T2,
    )
    manifest = FactoryReproducibilityManifest(
        experiment_id="experiment-1",
        evaluation_bundle_id="evaluation-1",
        dataset_snapshot_id="dataset-1",
        dataset_manifest_sha256=input_manifest_sha256,
        training_points_manifest_sha256=input_manifest_sha256,
        dataset_source_identity="lawful-provider:fixture-v1",
        dataset_license_identity="license-evidence:v1",
        splits=splits,
        model_version_id="model-1",
        model_artifact_sha256=SHA_A,
        learner_state_sha256=final_model.identity_sha256,
        model_config_sha256=SHA_B,
        research_protocol_id="protocol-1",
        protocol_sha256=SHA_C,
        evaluator_config_sha256=SHA_D,
        source_sha256=SHA_E,
        evaluator_source_sha256=SHA_F,
        environment_sha256="1" * 64,
        seed=7,
    )

    envelope = manifest.to_envelope()
    assert envelope["dataset"]["training_points_manifest_sha256"] == input_manifest_sha256
    assert envelope["model"]["learner_state_sha256"] == final_model.identity_sha256
    assert len(envelope["manifest_sha256"]) == 64

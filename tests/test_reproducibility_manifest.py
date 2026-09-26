from copy import deepcopy

import pytest

from autosport.reproducibility_manifest import (
    FactoryReproducibilityManifest,
    ReproducibilityManifestError,
    WalkForwardSplit,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64
SHA_F = "f" * 64
SHA_1 = "1" * 64
SHA_2 = "2" * 64
SHA_3 = "3" * 64
SHA_4 = "4" * 64
T1 = "2026-01-02T00:00:00+00:00"
T2 = "2026-01-03T00:00:00+00:00"
T3 = "2026-01-04T00:00:00+00:00"


def _split(
    fold_id="fold-2",
    training_indices=(0, 1),
    evaluation_index=2,
    training_cutoff=T1,
    evaluation_at=T2,
):
    return WalkForwardSplit(
        fold_id,
        training_indices,
        evaluation_index,
        training_cutoff,
        evaluation_at,
    )


def _manifest(**overrides):
    values = {
        "experiment_id": "experiment-1",
        "evaluation_bundle_id": "eval-1",
        "dataset_snapshot_id": "dataset-1",
        "dataset_manifest_sha256": SHA_A,
        "training_points_manifest_sha256": SHA_A,
        "dataset_source_identity": "lawful-provider:fixture-v1",
        "dataset_license_identity": "license-evidence:v1",
        "splits": (
            _split(),
            _split("fold-3", (0, 1, 2), 3, T2, T3),
        ),
        "model_version_id": "model-1",
        "model_artifact_sha256": SHA_B,
        "learner_state_sha256": SHA_C,
        "model_config_sha256": SHA_D,
        "research_protocol_id": "protocol-1",
        "protocol_sha256": SHA_E,
        "evaluator_config_sha256": SHA_F,
        "source_sha256": SHA_1,
        "evaluator_source_sha256": SHA_2,
        "environment_sha256": SHA_3,
        "seed": 7,
    }
    values.update(overrides)
    return FactoryReproducibilityManifest(**values)


def test_manifest_round_trip_is_exact_and_hash_stable():
    first = _manifest()
    second = _manifest()

    assert first.canonical_payload() == second.canonical_payload()
    assert first.manifest_sha256 == second.manifest_sha256
    assert len(first.manifest_sha256) == 64
    assert first.to_envelope()["truth"] == {
        "promotion_claim": False,
        "real_money_execution": False,
    }
    assert FactoryReproducibilityManifest.from_envelope(first.to_envelope()) == first


def test_manifest_binds_exact_dataset_population_and_split_indices():
    envelope = _manifest().to_envelope()

    assert envelope["dataset"] == {
        "dataset_snapshot_id": "dataset-1",
        "dataset_manifest_sha256": SHA_A,
        "training_points_manifest_sha256": SHA_A,
        "input_count": 4,
        "source_identity": "lawful-provider:fixture-v1",
        "license_identity": "license-evidence:v1",
    }
    assert envelope["walk_forward_splits"] == [
        {
            "fold_id": "fold-2",
            "training_indices": [0, 1],
            "evaluation_index": 2,
            "training_cutoff": T1,
            "evaluation_at": T2,
        },
        {
            "fold_id": "fold-3",
            "training_indices": [0, 1, 2],
            "evaluation_index": 3,
            "training_cutoff": T2,
            "evaluation_at": T3,
        },
    ]


def test_manifest_binds_model_learner_config_and_software_environment_lineage():
    envelope = _manifest().to_envelope()

    assert envelope["model"] == {
        "model_version_id": "model-1",
        "model_artifact_sha256": SHA_B,
        "learner_state_sha256": SHA_C,
        "config_sha256": SHA_D,
        "seed": 7,
    }
    assert envelope["research"] == {
        "research_protocol_id": "protocol-1",
        "protocol_sha256": SHA_E,
        "evaluator_config_sha256": SHA_F,
    }
    assert envelope["software_environment"] == {
        "source_sha256": SHA_1,
        "evaluator_source_sha256": SHA_2,
        "environment_sha256": SHA_3,
    }


def test_manifest_rejects_dataset_input_alias_or_mismatch():
    with pytest.raises(
        ReproducibilityManifestError,
        match="factory input manifest must match",
    ):
        _manifest(training_points_manifest_sha256=SHA_4)


def test_split_rejects_noncausal_or_ambiguous_indices():
    with pytest.raises(ReproducibilityManifestError, match="canonical ascending"):
        _split(training_indices=(1, 0))
    with pytest.raises(ReproducibilityManifestError, match="duplicates"):
        _split(training_indices=(0, 0))
    with pytest.raises(ReproducibilityManifestError, match="precede evaluation_index"):
        _split(training_indices=(0, 2))
    with pytest.raises(ReproducibilityManifestError, match="strictly precede"):
        _split(training_cutoff=T2, evaluation_at=T2)


def test_manifest_rejects_duplicate_or_noncanonical_split_order():
    with pytest.raises(
        ReproducibilityManifestError,
        match="fold_id values must be unique",
    ):
        _manifest(splits=(_split(), _split()))
    with pytest.raises(
        ReproducibilityManifestError,
        match="evaluation_index values must be unique",
    ):
        _manifest(
            splits=(
                _split("fold-a", (0,), 2, T1, T2),
                _split("fold-b", (1,), 2, T1, T2),
            )
        )
    with pytest.raises(
        ReproducibilityManifestError,
        match="ordered by evaluation_index",
    ):
        _manifest(
            splits=(
                _split("fold-3", (0, 1, 2), 3, T2, T3),
                _split("fold-2", (0, 1), 2, T1, T2),
            )
        )


def test_from_envelope_fails_closed_on_digest_or_lineage_tamper():
    envelope = _manifest().to_envelope()

    tampered_digest = deepcopy(envelope)
    tampered_digest["model"]["learner_state_sha256"] = SHA_4
    with pytest.raises(ReproducibilityManifestError, match="digest mismatch"):
        FactoryReproducibilityManifest.from_envelope(tampered_digest)

    extra_field = deepcopy(envelope)
    extra_field["dataset"]["raw_rows"] = []
    with pytest.raises(
        ReproducibilityManifestError,
        match="dataset lineage fields mismatch",
    ):
        FactoryReproducibilityManifest.from_envelope(extra_field)

    forged_truth = deepcopy(envelope)
    forged_truth["truth"]["promotion_claim"] = True
    with pytest.raises(ReproducibilityManifestError, match="truth boundary mismatch"):
        FactoryReproducibilityManifest.from_envelope(forged_truth)


def test_from_envelope_rejects_forged_input_count():
    envelope = _manifest().to_envelope()
    envelope["dataset"]["input_count"] = 5
    with pytest.raises(
        ReproducibilityManifestError,
        match="input_count does not match split lineage",
    ):
        FactoryReproducibilityManifest.from_envelope(envelope)


def test_from_envelope_rejects_unknown_top_level_fields_and_noncanonical_sha():
    envelope = _manifest().to_envelope()
    envelope["alias"] = "accepted"
    with pytest.raises(ReproducibilityManifestError, match="envelope fields mismatch"):
        FactoryReproducibilityManifest.from_envelope(envelope)

    with pytest.raises(ReproducibilityManifestError, match="lowercase SHA-256"):
        _manifest(source_sha256=SHA_A.upper())

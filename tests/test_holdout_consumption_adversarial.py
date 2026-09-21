import json

import pytest

from autosport._strategy_model_factory_impl import (
    _holdout_consumed_by_other_evidence,
)
from autosport.scientific_registry import promotion_holdout_access_id


SHA_A = "a" * 64
SHA_B = "b" * 64
T7 = "2026-01-08T00:00:00+00:00"


def _holdout_id(
    *,
    protocol_id: str = "protocol-factory",
    manifest_sha256: str = SHA_A,
    source_identity: str = "lawful-provider:fixture",
    license_identity: str = "license-evidence:v1",
    trial_family_id: str = "protocol-factory:confirmation-trial-family",
) -> str:
    return promotion_holdout_access_id(
        research_protocol_id=protocol_id,
        dataset_manifest_sha256=manifest_sha256,
        source_identity=source_identity,
        license_identity=license_identity,
        confirmation_trial_family_id=trial_family_id,
    )


def _attempt(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "experiment_id": "experiment-v2",
        "research_protocol_id": "protocol-factory",
        "research_question_id": "question-factory",
        "hypothesis_id": "hypothesis-factory",
        "candidate_strategy_version_id": "strategy-v2",
        "candidate_model_version_id": "model-v2",
        "evaluation_bundle_id": "eval-v2",
        "dataset_snapshot_id": "dataset-factory",
        "confirmation_trial_family_id": "protocol-factory:confirmation-trial-family",
        "holdout_access_id": _holdout_id(),
        "estimand": "mse",
        "direction": "LOWER_IS_BETTER",
        "rollback_identity": "strategy-v1",
        "created_at": T7,
    }
    payload.update(overrides)
    return payload


def test_identical_frozen_attempt_remains_resumable_after_json_round_trip():
    frozen_attempt = _attempt()
    persisted_evidence = json.loads(json.dumps(frozen_attempt, sort_keys=True))
    persisted_evidence.update(
        {
            "cohort_id": "dataset-factory",
            "effective_sample_size": 5,
            "holdout_consumed": False,
            "practical_improvement": "0.15",
        }
    )

    assert not _holdout_consumed_by_other_evidence(
        (persisted_evidence,),
        same_attempt_identity=frozen_attempt,
    )


@pytest.mark.parametrize(
    ("field", "changed_value"),
    (
        ("experiment_id", "experiment-other"),
        ("research_protocol_id", "protocol-other"),
        ("research_question_id", "question-other"),
        ("hypothesis_id", "hypothesis-other"),
        ("candidate_strategy_version_id", "strategy-other"),
        ("candidate_model_version_id", "model-other"),
        ("evaluation_bundle_id", "eval-other"),
        ("dataset_snapshot_id", "dataset-renamed"),
        (
            "confirmation_trial_family_id",
            "protocol-factory:confirmation-trial-family-other",
        ),
        ("estimand", "log_loss"),
        ("direction", "HIGHER_IS_BETTER"),
        ("rollback_identity", "strategy-other"),
        ("created_at", "2026-01-09T00:00:00+00:00"),
    ),
)
def test_same_holdout_is_consumed_when_any_frozen_attempt_identity_changes(
    field: str,
    changed_value: object,
):
    frozen_attempt = _attempt()
    prior_evidence = _attempt(**{field: changed_value})

    assert _holdout_consumed_by_other_evidence(
        (prior_evidence,),
        same_attempt_identity=frozen_attempt,
    )


def test_dataset_snapshot_rename_cannot_reopen_same_underlying_holdout():
    first_snapshot = _attempt(dataset_snapshot_id="dataset-before-rename")
    renamed_snapshot = _attempt(dataset_snapshot_id="dataset-after-rename")

    assert first_snapshot["holdout_access_id"] == renamed_snapshot["holdout_access_id"]
    assert _holdout_consumed_by_other_evidence(
        (first_snapshot,),
        same_attempt_identity=renamed_snapshot,
    )


@pytest.mark.parametrize(
    "missing_field",
    (
        "experiment_id",
        "research_protocol_id",
        "research_question_id",
        "hypothesis_id",
        "candidate_strategy_version_id",
        "candidate_model_version_id",
        "evaluation_bundle_id",
        "dataset_snapshot_id",
        "confirmation_trial_family_id",
        "estimand",
        "direction",
        "rollback_identity",
        "created_at",
    ),
)
def test_same_holdout_with_incomplete_prior_identity_fails_closed(missing_field: str):
    frozen_attempt = _attempt()
    incomplete_prior = _attempt()
    incomplete_prior.pop(missing_field)

    assert _holdout_consumed_by_other_evidence(
        (incomplete_prior,),
        same_attempt_identity=frozen_attempt,
    )


def test_distinct_holdout_does_not_consume_current_attempt():
    frozen_attempt = _attempt()
    unrelated = _attempt(
        experiment_id="experiment-unrelated",
        holdout_access_id=_holdout_id(source_identity="lawful-provider:other"),
    )

    assert not _holdout_consumed_by_other_evidence(
        (unrelated,),
        same_attempt_identity=frozen_attempt,
    )


def test_one_conflicting_record_consumes_holdout_regardless_of_record_order():
    frozen_attempt = _attempt()
    same = _attempt()
    conflicting = _attempt(experiment_id="experiment-other")

    assert _holdout_consumed_by_other_evidence(
        (same, conflicting),
        same_attempt_identity=frozen_attempt,
    )
    assert _holdout_consumed_by_other_evidence(
        (conflicting, same),
        same_attempt_identity=frozen_attempt,
    )


@pytest.mark.parametrize(
    "overrides",
    (
        {"protocol_id": "protocol-other"},
        {"manifest_sha256": SHA_B},
        {"source_identity": "lawful-provider:other"},
        {"license_identity": "license-evidence:v2"},
        {"trial_family_id": "protocol-factory:confirmation-trial-family-other"},
    ),
)
def test_holdout_identity_does_not_collapse_distinct_authority(
    overrides: dict[str, str],
):
    assert _holdout_id(**overrides) != _holdout_id()

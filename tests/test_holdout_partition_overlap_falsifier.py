from __future__ import annotations

from dataclasses import dataclass

import pytest

from autosport import _holdout_physical_content_guard as guard
from autosport.scientific_registry import promotion_holdout_access_id


SHA_A = "a" * 64
SHA_B = "b" * 64
OBS_A = "c" * 64
OBS_B = "d" * 64
OBS_C = "e" * 64
OBS_D = "f" * 64
T7 = "2026-01-08T00:00:00+00:00"


def _holdout_id(*, protocol_id: str, manifest_sha256: str) -> str:
    return promotion_holdout_access_id(
        research_protocol_id=protocol_id,
        dataset_manifest_sha256=manifest_sha256,
        source_identity="lawful-provider:fixture",
        license_identity="license-evidence:v1",
        confirmation_trial_family_id=f"{protocol_id}:confirmation-trial-family",
    )


def _attempt(
    *,
    experiment_id: str,
    protocol_id: str,
    dataset_snapshot_id: str,
    manifest_sha256: str,
) -> dict[str, object]:
    return {
        "experiment_id": experiment_id,
        "research_protocol_id": protocol_id,
        "research_question_id": "question-factory",
        "hypothesis_id": "hypothesis-factory",
        "candidate_strategy_version_id": f"strategy:{experiment_id}",
        "candidate_model_version_id": f"model:{experiment_id}",
        "evaluation_bundle_id": f"eval:{experiment_id}",
        "dataset_snapshot_id": dataset_snapshot_id,
        "confirmation_trial_family_id": f"{protocol_id}:confirmation-trial-family",
        "holdout_access_id": _holdout_id(
            protocol_id=protocol_id,
            manifest_sha256=manifest_sha256,
        ),
        "estimand": "mse",
        "direction": "LOWER_IS_BETTER",
        "rollback_identity": "strategy-v1",
        "created_at": T7,
    }


@dataclass(frozen=True)
class _Record:
    payload: dict[str, object]


class _MembershipRegistry:
    """Minimal immutable DatasetSnapshot read surface with observation membership."""

    def __init__(
        self,
        snapshots: dict[str, tuple[str, tuple[str, ...] | None]],
    ) -> None:
        self._snapshots = snapshots

    def get(self, kind: str, record_id: str):
        assert kind == "DatasetSnapshot"
        value = self._snapshots.get(record_id)
        if value is None:
            return None
        manifest, membership = value
        payload: dict[str, object] = {"manifest_sha256": manifest}
        if membership is not None:
            payload["observation_membership_sha256s"] = list(membership)
        return _Record(payload)


def _prior_current() -> tuple[dict[str, object], dict[str, object]]:
    prior = _attempt(
        experiment_id="experiment-prior",
        protocol_id="protocol-prior",
        dataset_snapshot_id="dataset-prior",
        manifest_sha256=SHA_A,
    )
    current = _attempt(
        experiment_id="experiment-current",
        protocol_id="protocol-current",
        dataset_snapshot_id="dataset-current",
        manifest_sha256=SHA_B,
    )
    return prior, current


def test_distinct_manifests_with_overlapping_observations_are_already_consumed() -> None:
    prior, current = _prior_current()
    registry = _MembershipRegistry(
        {
            "dataset-prior": (SHA_A, (OBS_A, OBS_B)),
            "dataset-current": (SHA_B, (OBS_B, OBS_C)),
        }
    )

    assert guard._holdout_consumed_by_physical_evidence(
        (prior,),
        same_attempt_identity=current,
        registry=registry,
    )


def test_distinct_manifests_with_proven_disjoint_membership_remain_fresh() -> None:
    prior, current = _prior_current()
    registry = _MembershipRegistry(
        {
            "dataset-prior": (SHA_A, (OBS_A, OBS_B)),
            "dataset-current": (SHA_B, (OBS_C, OBS_D)),
        }
    )

    assert not guard._holdout_consumed_by_physical_evidence(
        (prior,),
        same_attempt_identity=current,
        registry=registry,
    )


@pytest.mark.parametrize(
    "snapshots",
    (
        {
            "dataset-prior": (SHA_A, (OBS_A, OBS_B)),
            "dataset-current": (SHA_B, None),
        },
        {
            "dataset-prior": (SHA_A, None),
            "dataset-current": (SHA_B, (OBS_C, OBS_D)),
        },
    ),
)
def test_distinct_manifests_without_bilateral_membership_fail_closed(
    snapshots: dict[str, tuple[str, tuple[str, ...] | None]],
) -> None:
    prior, current = _prior_current()
    registry = _MembershipRegistry(snapshots)

    assert guard._holdout_consumed_by_physical_evidence(
        (prior,),
        same_attempt_identity=current,
        registry=registry,
    )


def test_empty_membership_is_not_disjointness_evidence() -> None:
    prior, current = _prior_current()
    registry = _MembershipRegistry(
        {
            "dataset-prior": (SHA_A, ()),
            "dataset-current": (SHA_B, (OBS_C, OBS_D)),
        }
    )

    with pytest.raises(ValueError, match="must be a non-empty JSON list"):
        guard._holdout_consumed_by_physical_evidence(
            (prior,),
            same_attempt_identity=current,
            registry=registry,
        )


def test_duplicate_membership_identity_is_rejected() -> None:
    prior, current = _prior_current()
    registry = _MembershipRegistry(
        {
            "dataset-prior": (SHA_A, (OBS_A, OBS_A)),
            "dataset-current": (SHA_B, (OBS_C, OBS_D)),
        }
    )

    with pytest.raises(ValueError, match="contains duplicates"):
        guard._holdout_consumed_by_physical_evidence(
            (prior,),
            same_attempt_identity=current,
            registry=registry,
        )

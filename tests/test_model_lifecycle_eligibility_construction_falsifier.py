import pytest

from autosport.model_lifecycle import ModelEligibility


def _kwargs() -> dict[str, object]:
    return {
        "model_version_id": "model-v17",
        "model_artifact_sha256": "a" * 64,
        "lifecycle_revision": 1,
        "lifecycle_fingerprint_sha256": "b" * 64,
        "evaluated_at": "2026-09-21T07:30:00+00:00",
        "eligible": True,
        "reasons": (),
    }


def test_caller_cannot_mint_positive_model_lifecycle_eligibility() -> None:
    # ModelEligibility is a positive lifecycle prerequisite consumed downstream.
    # A caller that never supplied/evaluated a ModelLifecycleRevision must not be
    # able to manufacture the same result type returned by
    # evaluate_model_eligibility().
    with pytest.raises(TypeError):
        ModelEligibility(**_kwargs())


def test_caller_cannot_construct_internally_contradictory_eligibility() -> None:
    values = _kwargs()
    values["reasons"] = ("knowledge_expired",)

    # Even if the canonical repair keeps a public constructor, eligible=True
    # cannot coexist with a fail-closed rejection reason. Current parent accepts
    # this forged object unchanged.
    with pytest.raises((TypeError, ValueError)):
        ModelEligibility(**values)

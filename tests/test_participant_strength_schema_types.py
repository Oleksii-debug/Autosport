from __future__ import annotations

import pytest

from autosport.participant_strength import (
    HistogramCalibratedStrengthModel,
    ParticipantStrengthError,
    RatingDifferenceBaselineModel,
)


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_baseline_artifact_schema_version_requires_exact_integer(
    schema_version: object,
) -> None:
    model = RatingDifferenceBaselineModel(
        model_id="baseline-schema-types",
        training_cutoff="2026-01-01T00:00:00Z",
        training_count=1,
        training_manifest_sha256="a" * 64,
    )
    payload = model.to_payload()
    payload["schema_version"] = schema_version

    with pytest.raises(
        ParticipantStrengthError,
        match="unsupported baseline artifact schema",
    ):
        RatingDifferenceBaselineModel.from_payload(payload)


@pytest.mark.parametrize("schema_version", [True, 1.0])
def test_calibrated_artifact_schema_version_requires_exact_integer(
    schema_version: object,
) -> None:
    model = HistogramCalibratedStrengthModel(
        model_id="calibrated-schema-types",
        training_cutoff="2026-01-01T00:00:00Z",
        training_count=1,
        training_manifest_sha256="b" * 64,
        bin_count=2,
        prior_weight="2",
        bin_counts=(1, 0),
        bin_probabilities=("0.5", None),
    )
    payload = model.to_payload()
    payload["schema_version"] = schema_version

    with pytest.raises(
        ParticipantStrengthError,
        match="unsupported calibrated artifact schema",
    ):
        HistogramCalibratedStrengthModel.from_payload(payload)

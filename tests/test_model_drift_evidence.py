from __future__ import annotations

import hashlib
from decimal import ROUND_UP, localcontext

import pytest

from autosport.model_drift_evidence import (
    DriftObservation,
    DriftWindow,
    ModelDriftEvidenceError,
    TwoSampleKSEvidence,
    build_two_sample_ks_evidence,
    verify_two_sample_ks_evidence,
)


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _obs(
    sample_id: str,
    value: str,
    minute: int,
    *,
    day: int = 1,
    artifact: str | None = None,
) -> DriftObservation:
    return DriftObservation(
        sample_id=sample_id,
        observed_at=f"2026-01-{day:02d}T00:{minute:02d}:00Z",
        available_at=f"2026-01-{day:02d}T00:{minute:02d}:30+00:00",
        value=value,
        evidence_sha256=artifact or _hash("evidence:" + sample_id),
    )


def _window(
    window_id: str,
    values: tuple[str, ...],
    *,
    day: int,
    model_id: str = "model:alpha",
    artifact_sha256: str | None = None,
    metric_key: str = "calibrated_probability",
) -> DriftWindow:
    observations = tuple(
        _obs(f"{window_id}:{index}", value, index, day=day)
        for index, value in enumerate(values)
    )
    return DriftWindow(
        window_id=window_id,
        model_id=model_id,
        model_artifact_sha256=artifact_sha256 or _hash("model:alpha:v1"),
        metric_key=metric_key,
        window_start=f"2026-01-{day:02d}T00:00:00Z",
        window_end=f"2026-01-{day:02d}T01:00:00Z",
        observations=observations,
    )


def test_exact_ks_statistic_is_rational_and_deterministic_without_lifecycle_verdict():
    reference = _window("reference", ("1", "2", "3"), day=1)
    current = _window("current", ("2", "3", "4"), day=2)

    evidence = build_two_sample_ks_evidence(
        reference,
        current,
        evaluated_at="2026-01-02T01:00:00Z",
    )
    payload = evidence.to_payload()

    assert type(evidence) is TwoSampleKSEvidence
    assert evidence.ks_numerator == 1
    assert evidence.ks_denominator == 3
    assert evidence.max_difference_at == "1"
    assert payload["truth"]["threshold_applied"] is False
    assert payload["truth"]["lifecycle_classification_produced"] is False
    assert payload["truth"]["statistical_significance_claimed"] is False
    assert payload["truth"]["promotion_authorized"] is False
    assert payload["truth"]["execution_authorized"] is False
    assert "stable" not in payload
    assert "warning" not in payload
    assert "breach" not in payload
    assert evidence.evidence_sha256 == build_two_sample_ks_evidence(
        reference,
        current,
        evaluated_at="2026-01-02T01:00:00+00:00",
    ).evidence_sha256


def test_identical_empirical_distributions_produce_exact_zero():
    reference = _window("reference", ("1.0", "2.00", "3.000"), day=1)
    current = _window("current", ("1", "2", "3"), day=2)

    evidence = build_two_sample_ks_evidence(
        reference,
        current,
        evaluated_at="2026-01-02T01:00:00Z",
    )

    assert evidence.ks_numerator == 0
    assert evidence.ks_denominator == 1
    assert evidence.max_difference_at is None


def test_windows_must_be_causally_disjoint_and_complete_before_evaluation():
    reference = _window("reference", ("1",), day=1)
    overlapping = DriftWindow(
        window_id="current",
        model_id=reference.model_id,
        model_artifact_sha256=reference.model_artifact_sha256,
        metric_key=reference.metric_key,
        window_start="2026-01-01T00:30:00Z",
        window_end="2026-01-01T02:00:00Z",
        observations=(
            DriftObservation(
                sample_id="current:0",
                observed_at="2026-01-01T00:45:00Z",
                available_at="2026-01-01T00:45:30Z",
                value="2",
                evidence_sha256=_hash("current:0"),
            ),
        ),
    )

    with pytest.raises(ModelDriftEvidenceError, match="must not overlap"):
        build_two_sample_ks_evidence(
            reference,
            overlapping,
            evaluated_at="2026-01-01T02:00:00Z",
        )

    current = _window("current", ("2",), day=2)
    with pytest.raises(ModelDriftEvidenceError, match="complete current window"):
        build_two_sample_ks_evidence(
            reference,
            current,
            evaluated_at="2026-01-02T00:59:59Z",
        )


def test_cross_model_artifact_or_metric_comparison_fails_closed():
    reference = _window("reference", ("1", "2"), day=1)

    with pytest.raises(ModelDriftEvidenceError, match="same model/artifact/metric"):
        build_two_sample_ks_evidence(
            reference,
            _window("current", ("1", "2"), day=2, model_id="model:beta"),
            evaluated_at="2026-01-02T01:00:00Z",
        )

    with pytest.raises(ModelDriftEvidenceError, match="same model/artifact/metric"):
        build_two_sample_ks_evidence(
            reference,
            _window(
                "current",
                ("1", "2"),
                day=2,
                artifact_sha256=_hash("model:alpha:v2"),
            ),
            evaluated_at="2026-01-02T01:00:00Z",
        )

    with pytest.raises(ModelDriftEvidenceError, match="same model/artifact/metric"):
        build_two_sample_ks_evidence(
            reference,
            _window(
                "current",
                ("1", "2"),
                day=2,
                metric_key="raw_logit",
            ),
            evaluated_at="2026-01-02T01:00:00Z",
        )


def test_reused_sample_identity_across_windows_fails_closed():
    shared = DriftObservation(
        sample_id="shared",
        observed_at="2026-01-01T00:10:00Z",
        available_at="2026-01-01T00:10:30Z",
        value="1",
        evidence_sha256=_hash("shared:reference"),
    )
    reference = DriftWindow(
        window_id="reference",
        model_id="model:alpha",
        model_artifact_sha256=_hash("model:alpha:v1"),
        metric_key="score",
        window_start="2026-01-01T00:00:00Z",
        window_end="2026-01-01T01:00:00Z",
        observations=(shared,),
    )
    current = DriftWindow(
        window_id="current",
        model_id="model:alpha",
        model_artifact_sha256=_hash("model:alpha:v1"),
        metric_key="score",
        window_start="2026-01-02T00:00:00Z",
        window_end="2026-01-02T01:00:00Z",
        observations=(
            DriftObservation(
                sample_id="shared",
                observed_at="2026-01-02T00:10:00Z",
                available_at="2026-01-02T00:10:30Z",
                value="2",
                evidence_sha256=_hash("shared:current"),
            ),
        ),
    )

    with pytest.raises(ModelDriftEvidenceError, match="must not reuse sample_id"):
        build_two_sample_ks_evidence(
            reference,
            current,
            evaluated_at="2026-01-02T01:00:00Z",
        )


def test_window_rejects_noncausal_or_noncanonical_observations():
    with pytest.raises(ModelDriftEvidenceError, match="must not precede observed_at"):
        DriftObservation(
            sample_id="bad",
            observed_at="2026-01-01T00:10:00Z",
            available_at="2026-01-01T00:09:59Z",
            value="1",
            evidence_sha256=_hash("bad"),
        )

    first = _obs("a", "1", 1)
    second = _obs("b", "2", 2)
    with pytest.raises(ModelDriftEvidenceError, match="canonical causal ordering"):
        DriftWindow(
            window_id="reference",
            model_id="model:alpha",
            model_artifact_sha256=_hash("model:alpha:v1"),
            metric_key="score",
            window_start="2026-01-01T00:00:00Z",
            window_end="2026-01-01T01:00:00Z",
            observations=(second, first),
        )


def test_observation_value_normalization_and_evidence_identity_are_context_independent():
    def build(precision: int):
        with localcontext() as context:
            context.prec = precision
            context.rounding = ROUND_UP
            reference = _window(
                "reference",
                (
                    "0.1000000000000000000000000000000001",
                    "0.2000000000000000000000000000000002",
                ),
                day=1,
            )
            current = _window(
                "current",
                (
                    "0.1000000000000000000000000000000001",
                    "0.3000000000000000000000000000000003",
                ),
                day=2,
            )
            evidence = build_two_sample_ks_evidence(
                reference,
                current,
                evaluated_at="2026-01-02T01:00:00Z",
            )
            return (
                reference.to_payload(),
                current.to_payload(),
                evidence.to_payload(),
                evidence.evidence_sha256,
            )

    assert build(8) == build(60)


def test_one_observation_identity_change_changes_window_and_evidence_identity():
    reference = _window("reference", ("1", "2"), day=1)
    current = _window("current", ("2", "3"), day=2)
    changed = DriftWindow(
        window_id=current.window_id,
        model_id=current.model_id,
        model_artifact_sha256=current.model_artifact_sha256,
        metric_key=current.metric_key,
        window_start=current.window_start,
        window_end=current.window_end,
        observations=(
            current.observations[0],
            DriftObservation(
                sample_id=current.observations[1].sample_id,
                observed_at=current.observations[1].observed_at,
                available_at=current.observations[1].available_at,
                value=current.observations[1].value,
                evidence_sha256=_hash("different-source-row"),
            ),
        ),
    )

    first = build_two_sample_ks_evidence(
        reference,
        current,
        evaluated_at="2026-01-02T01:00:00Z",
    )
    second = build_two_sample_ks_evidence(
        reference,
        changed,
        evaluated_at="2026-01-02T01:00:00Z",
    )

    assert current.identity_sha256 != changed.identity_sha256
    assert first.evidence_sha256 != second.evidence_sha256


def test_window_binds_availability_time_as_the_causal_window_basis():
    reference = _window("reference", ("1", "2"), day=1)
    payload = reference.to_payload()

    assert payload["window_basis"] == "available_at"


def test_reused_source_evidence_identity_fails_closed_even_under_new_sample_alias():
    shared_evidence = _hash("same-underlying-evidence")
    reference = DriftWindow(
        window_id="reference",
        model_id="model:alpha",
        model_artifact_sha256=_hash("model:alpha:v1"),
        metric_key="score",
        window_start="2026-01-01T00:00:00Z",
        window_end="2026-01-01T01:00:00Z",
        observations=(
            DriftObservation(
                sample_id="reference-row",
                observed_at="2026-01-01T00:10:00Z",
                available_at="2026-01-01T00:10:30Z",
                value="1",
                evidence_sha256=shared_evidence,
            ),
        ),
    )
    current = DriftWindow(
        window_id="current",
        model_id="model:alpha",
        model_artifact_sha256=_hash("model:alpha:v1"),
        metric_key="score",
        window_start="2026-01-02T00:00:00Z",
        window_end="2026-01-02T01:00:00Z",
        observations=(
            DriftObservation(
                sample_id="renamed-current-row",
                observed_at="2026-01-02T00:10:00Z",
                available_at="2026-01-02T00:10:30Z",
                value="2",
                evidence_sha256=shared_evidence,
            ),
        ),
    )

    with pytest.raises(ModelDriftEvidenceError, match="must not reuse evidence_sha256"):
        build_two_sample_ks_evidence(
            reference,
            current,
            evaluated_at="2026-01-02T01:00:00Z",
        )


def test_duplicate_source_evidence_within_one_window_fails_closed():
    evidence = _hash("duplicated-source")
    first = DriftObservation(
        sample_id="a",
        observed_at="2026-01-01T00:01:00Z",
        available_at="2026-01-01T00:01:30Z",
        value="1",
        evidence_sha256=evidence,
    )
    second = DriftObservation(
        sample_id="b",
        observed_at="2026-01-01T00:02:00Z",
        available_at="2026-01-01T00:02:30Z",
        value="2",
        evidence_sha256=evidence,
    )

    with pytest.raises(ModelDriftEvidenceError, match="evidence_sha256 values must be unique"):
        DriftWindow(
            window_id="reference",
            model_id="model:alpha",
            model_artifact_sha256=_hash("model:alpha:v1"),
            metric_key="score",
            window_start="2026-01-01T00:00:00Z",
            window_end="2026-01-01T01:00:00Z",
            observations=(first, second),
        )


def test_float_measurement_input_is_rejected_instead_of_silently_exactified():
    with pytest.raises(ModelDriftEvidenceError, match="exact decimal"):
        DriftObservation(
            sample_id="float-input",
            observed_at="2026-01-01T00:10:00Z",
            available_at="2026-01-01T00:10:30Z",
            value=0.1,  # type: ignore[arg-type]
            evidence_sha256=_hash("float-input"),
        )


def test_window_subclass_cannot_override_canonical_measurement_authority():
    class ForgedWindow(DriftWindow):
        @property
        def identity_sha256(self) -> str:
            return _hash("forged")

    reference = _window("reference", ("1",), day=1)
    current_base = _window("current", ("2",), day=2)
    current = ForgedWindow(
        window_id=current_base.window_id,
        model_id=current_base.model_id,
        model_artifact_sha256=current_base.model_artifact_sha256,
        metric_key=current_base.metric_key,
        window_start=current_base.window_start,
        window_end=current_base.window_end,
        observations=current_base.observations,
    )

    with pytest.raises(ModelDriftEvidenceError, match="exact DriftWindow"):
        build_two_sample_ks_evidence(
            reference,
            current,
            evaluated_at="2026-01-02T01:00:00Z",
        )


def test_unequal_sample_sizes_and_ties_have_exact_expected_statistic():
    reference = _window("reference", ("0", "1"), day=1)
    current = _window("current", ("1", "1", "2"), day=2)

    evidence = build_two_sample_ks_evidence(
        reference,
        current,
        evaluated_at="2026-01-02T01:00:00Z",
    )

    assert (evidence.ks_numerator, evidence.ks_denominator) == (1, 2)
    assert evidence.max_difference_at == "0"
    assert evidence.to_payload()["window_basis"] == "available_at"


def test_window_ids_must_be_distinct_even_when_time_ranges_are_disjoint():
    reference = _window("same-logical-window", ("1",), day=1)
    current = _window("same-logical-window", ("2",), day=2)

    with pytest.raises(ModelDriftEvidenceError, match="distinct window_id"):
        build_two_sample_ks_evidence(
            reference,
            current,
            evaluated_at="2026-01-02T01:00:00Z",
        )


def test_verifier_rederives_against_exact_windows():
    reference = _window("reference", ("1", "2"), day=1)
    current = _window("current", ("2", "3"), day=2)
    other_current = _window("other-current", ("2", "4"), day=2)
    evidence = build_two_sample_ks_evidence(
        reference,
        current,
        evaluated_at="2026-01-02T01:00:00Z",
    )

    verify_two_sample_ks_evidence(evidence, reference, current)
    with pytest.raises(ModelDriftEvidenceError, match="does not match exact"):
        verify_two_sample_ks_evidence(evidence, reference, other_current)

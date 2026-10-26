from __future__ import annotations

from dataclasses import replace

import pytest

from autosport.operator_responsiveness import (
    ResponsivenessBudget,
    ResponsivenessEvidenceError,
    ResponsivenessReason,
    ResponsivenessSample,
    ResponsivenessStatus,
    evaluate_operator_responsiveness,
)


def _sample(
    index: int,
    duration: int | None,
    *,
    process: str = "process-1",
    clock: str = "perf-counter-ns",
) -> ResponsivenessSample:
    start = index * 1_000
    return ResponsivenessSample(
        sample_id=f"sample-{index:03d}",
        process_instance_id=process,
        clock_domain=clock,
        started_ns=start,
        completed_ns=None if duration is None else start + duration,
    )


def _budget(*, p95: int = 100, maximum: int = 200) -> ResponsivenessBudget:
    return ResponsivenessBudget(p95_ns=p95, max_ns=maximum)


def test_complete_samples_within_budgets_pass_without_claiming_measurement_authority() -> None:
    result = evaluate_operator_responsiveness(
        (_sample(1, 40), _sample(2, 80), _sample(3, 90)),
        budget=_budget(),
    )
    assert result.status is ResponsivenessStatus.PASS
    assert result.reason is ResponsivenessReason.WITHIN_BUDGET
    assert result.within_budget is True
    assert result.p95_ns == 90
    assert result.max_ns == 90
    assert result.measurement_authoritative is False
    assert result.human_tested is False
    assert result.nvda_verified is False


def test_nearest_rank_p95_is_deterministic() -> None:
    samples = tuple(_sample(index, index) for index in range(1, 21))
    result = evaluate_operator_responsiveness(
        samples,
        budget=_budget(p95=19, maximum=20),
    )
    assert result.status is ResponsivenessStatus.PASS
    assert result.p95_ns == 19
    assert result.max_ns == 20


def test_p95_only_budget_failure_is_distinct() -> None:
    result = evaluate_operator_responsiveness(
        tuple(_sample(index, 110 if index < 20 else 150) for index in range(1, 21)),
        budget=_budget(p95=100, maximum=200),
    )
    assert result.status is ResponsivenessStatus.FAIL
    assert result.reason is ResponsivenessReason.P95_BUDGET_EXCEEDED


def test_max_only_budget_failure_is_distinct() -> None:
    durations = [50] * 19 + [250]
    result = evaluate_operator_responsiveness(
        tuple(_sample(index, duration) for index, duration in enumerate(durations, 1)),
        budget=_budget(p95=100, maximum=200),
    )
    assert result.p95_ns == 50
    assert result.max_ns == 250
    assert result.reason is ResponsivenessReason.MAX_BUDGET_EXCEEDED


def test_both_budget_failures_are_distinct() -> None:
    result = evaluate_operator_responsiveness(
        tuple(_sample(index, 250) for index in range(1, 5)),
        budget=_budget(p95=100, maximum=200),
    )
    assert result.reason is ResponsivenessReason.BOTH_BUDGETS_EXCEEDED


def test_incomplete_sample_is_explicit_and_partial_metrics_are_not_published() -> None:
    result = evaluate_operator_responsiveness(
        (_sample(1, 50), _sample(2, None), _sample(3, 60)),
        budget=_budget(),
    )
    assert result.status is ResponsivenessStatus.INCOMPLETE_EVIDENCE
    assert result.reason is ResponsivenessReason.SAMPLE_NOT_COMPLETED
    assert result.sample_count == 3
    assert result.completed_count == 2
    assert result.p95_ns is None
    assert result.max_ns is None
    assert result.evidence_sha256 is not None


def test_sample_order_does_not_change_set_digest_or_metrics() -> None:
    values = (_sample(1, 90), _sample(2, 50), _sample(3, 70))
    first = evaluate_operator_responsiveness(values, budget=_budget())
    second = evaluate_operator_responsiveness(tuple(reversed(values)), budget=_budget())
    assert first.evidence_sha256 == second.evidence_sha256
    assert first.p95_ns == second.p95_ns
    assert first.max_ns == second.max_ns


def test_sample_change_changes_digest() -> None:
    first = evaluate_operator_responsiveness((_sample(1, 50),), budget=_budget())
    second = evaluate_operator_responsiveness((_sample(1, 51),), budget=_budget())
    assert first.evidence_sha256 != second.evidence_sha256


def test_budget_change_changes_digest() -> None:
    samples = (_sample(1, 50),)
    first = evaluate_operator_responsiveness(samples, budget=_budget(p95=100, maximum=200))
    second = evaluate_operator_responsiveness(samples, budget=_budget(p95=101, maximum=200))
    assert first.evidence_sha256 != second.evidence_sha256


def test_duplicate_sample_identity_is_invalid_evidence() -> None:
    result = evaluate_operator_responsiveness(
        (_sample(1, 50), replace(_sample(1, 50), completed_ns=1_060)),
        budget=_budget(),
    )
    assert result.status is ResponsivenessStatus.INVALID_EVIDENCE
    assert result.evidence_sha256 is None


def test_mixed_process_instances_are_invalid_evidence() -> None:
    result = evaluate_operator_responsiveness(
        (_sample(1, 50), _sample(2, 50, process="process-2")),
        budget=_budget(),
    )
    assert result.status is ResponsivenessStatus.INVALID_EVIDENCE


def test_mixed_clock_domains_are_invalid_evidence() -> None:
    result = evaluate_operator_responsiveness(
        (_sample(1, 50), _sample(2, 50, clock="other-clock")),
        budget=_budget(),
    )
    assert result.status is ResponsivenessStatus.INVALID_EVIDENCE


def test_blank_identity_in_serialized_sample_is_invalid_without_digest_exception() -> None:
    raw = _sample(1, 50).to_dict()
    raw["sample_id"] = " "
    result = evaluate_operator_responsiveness((raw,), budget=_budget())
    assert result.status is ResponsivenessStatus.INVALID_EVIDENCE
    assert result.evidence_sha256 is None


def test_negative_timestamp_is_invalid_evidence() -> None:
    raw = _sample(1, 50).to_dict()
    raw["started_ns"] = -1
    result = evaluate_operator_responsiveness((raw,), budget=_budget())
    assert result.status is ResponsivenessStatus.INVALID_EVIDENCE


def test_inverted_timestamp_is_invalid_evidence() -> None:
    raw = _sample(1, 50).to_dict()
    raw["completed_ns"] = raw["started_ns"] - 1
    result = evaluate_operator_responsiveness((raw,), budget=_budget())
    assert result.status is ResponsivenessStatus.INVALID_EVIDENCE


def test_bool_timestamp_is_not_accepted_as_integer() -> None:
    raw = _sample(1, 50).to_dict()
    raw["started_ns"] = True
    result = evaluate_operator_responsiveness((raw,), budget=_budget())
    assert result.status is ResponsivenessStatus.INVALID_EVIDENCE


def test_malformed_sample_container_is_invalid_evidence() -> None:
    result = evaluate_operator_responsiveness("not-samples", budget=_budget())  # type: ignore[arg-type]
    assert result.status is ResponsivenessStatus.INVALID_EVIDENCE


def test_empty_sample_set_is_invalid_evidence() -> None:
    result = evaluate_operator_responsiveness((), budget=_budget())
    assert result.status is ResponsivenessStatus.INVALID_EVIDENCE


def test_budget_requires_exact_positive_integers_and_coherent_thresholds() -> None:
    for raw in (
        {"p95_ns": True, "max_ns": 200},
        {"p95_ns": 0, "max_ns": 200},
        {"p95_ns": 201, "max_ns": 200},
        {"p95_ns": 100, "max_ns": -1},
    ):
        result = evaluate_operator_responsiveness((_sample(1, 50),), budget=raw)
        assert result.status is ResponsivenessStatus.INVALID_EVIDENCE


def test_unknown_serialized_keys_are_invalid_evidence() -> None:
    raw = _sample(1, 50).to_dict()
    raw["extra"] = "ignored?"
    result = evaluate_operator_responsiveness((raw,), budget=_budget())
    assert result.status is ResponsivenessStatus.INVALID_EVIDENCE


def test_low_level_tampered_frozen_sample_is_revalidated() -> None:
    sample = _sample(1, 50)
    object.__setattr__(sample, "completed_ns", -1)
    result = evaluate_operator_responsiveness((sample,), budget=_budget())
    assert result.status is ResponsivenessStatus.INVALID_EVIDENCE


def test_invalid_evidence_never_exposes_partial_trusted_context() -> None:
    raw = _sample(1, 50).to_dict()
    raw["process_instance_id"] = " process-1"
    result = evaluate_operator_responsiveness((raw,), budget=_budget())
    assert result.status is ResponsivenessStatus.INVALID_EVIDENCE
    assert result.process_instance_id is None
    assert result.clock_domain is None
    assert result.budget is None
    assert result.p95_ns is None
    assert result.max_ns is None
    assert result.evidence_sha256 is None


def test_direct_evaluation_cannot_assert_human_or_nvda_truth() -> None:
    result = evaluate_operator_responsiveness((_sample(1, 50),), budget=_budget())
    with pytest.raises(ResponsivenessEvidenceError, match="human_tested"):
        replace(result, human_tested=True)
    with pytest.raises(ResponsivenessEvidenceError, match="nvda_verified"):
        replace(result, nvda_verified=True)
    with pytest.raises(ResponsivenessEvidenceError, match="measurement_authoritative"):
        replace(result, measurement_authoritative=True)

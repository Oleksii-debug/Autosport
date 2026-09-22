from __future__ import annotations

from autosport.drift_control import (
    DriftKind,
    DriftMetric,
    DriftMonitor,
    DriftState,
    DriftWindow,
)
from autosport.model_lifecycle import (
    ModelDriftState,
    ModelLifecycleRevision,
    ModelLifecycleState,
    evaluate_model_eligibility,
)
from autosport.scientific_registry import (
    DatasetSnapshot,
    ExperimentRecord,
    FeatureSet,
    ModelVersion,
    ResearchOutcome,
    ResearchQuestion,
    ScientificRegistry,
    StrategyVersion,
)


SHA_A = "a" * 64
SHA_B = "b" * 64
SHA_C = "c" * 64
SHA_D = "d" * 64
SHA_E = "e" * 64

BASELINE_START = "2026-02-01T00:00:00Z"
BASELINE_END = "2026-02-02T00:00:00Z"
BASELINE_AS_OF = "2026-02-04T00:00:00Z"
CURRENT_START = "2026-02-09T00:00:00Z"
CURRENT_END = "2026-02-10T00:00:00Z"
CURRENT_AS_OF = "2026-02-11T00:00:00Z"
FINDING_AS_OF = "2026-02-12T00:00:00Z"


def _baseline_window() -> DriftWindow:
    return DriftWindow.from_samples(
        dataset_snapshot_id="dataset-baseline",
        source_identity="lawful:feed-a",
        window_start=BASELINE_START,
        window_end=BASELINE_END,
        as_of=BASELINE_AS_OF,
        values=("1", "2"),
        value_observed_at=(
            "2026-02-01T12:00:00Z",
            "2026-02-02T00:00:00Z",
        ),
        value_available_at=(
            "2026-02-03T00:00:00Z",
            "2026-02-03T00:00:00Z",
        ),
    )


def _current_window(values: tuple[str, ...]) -> DriftWindow:
    return DriftWindow.from_samples(
        dataset_snapshot_id="dataset-current",
        source_identity="lawful:feed-a",
        window_start=CURRENT_START,
        window_end=CURRENT_END,
        as_of=CURRENT_AS_OF,
        values=values,
        value_observed_at=(
            "2026-02-09T12:00:00Z",
            "2026-02-10T00:00:00Z",
        ),
        value_available_at=(
            "2026-02-10T12:00:00Z",
            "2026-02-10T12:00:00Z",
        ),
    )


def _registry(tmp_path, *, current: DriftWindow) -> ScientificRegistry:
    baseline = _baseline_window()
    registry = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )
    registry.append(
        ResearchQuestion(
            question_id="question-drift",
            statement="Has the frozen strategy context materially drifted?",
            source_sha256=SHA_A,
            created_at="2026-01-01T00:00:00Z",
        )
    )
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id="dataset-train",
            manifest_sha256=SHA_A,
            source_identity="lawful:feed-a",
            license_identity="license:test",
            causal_cutoff="2026-01-01T00:00:00Z",
            available_at_utc="2026-01-01T12:00:00Z",
        )
    )
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id="dataset-baseline",
            manifest_sha256=baseline.evidence_sha256,
            source_identity="lawful:feed-a",
            license_identity="license:test",
            causal_cutoff=BASELINE_END,
            available_at_utc="2026-02-03T00:00:00Z",
        )
    )
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id="dataset-current",
            manifest_sha256=current.evidence_sha256,
            source_identity="lawful:feed-a",
            license_identity="license:test",
            causal_cutoff=CURRENT_END,
            available_at_utc="2026-02-10T12:00:00Z",
        )
    )
    registry.append(
        FeatureSet(
            feature_set_id="feature-1",
            version="v1",
            definition_sha256=SHA_A,
            source_sha256=SHA_B,
            available_at_utc="2026-01-01T12:00:00Z",
        )
    )
    registry.append(
        ModelVersion(
            model_version_id="model-1",
            model_family="calibrated-baseline",
            artifact_sha256=SHA_A,
            source_sha256=SHA_B,
            environment_sha256=SHA_C,
            dataset_snapshot_id="dataset-train",
            feature_set_id="feature-1",
            research_protocol_id="protocol-1",
            seed=7,
            config_sha256=SHA_D,
            created_at="2026-01-02T00:00:00Z",
        )
    )
    registry.append(
        StrategyVersion(
            strategy_version_id="strategy-1",
            canonical_strategy_id="strategy-context",
            source_sha256=SHA_B,
            environment_sha256=SHA_C,
            config_sha256=SHA_D,
            created_at="2026-01-03T00:00:00Z",
            model_version_id="model-1",
        )
    )
    registry.append(
        ExperimentRecord(
            experiment_id="experiment-baseline",
            research_protocol_id="protocol-1",
            dataset_snapshot_id="dataset-baseline",
            feature_set_id="feature-1",
            strategy_version_id="strategy-1",
            evaluation_bundle_id="eval-baseline",
            seed=7,
            config_sha256=SHA_D,
            outcome=ResearchOutcome.POSITIVE,
            created_at="2026-02-02T12:00:00Z",
            model_version_id="model-1",
            completed_at="2026-02-03T12:00:00Z",
        )
    )
    return registry


def _canonical_finding(
    tmp_path,
    *,
    current_values: tuple[str, ...],
    min_samples: int = 2,
):
    current = _current_window(current_values)
    registry = _registry(tmp_path, current=current)
    monitor = DriftMonitor(registry)
    reference = monitor.create_reference(
        drift_kind=DriftKind.FORECAST_PERFORMANCE,
        metric=DriftMetric.MEAN_ABSOLUTE_SHIFT,
        model_version_id="model-1",
        strategy_version_id="strategy-1",
        feature_set_id="feature-1",
        experiment_id="experiment-baseline",
        baseline=_baseline_window(),
        min_samples=min_samples,
        threshold="0.5",
        metric_definition_sha256=SHA_E,
    )
    finding = monitor.evaluate(
        reference.reference_id,
        current,
        evaluated_at=FINDING_AS_OF,
    )
    # Prove this is not merely a shape-valid caller DTO: the canonical monitor
    # can re-resolve and re-prove the immutable registry lineage.
    monitor.require_canonical_finding(
        finding.finding_id,
        as_of=FINDING_AS_OF,
    )
    return finding


def _lifecycle(evidence_ref: str) -> ModelLifecycleRevision:
    return ModelLifecycleRevision(
        model_version_id="model-1",
        model_artifact_sha256=SHA_A,
        revision=1,
        lifecycle_state=ModelLifecycleState.ACTIVE,
        drift_state=ModelDriftState.STABLE,
        created_at="2026-01-02T00:00:00+00:00",
        updated_at="2026-02-11T00:01:00+00:00",
        knowledge_valid_until="2026-03-01T00:00:00+00:00",
        drift_observed_at="2026-02-11T00:00:00+00:00",
        drift_valid_until="2026-02-13T00:00:00+00:00",
        evidence_refs=(evidence_ref,),
        reason_code="drift_review_stable",
    )


def _eligibility(evidence_ref: str):
    return evaluate_model_eligibility(
        _lifecycle(evidence_ref),
        evaluated_at="2026-02-12T00:00:00+00:00",
    )


def test_free_form_stable_evidence_ref_cannot_mint_positive_eligibility() -> None:
    result = _eligibility("f" * 64)

    assert not result.eligible


def test_canonical_no_drift_finding_is_the_positive_control(tmp_path) -> None:
    finding = _canonical_finding(
        tmp_path,
        current_values=("1.2", "2.2"),
    )
    assert finding.state is DriftState.NO_DRIFT

    # This is the one canonical drift state that may support a positive
    # lifecycle prerequisite, subject to the lifecycle's own time/state gates.
    result = _eligibility(finding.finding_id)
    assert result.eligible


def test_detected_drift_cannot_be_relabelled_stable_by_lifecycle(tmp_path) -> None:
    finding = _canonical_finding(
        tmp_path,
        current_values=("2", "3"),
    )
    assert finding.state is DriftState.DRIFT_DETECTED

    # Caller-selected ModelDriftState.STABLE must not override canonical #532
    # DRIFT_DETECTED truth merely by placing the finding id in evidence_refs.
    result = _eligibility(finding.finding_id)
    assert not result.eligible


def test_insufficient_drift_evidence_cannot_be_relabelled_stable(tmp_path) -> None:
    finding = _canonical_finding(
        tmp_path,
        current_values=("1.2", "2.2"),
        min_samples=3,
    )
    assert finding.state is DriftState.INSUFFICIENT_EVIDENCE

    # INSUFFICIENT_EVIDENCE is not positive stability evidence.
    result = _eligibility(finding.finding_id)
    assert not result.eligible

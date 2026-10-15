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


def _window(
    *,
    dataset_snapshot_id: str,
    window_start: str,
    window_end: str,
    as_of: str,
    values: tuple[str, str],
    observed_at: tuple[str, str],
    available_at: tuple[str, str],
) -> DriftWindow:
    return DriftWindow.from_samples(
        dataset_snapshot_id=dataset_snapshot_id,
        source_identity="lawful:feed-a",
        window_start=window_start,
        window_end=window_end,
        as_of=as_of,
        values=values,
        value_observed_at=observed_at,
        value_available_at=available_at,
    )


def _registry_with_two_findings(tmp_path):
    baseline = _window(
        dataset_snapshot_id="dataset-baseline",
        window_start="2026-02-01T00:00:00Z",
        window_end="2026-02-02T00:00:00Z",
        as_of="2026-02-04T00:00:00Z",
        values=("1", "2"),
        observed_at=(
            "2026-02-01T12:00:00Z",
            "2026-02-02T00:00:00Z",
        ),
        available_at=(
            "2026-02-03T00:00:00Z",
            "2026-02-03T00:00:00Z",
        ),
    )
    stable_current = _window(
        dataset_snapshot_id="dataset-current-stable",
        window_start="2026-02-09T00:00:00Z",
        window_end="2026-02-10T00:00:00Z",
        as_of="2026-02-11T00:00:00Z",
        values=("1.2", "2.2"),
        observed_at=(
            "2026-02-09T12:00:00Z",
            "2026-02-10T00:00:00Z",
        ),
        available_at=(
            "2026-02-10T12:00:00Z",
            "2026-02-10T12:00:00Z",
        ),
    )
    drifted_current = _window(
        dataset_snapshot_id="dataset-current-drifted",
        window_start="2026-02-10T00:00:00Z",
        window_end="2026-02-11T00:00:00Z",
        as_of="2026-02-12T00:00:00Z",
        values=("3", "4"),
        observed_at=(
            "2026-02-10T12:00:00Z",
            "2026-02-11T00:00:00Z",
        ),
        available_at=(
            "2026-02-11T12:00:00Z",
            "2026-02-11T12:00:00Z",
        ),
    )

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
    for window, available_at_utc in (
        (baseline, "2026-02-03T00:00:00Z"),
        (stable_current, "2026-02-10T12:00:00Z"),
        (drifted_current, "2026-02-11T12:00:00Z"),
    ):
        registry.append(
            DatasetSnapshot(
                dataset_snapshot_id=window.dataset_snapshot_id,
                manifest_sha256=window.evidence_sha256,
                source_identity="lawful:feed-a",
                license_identity="license:test",
                causal_cutoff=window.window_end,
                available_at_utc=available_at_utc,
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

    monitor = DriftMonitor(registry)
    reference = monitor.create_reference(
        drift_kind=DriftKind.FORECAST_PERFORMANCE,
        metric=DriftMetric.MEAN_ABSOLUTE_SHIFT,
        model_version_id="model-1",
        strategy_version_id="strategy-1",
        feature_set_id="feature-1",
        experiment_id="experiment-baseline",
        baseline=baseline,
        min_samples=2,
        threshold="0.5",
        metric_definition_sha256=SHA_E,
    )
    stable = monitor.evaluate(
        reference.reference_id,
        stable_current,
        evaluated_at="2026-02-11T01:00:00Z",
    )
    drifted = monitor.evaluate(
        reference.reference_id,
        drifted_current,
        evaluated_at="2026-02-12T01:00:00Z",
    )
    assert stable.state is DriftState.NO_DRIFT
    assert drifted.state is DriftState.DRIFT_DETECTED
    return registry, stable, drifted


def _lifecycle(stable_finding_id: str) -> ModelLifecycleRevision:
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
        evidence_refs=(stable_finding_id,),
        reason_code="drift_review_stable",
    )


def test_older_no_drift_finding_cannot_be_cherry_picked_after_newer_drift(tmp_path) -> None:
    registry, stable, drifted = _registry_with_two_findings(tmp_path)
    lifecycle = _lifecycle(stable.finding_id)

    before_new_finding = evaluate_model_eligibility(
        lifecycle,
        evaluated_at="2026-02-11T12:00:00+00:00",
        scientific_registry=registry,
    )
    assert before_new_finding.eligible

    after_new_finding = evaluate_model_eligibility(
        lifecycle,
        evaluated_at="2026-02-12T12:00:00+00:00",
        scientific_registry=registry,
    )
    assert not after_new_finding.eligible
    assert after_new_finding.reasons == ("canonical_drift_finding_superseded",)

    # The newer canonical record remains independently re-provable and negative.
    DriftMonitor(registry).require_canonical_finding(
        drifted.finding_id,
        as_of="2026-02-12T12:00:00+00:00",
    )

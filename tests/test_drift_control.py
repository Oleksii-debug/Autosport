import json
from dataclasses import replace
from decimal import localcontext

import pytest

from autosport.drift_control import (
    DriftCausalityError,
    DriftKind,
    DriftLineageError,
    DriftMetric,
    DriftMonitor,
    DriftRecommendation,
    DriftState,
    DriftWindow,
)
from autosport.research_supervisor import (
    ResearchPhase,
    ResearchSupervisor,
    ResearchSupervisorError,
    ResearchTrigger,
)
from autosport.scientific_registry import (
    ConflictingScientificRecordError,
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
TRAIN_CUTOFF = "2026-01-01T00:00:00Z"
BASELINE_START = "2026-02-01T00:00:00Z"
BASELINE_END = "2026-02-02T00:00:00Z"
BASELINE_AS_OF = "2026-02-04T00:00:00Z"
CURRENT_START = "2026-02-09T00:00:00Z"
CURRENT_END = "2026-02-10T00:00:00Z"
CURRENT_AS_OF = "2026-02-11T00:00:00Z"
EVALUATED_AT = "2026-02-12T00:00:00Z"


def _registry(tmp_path, *, baseline_window=None, current_window=None):
    baseline_window = _baseline_window() if baseline_window is None else baseline_window
    current_window = _current_window() if current_window is None else current_window
    registry = ScientificRegistry.initialize_pristine(tmp_path / "scientific-registry.json")
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
            causal_cutoff=TRAIN_CUTOFF,
            available_at_utc="2026-01-01T12:00:00Z",
        )
    )
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id="dataset-baseline",
            manifest_sha256=baseline_window.evidence_sha256,
            source_identity="lawful:feed-a",
            license_identity="license:test",
            causal_cutoff=BASELINE_END,
            available_at_utc="2026-02-03T00:00:00Z",
        )
    )
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id="dataset-current",
            manifest_sha256=current_window.evidence_sha256,
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


def _baseline_window(**overrides):
    values = {
        "dataset_snapshot_id": "dataset-baseline",
        "source_identity": "lawful:feed-a",
        "window_start": BASELINE_START,
        "window_end": BASELINE_END,
        "as_of": BASELINE_AS_OF,
        "values": ("1", "2"),
        "value_observed_at": (
            "2026-02-01T12:00:00Z",
            "2026-02-02T00:00:00Z",
        ),
        "value_available_at": (
            "2026-02-03T00:00:00Z",
            "2026-02-03T00:00:00Z",
        ),
    }
    values.update(overrides)
    return DriftWindow.from_samples(**values)


def _current_window(**overrides):
    values = {
        "dataset_snapshot_id": "dataset-current",
        "source_identity": "lawful:feed-a",
        "window_start": CURRENT_START,
        "window_end": CURRENT_END,
        "as_of": CURRENT_AS_OF,
        "values": ("2", "3"),
        "value_observed_at": (
            "2026-02-09T12:00:00Z",
            "2026-02-10T00:00:00Z",
        ),
        "value_available_at": (
            "2026-02-10T12:00:00Z",
            "2026-02-10T12:00:00Z",
        ),
    }
    values.update(overrides)
    return DriftWindow.from_samples(**values)


def _reference(monitor, **overrides):
    values = {
        "drift_kind": DriftKind.FORECAST_PERFORMANCE,
        "metric": DriftMetric.MEAN_ABSOLUTE_SHIFT,
        "model_version_id": "model-1",
        "strategy_version_id": "strategy-1",
        "feature_set_id": "feature-1",
        "experiment_id": "experiment-baseline",
        "baseline": _baseline_window(),
        "min_samples": 2,
        "threshold": "0.5",
        "metric_definition_sha256": SHA_E,
    }
    values.update(overrides)
    return monitor.create_reference(**values)


def test_effective_sample_size_is_explicit_hash_bound_evidence(tmp_path):
    current = _current_window(effective_sample_size=1)
    registry = _registry(tmp_path, current_window=current)
    monitor = DriftMonitor(registry)
    reference = _reference(monitor)

    finding = monitor.evaluate(
        reference.reference_id,
        current,
        evaluated_at=EVALUATED_AT,
    )
    observation = registry.get("DriftObservation", finding.observation_id)
    assert observation is not None
    assert observation.payload["sample_count"] == 2
    assert observation.payload["effective_sample_size"] == 1

    with pytest.raises(ValueError, match="cannot exceed sample_count"):
        _current_window(effective_sample_size=3)

    with pytest.raises(ValueError, match="evidence_sha256"):
        replace(current, effective_sample_size=2)


def test_scoped_drift_evidence_is_hash_bound_and_scope_mismatch_fails_closed(tmp_path):
    baseline = _baseline_window(
        sport="table_tennis",
        league="league-a",
        regime="pre_match",
    )
    current = _current_window(
        sport="table_tennis",
        league="league-a",
        regime="pre_match",
    )
    registry = _registry(
        tmp_path,
        baseline_window=baseline,
        current_window=current,
    )
    monitor = DriftMonitor(registry)
    reference = _reference(monitor, baseline=baseline)

    finding = monitor.evaluate(
        reference.reference_id,
        current,
        evaluated_at=EVALUATED_AT,
    )
    observation = registry.get("DriftObservation", finding.observation_id)
    assert observation is not None
    assert observation.payload["sport"] == "table_tennis"
    assert observation.payload["league"] == "league-a"
    assert observation.payload["regime"] == "pre_match"

    mismatched = DriftWindow.from_samples(
        dataset_snapshot_id="dataset-other-scope",
        source_identity="lawful:feed-a",
        window_start="2026-02-11T00:00:00Z",
        window_end="2026-02-12T00:00:00Z",
        as_of="2026-02-13T00:00:00Z",
        values=("2", "3"),
        value_observed_at=(
            "2026-02-11T12:00:00Z",
            "2026-02-12T00:00:00Z",
        ),
        value_available_at=(
            "2026-02-13T00:00:00Z",
            "2026-02-13T00:00:00Z",
        ),
        sport="table_tennis",
        league="league-b",
        regime="pre_match",
    )
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id=mismatched.dataset_snapshot_id,
            manifest_sha256=mismatched.evidence_sha256,
            source_identity=mismatched.source_identity,
            license_identity="license:test",
            causal_cutoff=mismatched.window_end,
            available_at_utc=mismatched.as_of,
        )
    )
    mismatch_finding = monitor.evaluate(
        reference.reference_id,
        mismatched,
        evaluated_at=mismatched.as_of,
    )
    assert mismatch_finding.state is DriftState.INSUFFICIENT_EVIDENCE
    assert mismatch_finding.insufficiency_reason == "SCOPE_MISMATCH"


def test_detected_drift_is_durable_restart_safe_and_diagnostic_only(tmp_path):
    registry = _registry(tmp_path)
    monitor = DriftMonitor(registry)
    reference = _reference(monitor)

    finding = monitor.evaluate(
        reference.reference_id,
        _current_window(),
        evaluated_at=EVALUATED_AT,
    )

    assert finding.state is DriftState.DRIFT_DETECTED
    assert finding.recommendation is DriftRecommendation.RESEARCH_RETRAIN_CHALLENGER
    assert finding.absolute_delta_fraction == "1/1"

    reopened = ScientificRegistry(registry.path)
    stored = reopened.get("DriftFinding", finding.finding_id)
    assert stored is not None
    assert stored.payload["truth"] == "STATISTICAL_EVIDENCE_ONLY"
    assert stored.payload["automatic_promotion_authorized"] is False
    assert stored.payload["financial_authority_change_authorized"] is False
    assert stored.payload["real_money_execution_authorized"] is False
    assert len(DriftMonitor(reopened).list_findings(as_of=EVALUATED_AT)) == 1


def test_timezone_equivalent_reference_collapses_to_one_identity(tmp_path):
    registry = _registry(tmp_path)
    monitor = DriftMonitor(registry)
    first = _reference(monitor)
    second = _reference(
        monitor,
        baseline=_baseline_window(
            window_start="2026-02-01T02:00:00+02:00",
            window_end="2026-02-02T02:00:00+02:00",
            as_of="2026-02-04T02:00:00+02:00",
            value_available_at=(
                "2026-02-03T02:00:00+02:00",
                "2026-02-03T02:00:00+02:00",
            ),
        ),
    )

    assert second.reference_id == first.reference_id
    assert len(
        registry.causal_records("DriftReference", as_of=BASELINE_AS_OF)
    ) == 1


def test_equal_threshold_is_no_drift_not_a_noisy_false_positive(tmp_path):
    current = _current_window(values=("2", "2"))
    monitor = DriftMonitor(_registry(tmp_path, current_window=current))
    reference = _reference(monitor)

    finding = monitor.evaluate(
        reference.reference_id,
        current,
        evaluated_at=EVALUATED_AT,
    )

    assert finding.absolute_delta_fraction == "1/2"
    assert finding.state is DriftState.NO_DRIFT
    assert finding.recommendation is DriftRecommendation.NONE


def test_insufficient_window_is_first_class_durable_evidence(tmp_path):
    current = _current_window(
        values=("2",),
        value_observed_at=("2026-02-10T00:00:00Z",),
        value_available_at=("2026-02-10T12:00:00Z",),
    )
    registry = _registry(tmp_path, current_window=current)
    monitor = DriftMonitor(registry)
    reference = _reference(monitor, min_samples=3)

    finding = monitor.evaluate(
        reference.reference_id,
        current,
        evaluated_at=EVALUATED_AT,
    )

    assert finding.state is DriftState.INSUFFICIENT_EVIDENCE
    assert finding.insufficiency_reason == "REFERENCE_SAMPLE_COUNT"
    assert finding.absolute_delta_fraction is None
    stored = registry.get("DriftFinding", finding.finding_id)
    assert stored is not None
    assert stored.payload["state"] == "INSUFFICIENT_EVIDENCE"


def test_incomparable_source_is_fail_closed_as_insufficient_evidence(tmp_path):
    other = _current_window(
        dataset_snapshot_id="dataset-other-source",
        source_identity="lawful:feed-b",
    )
    registry = _registry(tmp_path)
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id="dataset-other-source",
            manifest_sha256=other.evidence_sha256,
            source_identity="lawful:feed-b",
            license_identity="license:test",
            causal_cutoff=CURRENT_END,
            available_at_utc="2026-02-10T12:00:00Z",
        )
    )
    monitor = DriftMonitor(registry)
    reference = _reference(monitor)

    finding = monitor.evaluate(
        reference.reference_id,
        other,
        evaluated_at=EVALUATED_AT,
    )

    assert finding.state is DriftState.INSUFFICIENT_EVIDENCE
    assert finding.insufficiency_reason == "SOURCE_IDENTITY_MISMATCH"


def test_future_dataset_and_future_value_are_rejected(tmp_path):
    future = _current_window(dataset_snapshot_id="dataset-future")
    registry = _registry(tmp_path)
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id="dataset-future",
            manifest_sha256=future.evidence_sha256,
            source_identity="lawful:feed-a",
            license_identity="license:test",
            causal_cutoff=CURRENT_END,
            available_at_utc="2026-03-01T00:00:00Z",
        )
    )
    monitor = DriftMonitor(registry)
    reference = _reference(monitor)

    with pytest.raises(DriftCausalityError, match="unavailable"):
        monitor.evaluate(
            reference.reference_id,
            future,
            evaluated_at=EVALUATED_AT,
        )

    with pytest.raises(DriftCausalityError, match="unavailable"):
        _current_window(
            value_available_at=(
                "2026-02-10T12:00:00Z",
                "2026-03-01T00:00:00Z",
            )
        )


def test_model_feature_lineage_mismatch_is_rejected(tmp_path):
    registry = _registry(tmp_path)
    registry.append(
        FeatureSet(
            feature_set_id="feature-2",
            version="v2",
            definition_sha256=SHA_B,
            source_sha256=SHA_C,
            available_at_utc="2026-01-04T00:00:00Z",
        )
    )
    monitor = DriftMonitor(registry)

    with pytest.raises(DriftLineageError, match="model/feature"):
        _reference(monitor, feature_set_id="feature-2")


def test_baseline_before_training_cutoff_is_rejected(tmp_path):
    baseline = _baseline_window(window_start="2025-12-31T00:00:00Z")
    monitor = DriftMonitor(_registry(tmp_path, baseline_window=baseline))

    with pytest.raises(DriftCausalityError, match="training cutoff"):
        _reference(
            monitor,
            baseline=baseline,
        )


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_values_are_rejected(bad):
    with pytest.raises(ValueError, match="finite"):
        _current_window(values=(bad,), value_available_at=("2026-02-10T12:00:00Z",))


def test_duplicate_evaluation_is_idempotent_and_conflicting_finding_fails(tmp_path):
    registry = _registry(tmp_path)
    monitor = DriftMonitor(registry)
    reference = _reference(monitor)
    current = _current_window()

    first = monitor.evaluate(reference.reference_id, current, evaluated_at=EVALUATED_AT)
    reopened = ScientificRegistry(registry.path)
    second = DriftMonitor(reopened).evaluate(
        reference.reference_id,
        current,
        evaluated_at="2026-02-13T00:00:00Z",
    )

    assert second == first
    assert first.evaluated_at == CURRENT_AS_OF
    assert len(reopened.causal_records("DriftObservation", as_of="2026-02-13T00:00:00Z")) == 1
    assert len(reopened.causal_records("DriftFinding", as_of="2026-02-13T00:00:00Z")) == 1

    conflicting = replace(first, evidence_sha256=SHA_A)
    with pytest.raises(ConflictingScientificRecordError, match="conflicting immutable"):
        registry.append(conflicting)


def test_registry_detects_drift_finding_tamper_after_restart(tmp_path):
    registry = _registry(tmp_path)
    monitor = DriftMonitor(registry)
    reference = _reference(monitor)
    finding = monitor.evaluate(reference.reference_id, _current_window(), evaluated_at=EVALUATED_AT)

    raw = json.loads(registry.path.read_text(encoding="utf-8"))
    target = next(
        item
        for item in raw["records"]
        if item["record_type"] == "DriftFinding" and item["record_id"] == finding.finding_id
    )
    target["payload"]["state"] = "NO_DRIFT"
    registry.path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(ValueError, match="record digest mismatch"):
        ScientificRegistry(registry.path)


def test_later_data_revision_creates_new_evidence_without_rewriting_history(tmp_path):
    registry = _registry(tmp_path)
    monitor = DriftMonitor(registry)
    reference = _reference(monitor)

    first_window = _current_window()
    second_window = _current_window(
        dataset_snapshot_id="dataset-current-r2",
        values=("2", "2.5"),
    )
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id=second_window.dataset_snapshot_id,
            manifest_sha256=second_window.evidence_sha256,
            source_identity=second_window.source_identity,
            license_identity="license:test",
            causal_cutoff=CURRENT_END,
            available_at_utc="2026-02-10T12:00:00Z",
        )
    )

    first = monitor.evaluate(
        reference.reference_id,
        first_window,
        evaluated_at=EVALUATED_AT,
    )
    second = monitor.evaluate(
        reference.reference_id,
        second_window,
        evaluated_at="2026-02-13T00:00:00Z",
    )

    assert second.finding_id != first.finding_id
    findings = registry.causal_records(
        "DriftFinding", as_of="2026-02-13T00:00:00Z"
    )
    assert {item.record_id for item in findings} == {
        first.finding_id,
        second.finding_id,
    }


def test_drift_finding_can_only_enter_supervisor_as_bounded_evidence_binding(tmp_path):
    registry = _registry(tmp_path)
    monitor = DriftMonitor(registry)
    reference = _reference(monitor)
    finding = monitor.evaluate(
        reference.reference_id,
        _current_window(),
        evaluated_at=EVALUATED_AT,
    )

    supervisor = ResearchSupervisor.initialize_pristine(
        tmp_path / "research-supervisor.json",
        registry,
    )
    started = supervisor.accept_trigger(
        ResearchTrigger(
            trigger_id="trigger-from-drift",
            question_id="question-drift",
            requested_at="2026-02-12T00:01:00Z",
            budget_units=8,
        )
    )
    advanced = supervisor.advance(
        started.run_id,
        expected_phase=ResearchPhase.QUESTION,
        at="2026-02-12T00:02:00Z",
        bindings=monitor.finding_binding(finding),
    )

    assert advanced.bindings == (("drift_finding_id", finding.finding_id),)
    assert registry.causal_records(
        "PromotionDecision", as_of="2026-02-12T00:02:00Z"
    ) == ()


def test_snapshot_must_cover_entire_drift_window(tmp_path):
    stale = _current_window(dataset_snapshot_id="dataset-stale")
    registry = _registry(tmp_path)
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id=stale.dataset_snapshot_id,
            manifest_sha256=stale.evidence_sha256,
            source_identity=stale.source_identity,
            license_identity="license:test",
            causal_cutoff="2026-02-09T18:00:00Z",
            available_at_utc="2026-02-10T12:00:00Z",
        )
    )
    monitor = DriftMonitor(registry)
    reference = _reference(monitor)

    with pytest.raises(DriftCausalityError, match="cover drift window_end"):
        monitor.evaluate(reference.reference_id, stale, evaluated_at=EVALUATED_AT)


def test_exact_samples_are_bound_to_registered_dataset_manifest(tmp_path):
    registry = _registry(tmp_path)
    monitor = DriftMonitor(registry)
    reference = _reference(monitor)
    altered = _current_window(values=("2", "4"))

    with pytest.raises(DriftLineageError, match="DatasetSnapshot manifest"):
        monitor.evaluate(reference.reference_id, altered, evaluated_at=EVALUATED_AT)


def test_sample_observation_must_belong_to_declared_window():
    with pytest.raises(DriftCausalityError, match="outside the declared window"):
        _current_window(
            value_observed_at=(
                "2026-02-08T23:59:59Z",
                "2026-02-10T00:00:00Z",
            )
        )


def test_revision_identity_cannot_be_relabelled_without_new_evidence():
    current = _current_window()
    with pytest.raises(ValueError, match="revision_id"):
        replace(current, revision_id=SHA_D)


def test_decimal_canonicalization_is_independent_of_ambient_context():
    value = "1.234567890123456789"
    with localcontext() as context:
        context.prec = 6
        low_precision = _current_window(values=(value, "2"))
    with localcontext() as context:
        context.prec = 50
        high_precision = _current_window(values=(value, "2"))

    assert low_precision.evidence_sha256 == high_precision.evidence_sha256
    assert low_precision.mean_fraction == high_precision.mean_fraction


def test_supervisor_rejects_drift_finding_from_incompatible_context(tmp_path):
    registry = _registry(tmp_path)
    monitor = DriftMonitor(registry)
    reference = _reference(monitor)
    finding = monitor.evaluate(
        reference.reference_id,
        _current_window(),
        evaluated_at=EVALUATED_AT,
    )
    registry.append(
        ModelVersion(
            model_version_id="model-2",
            model_family="other-context",
            artifact_sha256=SHA_B,
            source_sha256=SHA_C,
            environment_sha256=SHA_D,
            dataset_snapshot_id="dataset-train",
            feature_set_id="feature-1",
            research_protocol_id="protocol-1",
            seed=9,
            config_sha256=SHA_E,
            created_at="2026-01-04T00:00:00Z",
        )
    )
    supervisor = ResearchSupervisor.initialize_pristine(
        tmp_path / "research-supervisor.json",
        registry,
    )
    started = supervisor.accept_trigger(
        ResearchTrigger(
            trigger_id="trigger-context-mismatch",
            question_id="question-drift",
            requested_at="2026-02-12T00:01:00Z",
            budget_units=8,
        )
    )
    contextual = supervisor.advance(
        started.run_id,
        expected_phase=ResearchPhase.QUESTION,
        at="2026-02-12T00:02:00Z",
        bindings=(("model_version_id", "model-2"),),
    )

    with pytest.raises(ResearchSupervisorError, match="drift finding context mismatch"):
        supervisor.advance(
            contextual.run_id,
            expected_phase=ResearchPhase.HYPOTHESIS,
            at="2026-02-12T00:03:00Z",
            bindings=monitor.finding_binding(finding),
        )

    unchanged = supervisor.status(contextual.run_id)
    assert unchanged.phase is ResearchPhase.HYPOTHESIS
    assert unchanged.bindings == (("model_version_id", "model-2"),)

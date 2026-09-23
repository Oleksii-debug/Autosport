from dataclasses import replace

import pytest

from autosport.drift_control import (
    DriftKind,
    DriftMetric,
    DriftMonitor,
    DriftState,
    DriftWindow,
)
from autosport.incident_drift_projection import (
    DriftIncidentProjectionError,
    project_canonical_drift_model_risk,
    validate_canonical_drift_model_risk,
)
from autosport.incident_risk_register import (
    RegisterEntryKind,
    RiskEvidenceState,
    RiskSeverity,
    RiskStatus,
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
TRAIN_CUTOFF = "2026-01-01T00:00:00Z"
BASELINE_START = "2026-02-01T00:00:00Z"
BASELINE_END = "2026-02-02T00:00:00Z"
BASELINE_AS_OF = "2026-02-04T00:00:00Z"
CURRENT_START = "2026-02-09T00:00:00Z"
CURRENT_END = "2026-02-10T00:00:00Z"
CURRENT_AS_OF = "2026-02-11T00:00:00Z"
EVALUATED_AT = "2026-02-12T00:00:00Z"


def baseline_window(**overrides):
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


def current_window(**overrides):
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


def registry(tmp_path, *, current=None):
    current = current_window() if current is None else current
    baseline = baseline_window()
    value = ScientificRegistry.initialize_pristine(
        tmp_path / "scientific-registry.json"
    )
    value.append(
        ResearchQuestion(
            question_id="question-drift",
            statement="Has the frozen strategy context materially drifted?",
            source_sha256=SHA_A,
            created_at="2026-01-01T00:00:00Z",
        )
    )
    value.append(
        DatasetSnapshot(
            dataset_snapshot_id="dataset-train",
            manifest_sha256=SHA_A,
            source_identity="lawful:feed-a",
            license_identity="license:test",
            causal_cutoff=TRAIN_CUTOFF,
            available_at_utc="2026-01-01T12:00:00Z",
        )
    )
    value.append(
        DatasetSnapshot(
            dataset_snapshot_id="dataset-baseline",
            manifest_sha256=baseline.evidence_sha256,
            source_identity="lawful:feed-a",
            license_identity="license:test",
            causal_cutoff=BASELINE_END,
            available_at_utc="2026-02-03T00:00:00Z",
        )
    )
    value.append(
        DatasetSnapshot(
            dataset_snapshot_id="dataset-current",
            manifest_sha256=current.evidence_sha256,
            source_identity="lawful:feed-a",
            license_identity="license:test",
            causal_cutoff=CURRENT_END,
            available_at_utc="2026-02-10T12:00:00Z",
        )
    )
    value.append(
        FeatureSet(
            feature_set_id="feature-1",
            version="v1",
            definition_sha256=SHA_A,
            source_sha256=SHA_B,
            available_at_utc="2026-01-01T12:00:00Z",
        )
    )
    value.append(
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
    value.append(
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
    value.append(
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
    return value


def reference(
    monitor,
    *,
    threshold="0.5",
    drift_kind=DriftKind.FORECAST_PERFORMANCE,
):
    return monitor.create_reference(
        drift_kind=drift_kind,
        metric=DriftMetric.MEAN_ABSOLUTE_SHIFT,
        model_version_id="model-1",
        strategy_version_id="strategy-1",
        feature_set_id="feature-1",
        experiment_id="experiment-baseline",
        baseline=baseline_window(),
        min_samples=2,
        threshold=threshold,
        metric_definition_sha256=SHA_E,
    )


def canonical_finding(
    tmp_path,
    *,
    threshold="0.5",
    drift_kind=DriftKind.FORECAST_PERFORMANCE,
    current=None,
):
    current = current_window() if current is None else current
    scientific = registry(tmp_path, current=current)
    monitor = DriftMonitor(scientific)
    drift_reference = reference(
        monitor,
        threshold=threshold,
        drift_kind=drift_kind,
    )
    finding = monitor.evaluate(
        drift_reference.reference_id,
        current,
        evaluated_at=EVALUATED_AT,
    )
    return scientific, monitor, finding


def test_detected_drift_projects_verified_open_model_risk(tmp_path):
    _scientific, monitor, finding = canonical_finding(tmp_path)

    assert finding.state is DriftState.DRIFT_DETECTED
    entry = project_canonical_drift_model_risk(
        monitor,
        finding_id=finding.finding_id,
        as_of=EVALUATED_AT,
    )

    assert entry is not None
    assert entry.kind is RegisterEntryKind.MODEL_RISK
    assert entry.status is RiskStatus.OPEN
    assert entry.evidence_state is RiskEvidenceState.VERIFIED
    assert entry.severity is RiskSeverity.HIGH
    assert entry.model_version_ids == ("model-1",)
    assert entry.requires_operator_action is True
    assert entry.opened_at == "2026-02-11T00:00:00+00:00"
    assert entry.updated_at == entry.opened_at
    assert len(entry.occurrence_evidence_refs) == 1
    assert entry.occurrence_evidence_refs == entry.evidence_refs
    assert "automatic" in entry.summary
    validate_canonical_drift_model_risk(
        monitor,
        entry=entry,
        as_of=EVALUATED_AT,
    )


def test_covariate_drift_uses_bounded_medium_policy(tmp_path):
    _scientific, monitor, finding = canonical_finding(
        tmp_path,
        drift_kind=DriftKind.COVARIATE,
    )
    entry = project_canonical_drift_model_risk(
        monitor,
        finding_id=finding.finding_id,
        as_of=EVALUATED_AT,
    )
    assert entry is not None
    assert entry.severity is RiskSeverity.MEDIUM
    validate_canonical_drift_model_risk(
        monitor,
        entry=entry,
        as_of=EVALUATED_AT,
    )


def test_no_drift_does_not_mint_or_close_model_risk(tmp_path):
    _scientific, monitor, finding = canonical_finding(
        tmp_path,
        threshold="2",
    )
    assert finding.state is DriftState.NO_DRIFT

    assert (
        project_canonical_drift_model_risk(
            monitor,
            finding_id=finding.finding_id,
            as_of=EVALUATED_AT,
        )
        is None
    )


def test_insufficient_evidence_does_not_mint_drift_risk(tmp_path):
    current = current_window(values=("2",), value_observed_at=("2026-02-10T00:00:00Z",), value_available_at=("2026-02-10T12:00:00Z",))
    scientific = registry(tmp_path, current=current)
    monitor = DriftMonitor(scientific)
    drift_reference = reference(monitor)
    finding = monitor.evaluate(
        drift_reference.reference_id,
        current,
        evaluated_at=EVALUATED_AT,
    )

    assert finding.state is DriftState.INSUFFICIENT_EVIDENCE
    assert (
        project_canonical_drift_model_risk(
            monitor,
            finding_id=finding.finding_id,
            as_of=EVALUATED_AT,
        )
        is None
    )


def test_restart_reprojects_identical_model_risk_identity(tmp_path):
    scientific, monitor, finding = canonical_finding(tmp_path)
    before = project_canonical_drift_model_risk(
        monitor,
        finding_id=finding.finding_id,
        as_of=EVALUATED_AT,
    )
    reopened = DriftMonitor(ScientificRegistry(scientific.path))
    after = project_canonical_drift_model_risk(
        reopened,
        finding_id=finding.finding_id,
        as_of=EVALUATED_AT,
    )

    assert before is not None
    assert after == before
    assert after.fingerprint_sha256 == before.fingerprint_sha256
    validate_canonical_drift_model_risk(
        reopened,
        entry=after,
        as_of=EVALUATED_AT,
    )


def test_use_time_validation_rejects_severity_spoof(tmp_path):
    _scientific, monitor, finding = canonical_finding(
        tmp_path,
        drift_kind=DriftKind.COVARIATE,
    )
    entry = project_canonical_drift_model_risk(
        monitor,
        finding_id=finding.finding_id,
        as_of=EVALUATED_AT,
    )
    assert entry is not None
    spoof = replace(entry, severity=RiskSeverity.HIGH)

    with pytest.raises(
        DriftIncidentProjectionError,
        match="severity does not match",
    ):
        validate_canonical_drift_model_risk(
            monitor,
            entry=spoof,
            as_of=EVALUATED_AT,
        )


def test_use_time_validation_rejects_false_terminal_status(tmp_path):
    _scientific, monitor, finding = canonical_finding(tmp_path)
    entry = project_canonical_drift_model_risk(
        monitor,
        finding_id=finding.finding_id,
        as_of=EVALUATED_AT,
    )
    assert entry is not None
    spoof = replace(
        entry,
        status=RiskStatus.RESOLVED,
        mitigation="Caller claims drift recovered.",
        requires_operator_action=False,
    )

    with pytest.raises(
        DriftIncidentProjectionError,
        match="cannot authorize terminal",
    ):
        validate_canonical_drift_model_risk(
            monitor,
            entry=spoof,
            as_of=EVALUATED_AT,
        )


def test_caller_minted_drift_evidence_reference_fails_closed(tmp_path):
    _scientific, monitor, finding = canonical_finding(tmp_path)
    entry = project_canonical_drift_model_risk(
        monitor,
        finding_id=finding.finding_id,
        as_of=EVALUATED_AT,
    )
    assert entry is not None
    fake_ref = "drift-finding-evidence:" + "0" * 64 + ":" + "1" * 64
    spoof = replace(
        entry,
        occurrence_evidence_refs=(fake_ref,),
        evidence_refs=(fake_ref,),
        entry_id=entry.entry_id,
    )

    # The generic IncidentRiskEntry constructor itself must reject the stale
    # caller-provided entry_id before the product validator can trust it.
    with pytest.raises(ValueError, match="product-derived occurrence identity"):
        replace(
            entry,
            occurrence_evidence_refs=(fake_ref,),
            evidence_refs=(fake_ref,),
        )


def test_validation_before_finding_availability_fails_closed(tmp_path):
    _scientific, monitor, finding = canonical_finding(tmp_path)
    entry = project_canonical_drift_model_risk(
        monitor,
        finding_id=finding.finding_id,
        as_of=EVALUATED_AT,
    )
    assert entry is not None

    with pytest.raises(DriftIncidentProjectionError, match="re-resolution failed"):
        validate_canonical_drift_model_risk(
            monitor,
            entry=entry,
            as_of="2026-02-10T00:00:00Z",
        )

from dataclasses import replace
import pytest

from autosport.champion_eligibility import (
    ChampionEligibilityDecision,
    ChampionEligibilityError,
    ChampionEligibilityStatus,
    bind_research_trigger,
    persist_eligibility_decision,
    validate_activation_eligibility,
)
from autosport.drift_control import (
    DriftKind,
    DriftMetric,
    DriftMonitor,
    DriftState,
    DriftWindow,
)
from autosport.research_supervisor import ResearchSupervisor
from autosport.scientific_registry import DatasetSnapshot, ScientificRegistry


ENV = "e" * 64
CONFIG = "f" * 64


def _decision_registry(tmp_path, *, threshold="0.5"):
    # Reuse the canonical drift fixtures so the decision layer is tested against
    # real ScientificRegistry/DriftFinding records rather than a second fake store.
    from tests.test_drift_control import _current_window, _reference, _registry

    registry = _registry(tmp_path)
    monitor = DriftMonitor(registry)
    reference = _reference(monitor, threshold=threshold)
    finding = monitor.evaluate(
        reference.reference_id,
        _current_window(),
        evaluated_at="2026-02-12T00:00:00Z",
    )
    return registry, finding


def _scoped_window(
    *,
    dataset_snapshot_id,
    window_start,
    window_end,
    as_of,
    values,
    sport="table_tennis",
    league="league-a",
    regime="pre_match",
):
    return DriftWindow.from_samples(
        dataset_snapshot_id=dataset_snapshot_id,
        source_identity="lawful:feed-a",
        window_start=window_start,
        window_end=window_end,
        as_of=as_of,
        values=values,
        value_observed_at=(window_start, window_end),
        value_available_at=(as_of, as_of),
        sport=sport,
        league=league,
        regime=regime,
    )


def _scoped_history(
    tmp_path,
    *,
    league="league-a",
    values_sequence=(("3", "4"), ("3", "4")),
):
    from tests.test_drift_control import _reference, _registry

    baseline = _scoped_window(
        dataset_snapshot_id="dataset-baseline",
        window_start="2026-02-01T00:00:00Z",
        window_end="2026-02-02T00:00:00Z",
        as_of="2026-02-04T00:00:00Z",
        values=("1", "2"),
        league=league,
    )
    specs = (
        ("dataset-current", "2026-02-09T00:00:00Z", "2026-02-10T00:00:00Z", "2026-02-11T00:00:00Z"),
        ("dataset-current-2", "2026-02-11T00:00:00Z", "2026-02-12T00:00:00Z", "2026-02-13T00:00:00Z"),
        ("dataset-current-3", "2026-02-13T00:00:00Z", "2026-02-14T00:00:00Z", "2026-02-15T00:00:00Z"),
        ("dataset-current-4", "2026-02-15T00:00:00Z", "2026-02-16T00:00:00Z", "2026-02-17T00:00:00Z"),
    )
    if not 1 <= len(values_sequence) <= len(specs):
        raise ValueError("values_sequence must contain one to four windows")
    windows = tuple(
        _scoped_window(
            dataset_snapshot_id=specs[index][0],
            window_start=specs[index][1],
            window_end=specs[index][2],
            as_of=specs[index][3],
            values=values,
            league=league,
        )
        for index, values in enumerate(values_sequence)
    )
    registry = _registry(
        tmp_path,
        baseline_window=baseline,
        current_window=windows[0],
    )
    monitor = DriftMonitor(registry)
    reference = _reference(monitor, baseline=baseline)

    findings = []
    for index, window in enumerate(windows):
        if index:
            registry.append(
                DatasetSnapshot(
                    dataset_snapshot_id=window.dataset_snapshot_id,
                    manifest_sha256=window.evidence_sha256,
                    source_identity=window.source_identity,
                    license_identity="license:test",
                    causal_cutoff=window.window_end,
                    available_at_utc=window.as_of,
                )
            )
        findings.append(
            monitor.evaluate(
                reference.reference_id,
                window,
                evaluated_at=window.as_of,
            )
        )
    return registry, tuple(findings), windows


def _scoped_decision(
    registry,
    findings,
    windows,
    *,
    league="league-a",
    research_trigger_id=None,
):
    return ChampionEligibilityDecision.from_findings(
        registry,
        canonical_strategy_id="strategy-context",
        strategy_version_id="strategy-1",
        model_version_id="model-1",
        environment_sha256=ENV,
        protocol_id="protocol-1",
        config_sha256=CONFIG,
        sport="table_tennis",
        league=league,
        regime="pre_match",
        finding_ids=tuple(item.finding_id for item in findings),
        window_start=windows[0].window_start,
        window_end=windows[-1].window_end,
        evaluated_at=windows[-1].as_of,
        valid_until="2026-02-28T00:00:00Z",
        minimum_samples=2,
        minimum_effective_sample_size=2,
        admissible_actions=("BET", "WAIT"),
        research_trigger_id=research_trigger_id,
    )


def _eligible_scoped_decision(tmp_path):
    registry, findings, windows = _scoped_history(
        tmp_path,
        values_sequence=(("1", "2"), ("1", "2")),
    )
    decision = _scoped_decision(registry, findings, windows)
    assert decision.status is ChampionEligibilityStatus.ELIGIBLE
    assert decision.recovery_streak == 2
    return registry, decision


def _make_decision(registry, finding, **overrides):
    values = {
        "canonical_strategy_id": "strategy-context",
        "strategy_version_id": "strategy-1",
        "model_version_id": "model-1",
        "environment_sha256": ENV,
        "protocol_id": "protocol-1",
        "config_sha256": CONFIG,
        "sport": "table_tennis",
        "league": "league-a",
        "regime": "pre_match",
        "finding_ids": (finding.finding_id,),
        "window_start": "2026-02-09T00:00:00Z",
        "window_end": "2026-02-10T00:00:00Z",
        "evaluated_at": "2026-02-12T00:00:00Z",
        "valid_until": "2026-02-13T00:00:00Z",
        "minimum_samples": 2,
        "minimum_effective_sample_size": 2,
        "degraded_streak": None,
        "recovery_streak": None,
        "admissible_actions": ("BET", "WAIT"),
    }
    values.update(overrides)
    return ChampionEligibilityDecision.from_findings(registry, **values)


def test_insufficient_evidence_is_wait_not_drift_claim(tmp_path):
    registry, finding = _decision_registry(tmp_path)
    decision = _make_decision(
        registry,
        finding,
        minimum_samples=3,
        minimum_effective_sample_size=3,
    )
    assert decision.status is ChampionEligibilityStatus.WAIT_MORE_EVIDENCE


def test_single_degradation_window_cannot_deactivate(tmp_path):
    registry, finding = _decision_registry(tmp_path)
    decision = _make_decision(registry, finding, degraded_streak=1)
    assert finding.state is DriftState.DRIFT_DETECTED
    assert decision.status is ChampionEligibilityStatus.WAIT_MORE_EVIDENCE


def test_single_finding_cannot_assert_sustained_degradation(tmp_path):
    registry, finding = _decision_registry(tmp_path)
    with pytest.raises(ChampionEligibilityError, match="degraded_streak"):
        _make_decision(registry, finding, degraded_streak=2)


def test_single_no_drift_window_waits_for_authoritative_scope_and_repetition(tmp_path):
    registry, finding = _decision_registry(tmp_path, threshold="10")
    decision = _make_decision(registry, finding)
    assert decision.status is ChampionEligibilityStatus.WAIT_MORE_EVIDENCE
    persist_eligibility_decision(registry, decision)
    with pytest.raises(ChampionEligibilityError, match="WAIT_MORE_EVIDENCE"):
        validate_activation_eligibility(
            registry,
            decision,
            as_of="2026-02-12T12:00:00Z",
            canonical_strategy_id="strategy-context",
            expected_strategy_version_id="strategy-1",
            expected_model_version_id="model-1",
            expected_environment_sha256=ENV,
            expected_protocol_id="protocol-1",
            expected_config_sha256=CONFIG,
            admissible_actions=frozenset({"WAIT"}),
        )


def test_real_scoped_drift_deactivates_only_affected_scope_and_recovers(tmp_path):
    registry, findings, windows = _scoped_history(
        tmp_path,
        values_sequence=(("3", "4"), ("3", "4"), ("1", "2"), ("1", "2")),
    )

    degraded = _scoped_decision(registry, findings[:2], windows[:2])
    assert degraded.status is ChampionEligibilityStatus.SHADOW_DEACTIVATED
    assert degraded.degraded_streak == 2
    assert degraded.recovery_streak == 0

    with pytest.raises(ChampionEligibilityError, match="scope does not match"):
        _scoped_decision(
            registry,
            findings[:2],
            windows[:2],
            league="league-b",
        )

    recovered = _scoped_decision(registry, findings, windows)
    assert recovered.status is ChampionEligibilityStatus.ELIGIBLE
    assert recovered.degraded_streak == 0
    assert recovered.recovery_streak == 2

    other_path = tmp_path / "league-b"
    other_path.mkdir()
    other_registry, other_findings, other_windows = _scoped_history(
        other_path,
        league="league-b",
        values_sequence=(("1", "2"), ("1", "2")),
    )
    unaffected = _scoped_decision(
        other_registry,
        other_findings,
        other_windows,
        league="league-b",
    )
    assert unaffected.status is ChampionEligibilityStatus.ELIGIBLE


def test_activation_rejects_expired_or_tampered_or_wider_evidence(tmp_path):
    registry, decision = _eligible_scoped_decision(tmp_path)
    persist_eligibility_decision(registry, decision)
    validate_activation_eligibility(
        registry,
        decision,
        as_of="2026-02-14T12:00:00Z",
        canonical_strategy_id="strategy-context",
        expected_strategy_version_id="strategy-1",
        expected_model_version_id="model-1",
        expected_environment_sha256=ENV,
        expected_protocol_id="protocol-1",
        expected_config_sha256=CONFIG,
        admissible_actions=frozenset({"WAIT"}),
    )
    with pytest.raises(ChampionEligibilityError, match="expired"):
        validate_activation_eligibility(
            registry,
            decision,
            as_of="2026-03-01T00:00:00Z",
            canonical_strategy_id="strategy-context",
            expected_strategy_version_id="strategy-1",
            expected_model_version_id="model-1",
            expected_environment_sha256=ENV,
            expected_protocol_id="protocol-1",
            expected_config_sha256=CONFIG,
            admissible_actions=frozenset({"WAIT"}),
        )
    with pytest.raises(ChampionEligibilityError, match="may only be narrowed"):
        validate_activation_eligibility(
            registry,
            decision,
            as_of="2026-02-14T12:00:00Z",
            canonical_strategy_id="strategy-context",
            expected_strategy_version_id="strategy-1",
            expected_model_version_id="model-1",
            expected_environment_sha256=ENV,
            expected_protocol_id="protocol-1",
            expected_config_sha256=CONFIG,
            admissible_actions=frozenset({"BET", "WAIT", "HEDGE"}),
        )


def test_research_trigger_binding_is_idempotent(tmp_path):
    registry, findings, windows = _scoped_history(tmp_path)
    decision = _scoped_decision(
        registry,
        findings,
        windows,
        research_trigger_id="champion-drift:scoped-degradation",
    )
    assert decision.status is ChampionEligibilityStatus.RESEARCH_REQUIRED
    assert decision.degraded_streak == 2
    supervisor = ResearchSupervisor.initialize_pristine(
        tmp_path / "research-supervisor.json",
        registry,
    )
    first = bind_research_trigger(
        supervisor,
        decision,
        question_id="question-drift",
        requested_at="2026-02-12T12:00:00Z",
        budget_units=8,
    )
    second = bind_research_trigger(
        supervisor,
        decision,
        question_id="question-drift",
        requested_at="2026-02-12T12:00:00Z",
        budget_units=8,
    )
    assert first == second


def test_activation_rejects_wrong_exact_lineage(tmp_path):
    registry, decision = _eligible_scoped_decision(tmp_path)
    persist_eligibility_decision(registry, decision)
    with pytest.raises(ChampionEligibilityError, match="model identity mismatch"):
        validate_activation_eligibility(
            registry,
            decision,
            as_of="2026-02-14T12:00:00Z",
            canonical_strategy_id="strategy-context",
            expected_strategy_version_id="strategy-1",
            expected_model_version_id="other-model",
            expected_environment_sha256=ENV,
            expected_protocol_id="protocol-1",
            expected_config_sha256=CONFIG,
            admissible_actions=frozenset({"WAIT"}),
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))

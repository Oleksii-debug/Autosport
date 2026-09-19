import pytest

from autosport.champion_eligibility import (
    ChampionEligibilityDecision,
    ChampionEligibilityError,
    ChampionEligibilityStatus,
    bind_research_trigger,
    persist_eligibility_decision,
    validate_activation_eligibility,
)
from autosport.drift_control import DriftKind, DriftMetric, DriftMonitor, DriftState
from autosport.research_supervisor import ResearchSupervisor
from autosport.scientific_registry import ScientificRegistry


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
        "degraded_streak": 0,
        "recovery_streak": 0,
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


def test_sustained_degradation_only_deactivates_scoped_champion(tmp_path):
    registry, finding = _decision_registry(tmp_path)
    decision = _make_decision(registry, finding, degraded_streak=2)
    assert decision.status is ChampionEligibilityStatus.SHADOW_DEACTIVATED
    assert (decision.sport, decision.league, decision.regime) == (
        "table_tennis",
        "league-a",
        "pre_match",
    )


def test_no_drift_is_activation_eligible_and_runtime_can_only_narrow_actions(tmp_path):
    registry, finding = _decision_registry(tmp_path, threshold="10")
    decision = _make_decision(registry, finding)
    assert decision.status is ChampionEligibilityStatus.ELIGIBLE
    persist_eligibility_decision(registry, decision)
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


def test_activation_rejects_expired_or_tampered_or_wider_evidence(tmp_path):
    registry, finding = _decision_registry(tmp_path, threshold="10")
    decision = _make_decision(registry, finding)
    persist_eligibility_decision(registry, decision)
    with pytest.raises(ChampionEligibilityError, match="expired"):
        validate_activation_eligibility(
            registry,
            decision,
            as_of="2026-02-14T00:00:00Z",
            canonical_strategy_id="strategy-1",
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
            as_of="2026-02-12T12:00:00Z",
            canonical_strategy_id="strategy-1",
            expected_strategy_version_id="strategy-1",
            expected_model_version_id="model-1",
            expected_environment_sha256=ENV,
            expected_protocol_id="protocol-1",
            expected_config_sha256=CONFIG,
            admissible_actions=frozenset({"BET", "WAIT", "HEDGE"}),
        )


def test_research_trigger_binding_is_idempotent_for_sustained_degradation(tmp_path):
    registry, finding = _decision_registry(tmp_path)
    decision = _make_decision(
        registry,
        finding,
        degraded_streak=2,
        research_trigger_id="reserved-by-caller",
    )
    assert decision.status is ChampionEligibilityStatus.RESEARCH_REQUIRED
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
    registry, finding = _decision_registry(tmp_path, threshold="10")
    decision = _make_decision(registry, finding)
    persist_eligibility_decision(registry, decision)
    with pytest.raises(ChampionEligibilityError, match="model identity mismatch"):
        validate_activation_eligibility(
            registry,
            decision,
            as_of="2026-02-12T12:00:00Z",
            canonical_strategy_id="strategy-1",
            expected_strategy_version_id="strategy-1",
            expected_model_version_id="other-model",
            expected_environment_sha256=ENV,
            expected_protocol_id="protocol-1",
            expected_config_sha256=CONFIG,
            admissible_actions=frozenset({"WAIT"}),
        )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))

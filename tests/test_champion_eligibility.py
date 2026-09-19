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


def test_activation_rejects_expired_or_tampered_or_wider_evidence(tmp_path):
    registry, finding = _decision_registry(tmp_path, threshold="10")
    decision = replace(
        _make_decision(registry, finding),
        status=ChampionEligibilityStatus.ELIGIBLE,
        reason="test-qualified",
    )
    persist_eligibility_decision(registry, decision)
    with pytest.raises(ChampionEligibilityError, match="expired"):
        validate_activation_eligibility(
            registry,
            decision,
            as_of="2026-02-14T00:00:00Z",
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
            as_of="2026-02-12T12:00:00Z",
            canonical_strategy_id="strategy-context",
            expected_strategy_version_id="strategy-1",
            expected_model_version_id="model-1",
            expected_environment_sha256=ENV,
            expected_protocol_id="protocol-1",
            expected_config_sha256=CONFIG,
            admissible_actions=frozenset({"BET", "WAIT", "HEDGE"}),
        )


def test_research_trigger_binding_is_idempotent(tmp_path):
    registry, finding = _decision_registry(tmp_path)
    decision = _make_decision(registry, finding)
    decision = ChampionEligibilityDecision(
        status=ChampionEligibilityStatus.RESEARCH_REQUIRED,
        canonical_strategy_id=decision.canonical_strategy_id,
        strategy_version_id=decision.strategy_version_id,
        model_version_id=decision.model_version_id,
        environment_sha256=decision.environment_sha256,
        protocol_id=decision.protocol_id,
        config_sha256=decision.config_sha256,
        sport=decision.sport,
        league=decision.league,
        regime=decision.regime,
        finding_ids=decision.finding_ids,
        finding_record_sha256s=decision.finding_record_sha256s,
        window_start=decision.window_start,
        window_end=decision.window_end,
        evaluated_at=decision.evaluated_at,
        valid_until=decision.valid_until,
        minimum_samples=decision.minimum_samples,
        minimum_effective_sample_size=decision.minimum_effective_sample_size,
        effective_sample_size=decision.effective_sample_size,
        degraded_streak=decision.degraded_streak,
        recovery_streak=decision.recovery_streak,
        admissible_actions=decision.admissible_actions,
        research_trigger_id="champion-drift:manual-test",
        reason="test",
    )
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
    decision = replace(
        _make_decision(registry, finding),
        status=ChampionEligibilityStatus.ELIGIBLE,
        reason="test-qualified",
    )
    persist_eligibility_decision(registry, decision)
    with pytest.raises(ChampionEligibilityError, match="model identity mismatch"):
        validate_activation_eligibility(
            registry,
            decision,
            as_of="2026-02-12T12:00:00Z",
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

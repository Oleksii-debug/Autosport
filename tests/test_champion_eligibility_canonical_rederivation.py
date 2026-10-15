from __future__ import annotations

import pytest

from autosport.drift_control import DriftMonitor, DriftState, DriftWindow
from autosport.scientific_registry import DatasetSnapshot
from autosport.champion_eligibility import (
    ChampionEligibilityDecision,
    ChampionEligibilityError,
    ChampionEligibilityStatus,
    persist_eligibility_decision,
    validate_activation_eligibility,
)
from tests.test_champion_eligibility import (
    CONFIG,
    ENV,
    _eligible_scoped_decision,
    _scoped_decision,
    _scoped_history,
)


def _forged_eligible_over_degraded_evidence(tmp_path):
    registry, findings, windows = _scoped_history(
        tmp_path,
        values_sequence=(("3", "4"), ("3", "4")),
    )
    canonical = _scoped_decision(registry, findings, windows)
    assert canonical.status is ChampionEligibilityStatus.SHADOW_DEACTIVATED
    assert canonical.degraded_streak == 2

    forged = ChampionEligibilityDecision(
        status=ChampionEligibilityStatus.ELIGIBLE,
        canonical_strategy_id=canonical.canonical_strategy_id,
        strategy_version_id=canonical.strategy_version_id,
        model_version_id=canonical.model_version_id,
        environment_sha256=canonical.environment_sha256,
        protocol_id=canonical.protocol_id,
        config_sha256=canonical.config_sha256,
        sport=canonical.sport,
        league=canonical.league,
        regime=canonical.regime,
        finding_ids=canonical.finding_ids,
        finding_record_sha256s=canonical.finding_record_sha256s,
        window_start=canonical.window_start,
        window_end=canonical.window_end,
        evaluated_at=canonical.evaluated_at,
        valid_until=canonical.valid_until,
        minimum_samples=canonical.minimum_samples,
        minimum_effective_sample_size=canonical.minimum_effective_sample_size,
        effective_sample_size=canonical.effective_sample_size,
        degraded_streak=canonical.degraded_streak,
        recovery_streak=canonical.recovery_streak,
        admissible_actions=canonical.admissible_actions,
        research_trigger_id=None,
        reason="forged eligible status over canonically degraded evidence",
    )
    assert forged.status is ChampionEligibilityStatus.ELIGIBLE
    return registry, canonical, forged


def _validate(registry, decision) -> None:
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


def test_persistence_rejects_directly_minted_eligible_status(tmp_path) -> None:
    registry, canonical, forged = _forged_eligible_over_degraded_evidence(tmp_path)

    with pytest.raises(
        ChampionEligibilityError,
        match="does not match canonical derivation",
    ):
        persist_eligibility_decision(registry, forged)

    assert registry.get(forged.record_type, forged.record_id) is None
    assert canonical.status is ChampionEligibilityStatus.SHADOW_DEACTIVATED


def test_activation_rederives_legacy_registered_decision_before_positive_use(
    tmp_path,
) -> None:
    registry, _canonical, forged = _forged_eligible_over_degraded_evidence(tmp_path)

    # Simulate a legacy/caller path that bypassed persist_eligibility_decision and
    # wrote a structurally valid ChampionEligibilityDecision directly to the registry.
    registry.append(forged)

    with pytest.raises(
        ChampionEligibilityError,
        match="does not match canonical derivation",
    ):
        _validate(registry, forged)


def test_canonical_eligible_decision_still_persists_and_activates(tmp_path) -> None:
    registry, decision = _eligible_scoped_decision(tmp_path)

    decision_id = persist_eligibility_decision(registry, decision)

    assert decision_id == decision.decision_id
    _validate(registry, decision)


def test_later_canonical_drift_invalidates_old_eligible_decision_without_future_leakage(
    tmp_path,
) -> None:
    registry, findings, windows = _scoped_history(
        tmp_path,
        values_sequence=(("1", "2"), ("1", "2"), ("3", "4"), ("3", "4")),
    )
    decision = _scoped_decision(registry, findings[:2], windows[:2])
    assert decision.status is ChampionEligibilityStatus.ELIGIBLE
    persist_eligibility_decision(registry, decision)

    # The later degraded windows exist durably but are not yet causal at this cutoff.
    _validate(registry, decision)

    # Once a later same-scope DRIFT_DETECTED window is causally visible, replaying
    # the old ELIGIBLE decision must not reactivate the champion.
    with pytest.raises(
        ChampionEligibilityError,
        match="later canonical drift invalidates champion eligibility",
    ):
        validate_activation_eligibility(
            registry,
            decision,
            as_of="2026-02-17T12:00:00Z",
            canonical_strategy_id="strategy-context",
            expected_strategy_version_id="strategy-1",
            expected_model_version_id="model-1",
            expected_environment_sha256=ENV,
            expected_protocol_id="protocol-1",
            expected_config_sha256=CONFIG,
            admissible_actions=frozenset({"WAIT"}),
        )


def test_late_revision_of_same_window_invalidates_old_eligible_decision(
    tmp_path,
) -> None:
    registry, findings, windows = _scoped_history(
        tmp_path,
        values_sequence=(("1", "2"), ("1", "2")),
    )
    decision = _scoped_decision(registry, findings, windows)
    assert decision.status is ChampionEligibilityStatus.ELIGIBLE
    persist_eligibility_decision(registry, decision)

    prior = windows[-1]
    revised = DriftWindow.from_samples(
        dataset_snapshot_id="dataset-current-2-revision",
        source_identity=prior.source_identity,
        window_start=prior.window_start,
        window_end=prior.window_end,
        as_of="2026-02-14T00:00:00Z",
        values=("3", "4"),
        value_observed_at=(prior.window_start, prior.window_end),
        value_available_at=("2026-02-14T00:00:00Z", "2026-02-14T00:00:00Z"),
        effective_sample_size=2,
        sport=prior.sport,
        league=prior.league,
        regime=prior.regime,
    )
    registry.append(
        DatasetSnapshot(
            dataset_snapshot_id=revised.dataset_snapshot_id,
            manifest_sha256=revised.evidence_sha256,
            source_identity=revised.source_identity,
            license_identity="license:test",
            causal_cutoff=revised.window_end,
            available_at_utc=revised.as_of,
        )
    )
    revised_finding = DriftMonitor(registry).evaluate(
        findings[-1].reference_id,
        revised,
        evaluated_at=revised.as_of,
    )
    assert revised_finding.state is DriftState.DRIFT_DETECTED

    with pytest.raises(
        ChampionEligibilityError,
        match="later canonical drift invalidates champion eligibility",
    ):
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


def test_historical_same_scope_drift_does_not_block_later_recovered_decision(
    tmp_path,
) -> None:
    registry, findings, windows = _scoped_history(
        tmp_path,
        values_sequence=(("3", "4"), ("3", "4"), ("1", "2"), ("1", "2")),
    )
    recovered = _scoped_decision(registry, findings[2:], windows[2:])
    assert recovered.status is ChampionEligibilityStatus.ELIGIBLE
    persist_eligibility_decision(registry, recovered)

    validate_activation_eligibility(
        registry,
        recovered,
        as_of="2026-02-17T12:00:00Z",
        canonical_strategy_id="strategy-context",
        expected_strategy_version_id="strategy-1",
        expected_model_version_id="model-1",
        expected_environment_sha256=ENV,
        expected_protocol_id="protocol-1",
        expected_config_sha256=CONFIG,
        admissible_actions=frozenset({"WAIT"}),
    )


def test_decision_subclass_cannot_cross_authority_boundary(tmp_path) -> None:
    registry, decision = _eligible_scoped_decision(tmp_path)

    class DerivedDecision(ChampionEligibilityDecision):
        pass

    derived = DerivedDecision(**{
        field: getattr(decision, field)
        for field in decision.__dataclass_fields__
        if field != "decision_id"
    })

    with pytest.raises(TypeError, match="exact ChampionEligibilityDecision"):
        persist_eligibility_decision(registry, derived)

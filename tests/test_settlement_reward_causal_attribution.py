from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.agent_loop import (
    AgentLoopError,
    AttributionComponent,
    AttributionFinding,
    AttributionStatus,
    OutcomeAttribution,
)
from autosport.learning_environment import EvidenceTruth


ENVIRONMENT_ID = "1" * 64
EPISODE_ID = "2" * 64
TRANSITION_ID = "3" * 64
ACTION_ID = "4" * 64
OUTCOME_ID = "5" * 64
REWARD_ID = "6" * 64
FORECAST_EVIDENCE = "7" * 64
EXECUTION_EVIDENCE = "8" * 64
RANDOMNESS_EVIDENCE = "9" * 64


def _finding(
    component: AttributionComponent,
    *,
    status: AttributionStatus = AttributionStatus.SUPPORTED,
    contribution: Decimal | None = None,
    evidence_sha256: str = FORECAST_EVIDENCE,
    reason_code: str = "FROZEN_PRESETTLEMENT_EVIDENCE",
) -> AttributionFinding:
    return AttributionFinding(
        component=component,
        status=status,
        evidence_sha256=evidence_sha256,
        evidence_available_at="2026-09-23T00:00:00Z",
        contribution=contribution,
        reason_code=reason_code,
    )


def _attribution(
    *,
    reward_value: Decimal,
    findings: tuple[AttributionFinding, ...],
    truth: EvidenceTruth = EvidenceTruth.OBSERVED,
    simulation_model_id: str | None = None,
) -> OutcomeAttribution:
    return OutcomeAttribution(
        environment_id=ENVIRONMENT_ID,
        episode_id=EPISODE_ID,
        transition_id=TRANSITION_ID,
        action_id=ACTION_ID,
        outcome_id=OUTCOME_ID,
        reward_id=REWARD_ID,
        reward_value=reward_value,
        truth=truth,
        simulation_model_id=simulation_model_id,
        attributed_at="2026-09-23T00:00:01Z",
        findings=tuple(sorted(findings, key=lambda item: item.component.value)),
    )


def test_positive_reward_does_not_force_positive_execution_attribution():
    attribution = _attribution(
        reward_value=Decimal("1.25"),
        findings=(
            _finding(
                AttributionComponent.FORECAST,
                contribution=Decimal("0.10"),
                reason_code="FROZEN_FORECAST_SCORE_CONTRIBUTION",
            ),
            _finding(
                AttributionComponent.EXECUTION,
                contribution=Decimal("-0.05"),
                evidence_sha256=EXECUTION_EVIDENCE,
                reason_code="FACTUAL_ADVERSE_PRICE_MOVEMENT",
            ),
        ),
    )

    assert attribution.reward_value == Decimal("1.25")
    by_component = {finding.component: finding for finding in attribution.findings}
    assert by_component[AttributionComponent.FORECAST].contribution == Decimal("0.10")
    assert by_component[AttributionComponent.EXECUTION].contribution == Decimal("-0.05")


def test_negative_reward_does_not_turn_frozen_forecast_into_categorical_failure():
    forecast = _finding(
        AttributionComponent.FORECAST,
        contribution=Decimal("0.03"),
        reason_code="FROZEN_PROPER_SCORE_ROW_CONTRIBUTION",
    )
    randomness = _finding(
        AttributionComponent.RANDOMNESS,
        status=AttributionStatus.UNKNOWN,
        contribution=None,
        evidence_sha256=RANDOMNESS_EVIDENCE,
        reason_code="SINGLE_OUTCOME_NOT_CAUSALLY_IDENTIFIABLE",
    )

    attribution = _attribution(
        reward_value=Decimal("-1.00"),
        findings=(forecast, randomness),
    )

    by_component = {finding.component: finding for finding in attribution.findings}
    assert by_component[AttributionComponent.FORECAST].status is AttributionStatus.SUPPORTED
    assert by_component[AttributionComponent.FORECAST].reason_code == (
        "FROZEN_PROPER_SCORE_ROW_CONTRIBUTION"
    )
    assert by_component[AttributionComponent.RANDOMNESS].status is AttributionStatus.UNKNOWN
    assert by_component[AttributionComponent.RANDOMNESS].contribution is None


def test_attribution_components_are_not_forced_to_add_up_to_reward():
    attribution = _attribution(
        reward_value=Decimal("3.00"),
        findings=(
            _finding(AttributionComponent.FORECAST, contribution=Decimal("0.20")),
            _finding(
                AttributionComponent.EXECUTION,
                contribution=Decimal("-0.02"),
                evidence_sha256=EXECUTION_EVIDENCE,
            ),
        ),
    )

    asserted_total = sum(
        (
            finding.contribution
            for finding in attribution.findings
            if finding.contribution is not None
        ),
        Decimal("0"),
    )
    assert asserted_total == Decimal("0.18")
    assert asserted_total != attribution.reward_value


def test_missing_causal_credit_cannot_be_zero_filled():
    with pytest.raises(AgentLoopError, match="UNKNOWN attribution cannot assert a contribution"):
        _finding(
            AttributionComponent.RANDOMNESS,
            status=AttributionStatus.UNKNOWN,
            contribution=Decimal("0"),
            evidence_sha256=RANDOMNESS_EVIDENCE,
            reason_code="MISSING_EVIDENCE_IS_NOT_ZERO",
        )


def test_reward_sign_cannot_rewrite_identical_finding_payload():
    findings = (
        _finding(
            AttributionComponent.RANDOMNESS,
            status=AttributionStatus.UNKNOWN,
            contribution=None,
            evidence_sha256=RANDOMNESS_EVIDENCE,
            reason_code="UNRESOLVED_SINGLE_DRAW_RANDOMNESS",
        ),
    )

    win = _attribution(reward_value=Decimal("1"), findings=findings)
    loss = _attribution(reward_value=Decimal("-1"), findings=findings)

    assert win.findings[0].payload() == loss.findings[0].payload()
    assert win.reward_value == -loss.reward_value


def test_truth_provenance_cannot_be_upgraded_by_simulation_label():
    simulated = _attribution(
        reward_value=Decimal("0.50"),
        findings=(
            _finding(
                AttributionComponent.EXECUTION,
                contribution=Decimal("0.01"),
                evidence_sha256=EXECUTION_EVIDENCE,
                reason_code="SIMULATED_EXECUTION_ONLY",
            ),
        ),
        truth=EvidenceTruth.SIMULATED,
        simulation_model_id="paper-fill-simulator-v1",
    )

    assert simulated.truth is EvidenceTruth.SIMULATED
    assert simulated.payload()["truth"] == EvidenceTruth.SIMULATED.value
    assert simulated.payload()["simulation_model_id"] == "paper-fill-simulator-v1"

    with pytest.raises(AgentLoopError, match="observed attribution cannot carry simulation_model_id"):
        _attribution(
            reward_value=Decimal("0.50"),
            findings=simulated.findings,
            truth=EvidenceTruth.OBSERVED,
            simulation_model_id="paper-fill-simulator-v1",
        )


def test_simulated_attribution_requires_explicit_model_identity():
    with pytest.raises(AgentLoopError, match="simulation_model_id"):
        _attribution(
            reward_value=Decimal("0.50"),
            findings=(
                _finding(
                    AttributionComponent.EXECUTION,
                    contribution=Decimal("0.01"),
                    evidence_sha256=EXECUTION_EVIDENCE,
                ),
            ),
            truth=EvidenceTruth.SIMULATED,
            simulation_model_id=None,
        )


def test_narrative_reason_cannot_mint_new_causal_attribution_identity():
    unresolved = _attribution(
        reward_value=Decimal("-1"),
        findings=(
            _finding(
                AttributionComponent.RANDOMNESS,
                status=AttributionStatus.UNKNOWN,
                contribution=None,
                evidence_sha256=RANDOMNESS_EVIDENCE,
                reason_code="UNKNOWN_CAUSE",
            ),
        ),
    )
    narrative = _attribution(
        reward_value=Decimal("-1"),
        findings=(
            _finding(
                AttributionComponent.RANDOMNESS,
                status=AttributionStatus.UNKNOWN,
                contribution=None,
                evidence_sha256=RANDOMNESS_EVIDENCE,
                reason_code="MODEL_FAILED_BECAUSE_DATA_WAS_STALE",
            ),
        ),
    )

    assert unresolved.attribution_id == narrative.attribution_id
    assert unresolved.findings[0].status is AttributionStatus.UNKNOWN
    assert narrative.findings[0].status is AttributionStatus.UNKNOWN
    assert narrative.findings[0].contribution is None

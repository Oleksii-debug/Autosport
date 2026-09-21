from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.learning_environment import EvidenceTruth
from autosport.policy_evaluation import (
    PolicyEvaluationCase,
    PolicyRewardMode,
    QualifiedCounterfactualAuthority,
    evaluate_policy_pair,
)
from autosport.transparent_bandit_policy import ActionEstimate, BanditPolicyState


ENVIRONMENT = "a" * 64
CONFIG = "b" * 64
REWARD_DEF = "c" * 64
EVALUATOR_SOURCE = "d" * 64
QUALIFICATION = "e" * 64
EVIDENCE_A = "f" * 64
EVIDENCE_B = "1" * 64
T0 = "2026-09-21T10:00:00Z"
T1 = "2026-09-21T11:00:00Z"
T2 = "2026-09-21T12:00:00Z"


def _policy(preferred: str) -> BanditPolicyState:
    return BanditPolicyState(
        environment_id=ENVIRONMENT,
        protocol_id="protocol-ticket-fragmentation-v1",
        config_sha256=CONFIG,
        seed=23,
        generation=0,
        estimates=tuple(
            ActionEstimate(
                action_type,
                1,
                Decimal("2") if action_type == preferred else Decimal("0"),
            )
            for action_type in ("BET", "WAIT")
        ),
    )


def _authority() -> QualifiedCounterfactualAuthority:
    return QualifiedCounterfactualAuthority(
        authority_id="paper-settlement-engine:v1",
        authority_version="1",
        evaluator_source_sha256=EVALUATOR_SOURCE,
        qualification_evidence_sha256=QUALIFICATION,
        reward_definition_sha256=REWARD_DEF,
        reward_mode=PolicyRewardMode.MECHANICAL_PAPER,
        scope="table-tennis:pre-match",
    )


def _case(*, sample_id: str, source_evidence_sha256: str) -> PolicyEvaluationCase:
    return PolicyEvaluationCase(
        sample_id=sample_id,
        observed_at=T0,
        reward_available_at=T1,
        admissible_actions=("BET", "WAIT"),
        action_rewards=(
            ("BET", Decimal("1")),
            ("WAIT", Decimal("0")),
        ),
        action_costs=(
            ("BET", Decimal("0")),
            ("WAIT", Decimal("0")),
        ),
        behavior_propensities=(
            ("BET", Decimal("0.5")),
            ("WAIT", Decimal("0.5")),
        ),
        reward_truth=EvidenceTruth.OBSERVED,
        reward_mode=PolicyRewardMode.MECHANICAL_PAPER,
        source_evidence_sha256=source_evidence_sha256,
        regime_id="table-tennis:pre-match",
        counterfactual_source_id="paper-settlement-engine:v1",
    )


def _evaluate(cases: tuple[PolicyEvaluationCase, ...]):
    return evaluate_policy_pair(
        _policy("WAIT"),
        _policy("BET"),
        cases,
        completed_at=T2,
        counterfactual_authority=_authority(),
    )


def test_sample_id_alias_cannot_multiply_one_causal_evidence_sample():
    original = _case(sample_id="ticket-001", source_evidence_sha256=EVIDENCE_A)
    aliased_duplicate = replace(original, sample_id="ticket-001-fragment-b")

    baseline = _evaluate((original,))
    assert baseline.effective_sample_size == Decimal("1")
    assert baseline.practical_improvement == Decimal("1")

    with pytest.raises(ValueError, match="duplicate|evidence|sample"):
        _evaluate((original, aliased_duplicate))


def test_distinct_source_evidence_remains_distinct_support():
    first = _case(sample_id="ticket-001", source_evidence_sha256=EVIDENCE_A)
    second = _case(sample_id="ticket-002", source_evidence_sha256=EVIDENCE_B)

    result = _evaluate((first, second))

    assert result.effective_sample_size == Decimal("2")
    assert result.practical_improvement == Decimal("1")
    assert len(result.samples) == 2

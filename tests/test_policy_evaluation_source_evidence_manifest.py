from decimal import Decimal

from autosport.learning_environment import EvidenceTruth
from autosport.policy_evaluation import (
    PolicyEvaluationCase,
    PolicyRewardMode,
    QualifiedCounterfactualAuthority,
    evaluate_policy_pair,
    policy_evaluation_cases_manifest_sha256,
)
from autosport.transparent_bandit_policy import ActionEstimate, BanditPolicyState


ENVIRONMENT = "a" * 64
CONFIG = "b" * 64
REWARD_DEF = "e" * 64
EVALUATOR_SOURCE = "7" * 64
QUALIFICATION_EVIDENCE = "8" * 64
SOURCE_EVIDENCE_A = "1" * 64
SOURCE_EVIDENCE_B = "2" * 64
T0 = "2026-09-19T10:00:00Z"
T1 = "2026-09-19T11:00:00Z"
T2 = "2026-09-19T12:00:00Z"


def _case(source_evidence_sha256: str) -> PolicyEvaluationCase:
    return PolicyEvaluationCase(
        sample_id="sample-1",
        observed_at=T0,
        reward_available_at=T1,
        admissible_actions=("BET", "WAIT"),
        action_rewards=(
            ("BET", Decimal("2")),
            ("WAIT", Decimal("0")),
        ),
        action_costs=(
            ("BET", Decimal("0.25")),
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


def _policy(*, bet_reward: str, wait_reward: str) -> BanditPolicyState:
    return BanditPolicyState(
        environment_id=ENVIRONMENT,
        protocol_id="protocol-policy-eval-v1",
        config_sha256=CONFIG,
        seed=7,
        generation=0,
        estimates=(
            ActionEstimate("BET", 1, Decimal(bet_reward)),
            ActionEstimate("WAIT", 1, Decimal(wait_reward)),
        ),
    )


def _authority() -> QualifiedCounterfactualAuthority:
    return QualifiedCounterfactualAuthority(
        authority_id="paper-settlement-engine:v1",
        authority_version="1",
        evaluator_source_sha256=EVALUATOR_SOURCE,
        qualification_evidence_sha256=QUALIFICATION_EVIDENCE,
        reward_definition_sha256=REWARD_DEF,
        reward_mode=PolicyRewardMode.MECHANICAL_PAPER,
        scope="table-tennis:pre-match",
        qualification_status="QUALIFIED",
    )


def test_frozen_cohort_manifest_binds_source_evidence_identity() -> None:
    """Observation-evidence substitution must change the frozen cohort identity."""

    original = _case(SOURCE_EVIDENCE_A)
    substituted = _case(SOURCE_EVIDENCE_B)

    assert original.canonical_payload() != substituted.canonical_payload()
    assert original.source_evidence_sha256 != substituted.source_evidence_sha256

    # The cohort manifest is the frozen evaluation-population identity. A source
    # observation/evidence substitution cannot retain that precommit identity,
    # even when future reward payloads and all scheduling/action fields are equal.
    assert policy_evaluation_cases_manifest_sha256(
        (original,)
    ) != policy_evaluation_cases_manifest_sha256((substituted,))


def test_evaluation_dataset_manifest_cannot_launder_source_evidence_substitution() -> None:
    """The paired evaluation must expose the same source-evidence binding."""

    predecessor = _policy(bet_reward="0", wait_reward="1")
    challenger = _policy(bet_reward="2", wait_reward="1")

    first = evaluate_policy_pair(
        predecessor,
        challenger,
        (_case(SOURCE_EVIDENCE_A),),
        completed_at=T2,
        counterfactual_authority=_authority(),
    )
    substituted = evaluate_policy_pair(
        predecessor,
        challenger,
        (_case(SOURCE_EVIDENCE_B),),
        completed_at=T2,
        counterfactual_authority=_authority(),
    )

    # Per-sample payload already records source_evidence_sha256, so retaining the
    # same dataset_manifest_sha256 here would make the frozen cohort identity
    # weaker than the evidence actually consumed by the evaluation.
    assert first.samples != substituted.samples
    assert first.dataset_manifest_sha256 != substituted.dataset_manifest_sha256

from decimal import Decimal

import pytest

from autosport.learning_environment import EvidenceTruth
from autosport.policy_evaluation import (
    PolicyEvaluationCase,
    QualifiedCounterfactualAuthority,
    PolicyEvaluationConfig,
    PolicyRewardMode,
    evaluate_policy_pair,
    policy_evaluation_cases_manifest_sha256,
)
from autosport.transparent_bandit_policy import ActionEstimate, BanditPolicyState


ENVIRONMENT = "a" * 64
CONFIG = "b" * 64
FEATURE_DEF = "c" * 64
FEATURE_SOURCE = "d" * 64
REWARD_DEF = "e" * 64
COST_DEF = "f" * 64
EVIDENCE_1 = "1" * 64
EVIDENCE_2 = "2" * 64
EVALUATOR_SOURCE = "7" * 64
QUALIFICATION_EVIDENCE = "8" * 64
T0 = "2026-09-19T10:00:00Z"
T1 = "2026-09-19T11:00:00Z"
T2 = "2026-09-19T12:00:00Z"


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


def _authority(
    *,
    authority_id="paper-settlement-engine:v1",
    evaluator_source_sha256=EVALUATOR_SOURCE,
    qualification_status="QUALIFIED",
    scope="table-tennis:pre-match",
    allowed=(EVIDENCE_1, EVIDENCE_2),
) -> QualifiedCounterfactualAuthority:
    return QualifiedCounterfactualAuthority(
        authority_id=authority_id,
        authority_version="1",
        evaluator_source_sha256=evaluator_source_sha256,
        qualification_evidence_sha256=QUALIFICATION_EVIDENCE,
        reward_definition_sha256=REWARD_DEF,
        reward_mode=PolicyRewardMode.MECHANICAL_PAPER,
        scope=scope,
        allowed_source_evidence_sha256=tuple(sorted(allowed)),
        qualification_status=qualification_status,
    )


def _case(
    sample_id: str,
    evidence_sha: str,
    *,
    rewards=(("BET", Decimal("2")), ("WAIT", Decimal("0"))),
    propensities=(("BET", Decimal("0.5")), ("WAIT", Decimal("0.5"))),
) -> PolicyEvaluationCase:
    return PolicyEvaluationCase(
        sample_id=sample_id,
        observed_at=T0,
        reward_available_at=T1,
        admissible_actions=("BET", "WAIT"),
        action_rewards=rewards,
        action_costs=(("BET", Decimal("0.25")), ("WAIT", Decimal("0"))),
        behavior_propensities=propensities,
        reward_truth=EvidenceTruth.OBSERVED,
        reward_mode=PolicyRewardMode.MECHANICAL_PAPER,
        source_evidence_sha256=evidence_sha,
        regime_id="table-tennis:pre-match",
        counterfactual_source_id="paper-settlement-engine:v1",
    )


def test_policy_evaluator_executes_exact_policy_choices_on_paired_rewards():
    predecessor = _policy(bet_reward="0", wait_reward="1")
    challenger = _policy(bet_reward="2", wait_reward="1")
    cases = (_case("sample-1", EVIDENCE_1), _case("sample-2", EVIDENCE_2))

    result = evaluate_policy_pair(
        predecessor,
        challenger,
        cases,
        completed_at=T2,
        counterfactual_authority=_authority(),
    )

    assert result.predecessor_policy_id == predecessor.policy_id
    assert result.challenger_policy_id == challenger.policy_id
    assert [sample["predecessor_action"] for sample in result.samples] == ["WAIT", "WAIT"]
    assert [sample["challenger_action"] for sample in result.samples] == ["BET", "BET"]
    assert result.practical_improvement == Decimal("1.75")
    assert dict(result.predecessor_metrics)["policy_loss"] == Decimal("0")
    assert dict(result.challenger_metrics)["policy_loss"] == Decimal("-1.75")
    assert result.effective_sample_size == Decimal("2")
    assert result.dataset_manifest_sha256 == policy_evaluation_cases_manifest_sha256(cases)


def test_policy_choice_change_changes_evaluation_with_same_reward_population():
    predecessor = _policy(bet_reward="0", wait_reward="1")
    challenger_bet = _policy(bet_reward="2", wait_reward="1")
    challenger_wait = _policy(bet_reward="0", wait_reward="2")
    cases = (_case("sample-1", EVIDENCE_1), _case("sample-2", EVIDENCE_2))

    bet_result = evaluate_policy_pair(
        predecessor,
        challenger_bet,
        cases,
        completed_at=T2,
        counterfactual_authority=_authority(),
    )
    wait_result = evaluate_policy_pair(
        predecessor,
        challenger_wait,
        cases,
        completed_at=T2,
        counterfactual_authority=_authority(),
    )

    assert bet_result.practical_improvement == Decimal("1.75")
    assert wait_result.practical_improvement == Decimal("0")
    assert bet_result.evaluation_sha256 != wait_result.evaluation_sha256


def test_observed_chosen_action_cannot_supply_unseen_counterfactual_reward():
    predecessor = _policy(bet_reward="0", wait_reward="1")
    challenger = _policy(bet_reward="2", wait_reward="1")
    case = PolicyEvaluationCase(
        sample_id="observed-only",
        observed_at=T0,
        reward_available_at=T1,
        admissible_actions=("BET", "WAIT"),
        action_rewards=(("WAIT", Decimal("0")),),
        action_costs=(("WAIT", Decimal("0")),),
        behavior_propensities=(("WAIT", Decimal("1")),),
        reward_truth=EvidenceTruth.OBSERVED,
        reward_mode=PolicyRewardMode.OBSERVED_ACTION,
        source_evidence_sha256=EVIDENCE_1,
        regime_id="table-tennis:pre-match",
        historical_action="WAIT",
    )

    with pytest.raises(ValueError, match="counterfactual|unchosen"):
        evaluate_policy_pair(predecessor, challenger, (case,), completed_at=T2)


def test_missing_behavior_support_fails_closed():
    predecessor = _policy(bet_reward="0", wait_reward="1")
    challenger = _policy(bet_reward="2", wait_reward="1")
    case = _case(
        "missing-support",
        EVIDENCE_1,
        propensities=(("WAIT", Decimal("1")),),
    )

    with pytest.raises(ValueError, match="propensity/support"):
        evaluate_policy_pair(
            predecessor,
            challenger,
            (case,),
            completed_at=T2,
            counterfactual_authority=_authority(),
        )


def test_missing_declared_cost_for_supported_reward_fails_closed():
    with pytest.raises(ValueError, match="explicit cost evidence"):
        PolicyEvaluationCase(
            sample_id="missing-cost",
            observed_at=T0,
            reward_available_at=T1,
            admissible_actions=("BET", "WAIT"),
            action_rewards=(("BET", Decimal("2")), ("WAIT", Decimal("0"))),
            action_costs=(("WAIT", Decimal("0")),),
            behavior_propensities=(("BET", Decimal("0.5")), ("WAIT", Decimal("0.5"))),
            reward_truth=EvidenceTruth.OBSERVED,
            reward_mode=PolicyRewardMode.MECHANICAL_PAPER,
            source_evidence_sha256=EVIDENCE_1,
            regime_id="table-tennis:pre-match",
            counterfactual_source_id="paper-settlement-engine:v1",
        )


def test_future_reward_fails_causal_cutoff():
    predecessor = _policy(bet_reward="0", wait_reward="1")
    challenger = _policy(bet_reward="2", wait_reward="1")
    case = PolicyEvaluationCase(
        sample_id="future",
        observed_at=T0,
        reward_available_at="2026-09-20T11:00:00Z",
        admissible_actions=("BET", "WAIT"),
        action_rewards=(("BET", Decimal("2")), ("WAIT", Decimal("0"))),
        action_costs=(("BET", Decimal("0")), ("WAIT", Decimal("0"))),
        behavior_propensities=(("BET", Decimal("0.5")), ("WAIT", Decimal("0.5"))),
        reward_truth=EvidenceTruth.OBSERVED,
        reward_mode=PolicyRewardMode.MECHANICAL_PAPER,
        source_evidence_sha256=EVIDENCE_1,
        regime_id="table-tennis:pre-match",
        counterfactual_source_id="paper-settlement-engine:v1",
    )

    with pytest.raises(ValueError, match="not causally available"):
        evaluate_policy_pair(
            predecessor,
            challenger,
            (case,),
            completed_at=T2,
            counterfactual_authority=_authority(),
        )


def test_counterfactual_reward_requires_frozen_authority():
    predecessor = _policy(bet_reward="0", wait_reward="1")
    challenger = _policy(bet_reward="2", wait_reward="1")
    case = _case("sample-1", EVIDENCE_1)

    with pytest.raises(ValueError, match="frozen qualified authority"):
        evaluate_policy_pair(
            predecessor,
            challenger,
            (case,),
            completed_at=T2,
        )


def test_invented_counterfactual_source_id_fails_closed():
    predecessor = _policy(bet_reward="0", wait_reward="1")
    challenger = _policy(bet_reward="2", wait_reward="1")
    base = _case("sample-1", EVIDENCE_1)
    case = PolicyEvaluationCase(
        sample_id=base.sample_id,
        observed_at=base.observed_at,
        reward_available_at=base.reward_available_at,
        admissible_actions=base.admissible_actions,
        action_rewards=base.action_rewards,
        action_costs=base.action_costs,
        behavior_propensities=base.behavior_propensities,
        reward_truth=base.reward_truth,
        reward_mode=base.reward_mode,
        source_evidence_sha256=base.source_evidence_sha256,
        regime_id=base.regime_id,
        historical_action=base.historical_action,
        counterfactual_source_id="invented-settlement-engine:v99",
    )

    with pytest.raises(ValueError, match="authority identity mismatch"):
        evaluate_policy_pair(
            predecessor,
            challenger,
            (case,),
            completed_at=T2,
            counterfactual_authority=_authority(),
        )


def test_unknown_counterfactual_evidence_digest_fails_closed():
    predecessor = _policy(bet_reward="0", wait_reward="1")
    challenger = _policy(bet_reward="2", wait_reward="1")
    case = _case("sample-unknown", "9" * 64)

    with pytest.raises(ValueError, match="not qualified by frozen authority"):
        evaluate_policy_pair(
            predecessor,
            challenger,
            (case,),
            completed_at=T2,
            counterfactual_authority=_authority(),
        )


def test_unqualified_counterfactual_authority_fails_closed():
    predecessor = _policy(bet_reward="0", wait_reward="1")
    challenger = _policy(bet_reward="2", wait_reward="1")
    case = _case("sample-1", EVIDENCE_1)

    with pytest.raises(ValueError, match="not qualified"):
        evaluate_policy_pair(
            predecessor,
            challenger,
            (case,),
            completed_at=T2,
            counterfactual_authority=_authority(
                qualification_status="UNQUALIFIED"
            ),
        )


def test_policy_evaluation_config_round_trip_is_exact():
    config = PolicyEvaluationConfig(
        "features-policy-v1",
        FEATURE_DEF,
        FEATURE_SOURCE,
        REWARD_DEF,
        COST_DEF,
        "WAIT",
        _authority(),
    )

    restored = PolicyEvaluationConfig.from_frozen_text(config.frozen_text)

    assert restored == config
    assert restored.config_sha256 == config.config_sha256

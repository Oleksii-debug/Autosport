from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.continual_learning_cycle import (
    ContinualLearningCycleError,
    ContinualLearningRecipe,
    FrozenPolicySuite,
    VerifiedLearningExperience,
    run_continual_learning_cycle,
    verify_continual_learning_replay,
)
from autosport.learning_environment import (
    Action,
    EvidenceTruth,
    RewardEvidence,
    Transition,
)
from autosport.policy_evaluation import (
    PolicyEvaluationCase,
    PolicyRewardMode,
    QualifiedCounterfactualAuthority,
)
from autosport.transparent_bandit_policy import BanditPolicyState


ENVIRONMENT = "a" * 64
CONFIG = "b" * 64
COMPLETED_AT = "2026-10-23T12:00:00+00:00"


def _experience(
    sample_id: str,
    *,
    action_type: str,
    reward_value: str,
    ordinal: int,
    source_character: str,
) -> VerifiedLearningExperience:
    observation_id = f"{ordinal:064x}"
    action = Action(
        environment_id=ENVIRONMENT,
        observation_id=observation_id,
        action_type=action_type,
        decided_at=f"2026-10-22T00:{ordinal:02d}:00+00:00",
    )
    outcome_id = f"{ordinal + 1000:064x}"
    reward = RewardEvidence(
        environment_id=ENVIRONMENT,
        action_id=action.action_id,
        outcome_id=outcome_id,
        reward=Decimal(reward_value),
        available_at=f"2026-10-22T00:{ordinal:02d}:10+00:00",
        truth=EvidenceTruth.OBSERVED,
    )
    transition = Transition(
        environment_id=ENVIRONMENT,
        episode_id=f"{ordinal + 2000:064x}",
        step_index=ordinal,
        observation_id=observation_id,
        action_id=action.action_id,
        outcome_id=outcome_id,
        reward_id=reward.reward_id,
        decision_at=action.decided_at,
        resolved_at=f"2026-10-22T00:{ordinal:02d}:20+00:00",
    )
    return VerifiedLearningExperience(
        sample_id=sample_id,
        source_evidence_sha256=source_character * 64,
        action=action,
        reward=reward,
        transition=transition,
    )


def _champion() -> BanditPolicyState:
    policy = BanditPolicyState.initial(
        environment_id=ENVIRONMENT,
        protocol_id="provider-free-cycle-v1",
        config_sha256=CONFIG,
        seed=23,
        action_types=frozenset({"BET", "WAIT"}),
    )
    bootstrap = _experience(
        "bootstrap-wait",
        action_type="WAIT",
        reward_value="1",
        ordinal=1,
        source_character="c",
    )
    champion, _ = policy.update(
        action=bootstrap.action,
        reward=bootstrap.reward,
        transition=bootstrap.transition,
    )
    assert champion.choose(admissible_actions=frozenset({"BET", "WAIT"})) == "WAIT"
    return champion


def _authority() -> QualifiedCounterfactualAuthority:
    return QualifiedCounterfactualAuthority(
        authority_id="mechanical-paper-cycle:v1",
        authority_version="1",
        evaluator_source_sha256="d" * 64,
        qualification_evidence_sha256="e" * 64,
        reward_definition_sha256="f" * 64,
        reward_mode=PolicyRewardMode.MECHANICAL_PAPER,
        scope="table-tennis:pre-match",
    )


def _case(
    sample_id: str,
    *,
    bet_reward: str,
    wait_reward: str,
    source_character: str,
) -> PolicyEvaluationCase:
    return PolicyEvaluationCase(
        sample_id=sample_id,
        observed_at="2026-10-22T08:00:00+00:00",
        reward_available_at="2026-10-22T10:00:00+00:00",
        admissible_actions=("BET", "WAIT"),
        action_rewards=(
            ("BET", Decimal(bet_reward)),
            ("WAIT", Decimal(wait_reward)),
        ),
        action_costs=(("BET", Decimal("0")), ("WAIT", Decimal("0"))),
        behavior_propensities=(
            ("BET", Decimal("0.5")),
            ("WAIT", Decimal("0.5")),
        ),
        reward_truth=EvidenceTruth.OBSERVED,
        reward_mode=PolicyRewardMode.MECHANICAL_PAPER,
        source_evidence_sha256=source_character * 64,
        regime_id="table-tennis:pre-match",
        counterfactual_source_id="mechanical-paper-cycle:v1",
    )


def _inputs(*, retention_bet: str = "1"):
    champion = _champion()
    new = (
        _experience(
            "new-bet",
            action_type="BET",
            reward_value="3",
            ordinal=2,
            source_character="1",
        ),
    )
    replay = (
        _experience(
            "replay-bet",
            action_type="BET",
            reward_value="3",
            ordinal=3,
            source_character="2",
        ),
    )
    retention = FrozenPolicySuite(
        "retention-v1",
        "retention",
        (
            _case(
                "retention-1",
                bet_reward=retention_bet,
                wait_reward="1",
                source_character="3",
            ),
        ),
    )
    holdout = FrozenPolicySuite(
        "holdout-v1",
        "holdout",
        (
            _case(
                "holdout-1",
                bet_reward="2",
                wait_reward="0",
                source_character="4",
            ),
        ),
    )
    recipe = ContinualLearningRecipe(
        champion_policy_id=champion.policy_id,
        retention_suite_sha256=retention.suite_sha256,
        holdout_suite_sha256=holdout.suite_sha256,
        max_updates=2,
        min_holdout_improvement=Decimal("1"),
        max_retention_drop=Decimal("0"),
        min_effective_sample_size=Decimal("1"),
    )
    return champion, new, replay, retention, holdout, recipe


def test_real_provider_free_cycle_produces_replay_verified_candidate() -> None:
    champion, new, replay, retention, holdout, recipe = _inputs()

    result = run_continual_learning_cycle(
        champion,
        new_experiences=new,
        replay_experiences=replay,
        retention_suite=retention,
        holdout_suite=holdout,
        recipe=recipe,
        completed_at=COMPLETED_AT,
        counterfactual_authority=_authority(),
    )

    assert result.state == "CANDIDATE_READY"
    assert result.candidate is not None
    assert result.candidate_policy_id == result.candidate.policy_id
    assert result.candidate.choose(
        admissible_actions=frozenset({"BET", "WAIT"})
    ) == "BET"
    assert result.retention_evaluation.practical_improvement == Decimal("0")
    assert result.holdout_evaluation.practical_improvement == Decimal("2")
    assert tuple(record.population for record in result.updates) == ("new", "replay")
    assert result.promotion_authorized is False
    assert result.qualification_reasons == ()
    assert verify_continual_learning_replay(
        champion,
        new_experiences=new,
        replay_experiences=replay,
        retention_suite=retention,
        holdout_suite=holdout,
        recipe=recipe,
        completed_at=COMPLETED_AT,
        result=result,
        counterfactual_authority=_authority(),
    )


def test_retention_regression_rolls_back_to_exact_champion() -> None:
    champion, new, replay, retention, holdout, recipe = _inputs(retention_bet="-1")

    result = run_continual_learning_cycle(
        champion,
        new_experiences=new,
        replay_experiences=replay,
        retention_suite=retention,
        holdout_suite=holdout,
        recipe=recipe,
        completed_at=COMPLETED_AT,
        counterfactual_authority=_authority(),
    )

    assert result.state == "ROLLED_BACK"
    assert result.candidate is None
    assert result.candidate_policy_id is None
    assert result.rollback_to_policy_id == champion.policy_id
    assert result.retention_evaluation.practical_improvement == Decimal("-2")
    assert result.qualification_reasons == (
        "aggregate retention degradation exceeds tolerance",
        "retention degradation exceeds tolerance in regimes: table-tennis:pre-match",
    )


def test_training_holdout_contamination_fails_before_update() -> None:
    champion, new, replay, retention, holdout, recipe = _inputs()
    contaminated = (replace(new[0], sample_id="holdout-1"),)

    with pytest.raises(ContinualLearningCycleError, match="contaminates"):
        run_continual_learning_cycle(
            champion,
            new_experiences=contaminated,
            replay_experiences=replay,
            retention_suite=retention,
            holdout_suite=holdout,
            recipe=recipe,
            completed_at=COMPLETED_AT,
            counterfactual_authority=_authority(),
        )


def test_training_holdout_source_evidence_contamination_fails_before_update() -> None:
    champion, new, replay, retention, holdout, recipe = _inputs()
    contaminated = (
        replace(
            new[0],
            source_evidence_sha256=holdout.cases[0].source_evidence_sha256,
        ),
    )

    with pytest.raises(
        ContinualLearningCycleError,
        match="training source evidence contaminates",
    ):
        run_continual_learning_cycle(
            champion,
            new_experiences=contaminated,
            replay_experiences=replay,
            retention_suite=retention,
            holdout_suite=holdout,
            recipe=recipe,
            completed_at=COMPLETED_AT,
            counterfactual_authority=_authority(),
        )


def test_cycle_receipt_cannot_self_authorize_promotion() -> None:
    champion, new, replay, retention, holdout, recipe = _inputs()
    result = run_continual_learning_cycle(
        champion,
        new_experiences=new,
        replay_experiences=replay,
        retention_suite=retention,
        holdout_suite=holdout,
        recipe=recipe,
        completed_at=COMPLETED_AT,
        counterfactual_authority=_authority(),
    )

    with pytest.raises(ContinualLearningCycleError, match="own promotion"):
        verify_continual_learning_replay(
            champion,
            new_experiences=new,
            replay_experiences=replay,
            retention_suite=retention,
            holdout_suite=holdout,
            recipe=recipe,
            completed_at=COMPLETED_AT,
            result=replace(result, promotion_authorized=True),
            counterfactual_authority=_authority(),
        )

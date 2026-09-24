from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib

from autosport.learning_environment import Action, EvidenceTruth, RewardEvidence, Transition
from autosport.policy_update_authority import (
    UTILITY_AUTHORITY_UNRESOLVED,
    attempt_utility_bound_update,
)
from autosport.policy_utility_evidence import (
    DecisionKind,
    PolicyUtilityEvidence,
    UtilityCompleteness,
    UtilityTruthClass,
)
from autosport.transparent_bandit_policy import BanditPolicyState


def _case(*, utility_protocol: str):
    environment_id = "a" * 64
    observation_id = "b" * 64
    outcome_id = "c" * 64
    episode_id = "d" * 64
    config_sha256 = "e" * 64

    action = Action(
        environment_id=environment_id,
        observation_id=observation_id,
        action_type="PAPER_PROPOSAL",
        decided_at="2026-09-21T08:00:00Z",
    )
    reward = RewardEvidence(
        environment_id=environment_id,
        action_id=action.action_id,
        outcome_id=outcome_id,
        reward=Decimal("0.25"),
        available_at="2026-09-21T08:00:01Z",
        truth=EvidenceTruth.OBSERVED,
        evidence=(("settlement", "canonical"),),
    )
    transition = Transition(
        environment_id=environment_id,
        episode_id=episode_id,
        step_index=1,
        observation_id=observation_id,
        action_id=action.action_id,
        outcome_id=outcome_id,
        reward_id=reward.reward_id,
        decision_at=action.decided_at,
        resolved_at="2026-09-21T08:00:02Z",
    )
    policy = BanditPolicyState.initial(
        environment_id=environment_id,
        protocol_id="governed-protocol-v1",
        config_sha256=config_sha256,
        seed=1,
        action_types=frozenset({"PAPER_PROPOSAL"}),
    )
    utility = PolicyUtilityEvidence(
        environment_id=environment_id,
        episode_id=episode_id,
        action_id=action.action_id,
        outcome_id=outcome_id,
        reward_id=reward.reward_id,
        transition_id=transition.transition_id,
        policy_id=policy.policy_id,
        model_id="transparent-bandit",
        strategy_id="paper-proposal",
        config_sha256=config_sha256,
        protocol_sha256=hashlib.sha256(utility_protocol.encode("utf-8")).hexdigest(),
        economic_goal_fingerprint="f" * 64,
        risk_fingerprint="1" * 64,
        bankroll_id="paper-bankroll",
        portfolio_identity="paper-portfolio",
        utility_definition_family="owner-net-utility",
        utility_definition_version="v1",
        utility_definition_sha256="2" * 64,
        completeness=UtilityCompleteness.INCOMPLETE,
        truth_class=UtilityTruthClass.OBSERVED,
        decision_kind=DecisionKind.POSITIONED,
        available_at=datetime(2026, 9, 21, 8, 0, 3, tzinfo=timezone.utc),
        currency="EUR",
        utility_value=Decimal("0.25"),
    )
    return policy, action, reward, transition, utility


def test_cross_protocol_utility_is_explicitly_rejected_without_policy_mutation() -> None:
    policy, action, reward, transition, utility = _case(
        utility_protocol="attacker-renamed-protocol"
    )

    successor, evidence = attempt_utility_bound_update(
        policy=policy,
        action=action,
        reward=reward,
        transition=transition,
        utility=utility,
    )

    assert successor == policy
    assert successor.generation == 0
    assert evidence.reason_codes == (
        UTILITY_AUTHORITY_UNRESOLVED,
        "utility_protocol_mismatch",
    )


def test_exact_protocol_binding_does_not_mint_a_false_mismatch() -> None:
    policy, action, reward, transition, utility = _case(
        utility_protocol="governed-protocol-v1"
    )

    successor, evidence = attempt_utility_bound_update(
        policy=policy,
        action=action,
        reward=reward,
        transition=transition,
        utility=utility,
    )

    assert successor == policy
    assert evidence.reason_codes == (UTILITY_AUTHORITY_UNRESOLVED,)

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib

from autosport.learning_environment import (
    CausalLearningEnvironment,
    EnvironmentIdentity,
    EvidenceTruth,
    Observation,
    Outcome,
    RewardEvidence,
)
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
from autosport.research_multiplicity import ExperimentFamilyMember
from autosport.transparent_bandit_policy import BanditPolicyState
from autosport.trial_family_accounting import TrialCandidateLineage


UTC = timezone.utc
BASE = datetime(2026, 9, 20, 14, 0, tzinfo=UTC)


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _environment_and_policy() -> tuple[CausalLearningEnvironment, BanditPolicyState]:
    identity = EnvironmentIdentity(
        source_id="paper-simulator-replication-v1",
        config_id="learning-config-v1",
        data_id="dataset-sim-replication",
        protocol_id="rq-reward-hacking-simulator-replication",
        cutoff_ts=_iso(BASE + timedelta(hours=3)),
        seed=41,
    )
    environment = CausalLearningEnvironment(
        identity,
        episode_key="episode-simulator-replication",
        policy_id="transparent-bandit-bootstrap-v1",
        admissible_actions=frozenset({"PAPER_PROPOSAL", "WAIT"}),
    )
    policy = BanditPolicyState.initial(
        environment_id=environment.environment_id,
        protocol_id="rq-reward-hacking-simulator-replication",
        config_sha256=hashlib.sha256(b"simulator-replication-config-v1").hexdigest(),
        seed=41,
        action_types=frozenset({"PAPER_PROPOSAL", "WAIT"}),
    )
    return environment, policy


def _simulated_transition(
    environment: CausalLearningEnvironment,
    *,
    index: int,
):
    observed_at = BASE + timedelta(minutes=index * 4 + 1)
    available_at = observed_at + timedelta(seconds=1)
    decision_at = available_at + timedelta(seconds=1)
    revealed_at = decision_at + timedelta(minutes=1)
    reward_at = revealed_at + timedelta(seconds=1)
    resolved_at = reward_at + timedelta(seconds=1)

    observation = Observation(
        environment_id=environment.environment_id,
        observed_at=_iso(observed_at),
        available_at=_iso(available_at),
        evidence=(
            ("seed_clone_family", "favorable-fixture-v1"),
            ("simulated_scenario", f"scenario-{index:02d}"),
        ),
    )
    action = environment.act(
        observation,
        action_type="PAPER_PROPOSAL",
        decision_at=_iso(decision_at),
        parameters=(("candidate", f"candidate-{index:02d}"),),
    )
    outcome = Outcome(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        revealed_at=_iso(revealed_at),
        truth=EvidenceTruth.OBSERVED,
        evidence=(("fixture_result", f"result-{index:02d}"),),
    )
    reward = RewardEvidence(
        environment_id=environment.environment_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward=Decimal("0.10") + (Decimal(index) / Decimal("1000")),
        available_at=_iso(reward_at),
        truth=EvidenceTruth.SIMULATED,
        evidence=(("paper_execution", f"optimistic-fill-{index:02d}"),),
        simulation_model_id="optimistic-fill-model-v1",
    )
    transition = environment.resolve(
        action.action_id,
        outcome=outcome,
        reward=reward,
        resolved_at=_iso(resolved_at),
    )
    return action, reward, transition, resolved_at


def _laundered_utility(
    policy: BanditPolicyState,
    action,
    reward: RewardEvidence,
    transition,
    *,
    available_at: datetime,
) -> PolicyUtilityEvidence:
    # Deliberately hostile input: the reward is SIMULATED, while the caller tries
    # to label owner utility OBSERVED. Schema v1 is not allowed to authorize a
    # product policy update, so repetition must not turn this into authority.
    return PolicyUtilityEvidence(
        environment_id=policy.environment_id,
        episode_id=transition.episode_id,
        action_id=action.action_id,
        outcome_id=reward.outcome_id,
        reward_id=reward.reward_id,
        transition_id=transition.transition_id,
        policy_id=policy.policy_id,
        model_id="transparent-bandit",
        strategy_id="paper-proposal",
        config_sha256=policy.config_sha256,
        protocol_sha256=hashlib.sha256(
            b"rq-reward-hacking-simulator-replication"
        ).hexdigest(),
        economic_goal_fingerprint=hashlib.sha256(b"goal").hexdigest(),
        risk_fingerprint=hashlib.sha256(b"risk").hexdigest(),
        bankroll_id="paper-bankroll",
        portfolio_identity="paper-portfolio",
        utility_definition_family="owner-net-utility",
        utility_definition_version="v1",
        utility_definition_sha256=hashlib.sha256(
            b"owner-net-utility-v1"
        ).hexdigest(),
        completeness=UtilityCompleteness.INCOMPLETE,
        truth_class=UtilityTruthClass.OBSERVED,
        decision_kind=DecisionKind.POSITIONED,
        available_at=available_at,
        currency="EUR",
        utility_value=reward.reward,
    )


def test_repeated_simulated_rewards_cannot_activity_farm_governed_policy_updates() -> None:
    environment, policy = _environment_and_policy()

    # The generic learner intentionally accepts simulated reward evidence. Prove
    # this falsifier is exercising a real lower-level mutation path rather than
    # a vacuous malformed-input rejection.
    first_action, first_reward, first_transition, first_resolved_at = (
        _simulated_transition(environment, index=0)
    )
    generic_successor, generic_evidence = policy.update(
        action=first_action,
        reward=first_reward,
        transition=first_transition,
    )
    assert generic_successor != policy
    assert generic_successor.generation == 1
    assert generic_evidence.reward_truth is EvidenceTruth.SIMULATED

    utility = _laundered_utility(
        policy,
        first_action,
        first_reward,
        first_transition,
        available_at=first_resolved_at + timedelta(seconds=1),
    )
    governed_successor, gate = attempt_utility_bound_update(
        policy=policy,
        action=first_action,
        reward=first_reward,
        transition=first_transition,
        utility=utility,
    )
    assert governed_successor == policy
    assert gate.reason_codes == (UTILITY_AUTHORITY_UNRESOLVED,)

    # Distinct action/reward identities cannot accumulate enough synthetic volume
    # to cross the product authority boundary. Every attempt is independently
    # blocked, and the immutable governed policy remains exactly unchanged.
    blocked = {gate.evidence_id}
    for index in range(1, 33):
        action, reward, transition, resolved_at = _simulated_transition(
            environment,
            index=index,
        )
        utility = _laundered_utility(
            policy,
            action,
            reward,
            transition,
            available_at=resolved_at + timedelta(seconds=1),
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
        assert successor.applied_action_ids == ()
        assert successor.applied_reward_ids == ()
        assert evidence.predecessor_policy_id == policy.policy_id
        assert evidence.successor_policy_id == policy.policy_id
        assert evidence.reason_codes == (UTILITY_AUTHORITY_UNRESOLVED,)
        blocked.add(evidence.evidence_id)

    assert len(blocked) == 33
    assert policy.generation == 0
    assert policy.applied_action_ids == ()
    assert policy.applied_reward_ids == ()


def test_trial_candidate_seed_variation_is_not_hidden_replication() -> None:
    base = TrialCandidateLineage(
        strategy_version_id="strategy-1",
        model_version_id="model-1",
        dataset_snapshot_id="dataset-1",
        feature_set_id="features-1",
        seed=7,
        config_sha256="b" * 64,
    )
    seed_clone = TrialCandidateLineage(
        strategy_version_id=base.strategy_version_id,
        model_version_id=base.model_version_id,
        dataset_snapshot_id=base.dataset_snapshot_id,
        feature_set_id=base.feature_set_id,
        seed=8,
        config_sha256=base.config_sha256,
    )
    frozen_member = ExperimentFamilyMember(
        hypothesis_id="hypothesis-1",
        hypothesis_sha256="a" * 64,
        semantic_variant_sha256=base.semantic_sha256,
        candidate_label="candidate",
    )

    assert seed_clone.semantic_sha256 != base.semantic_sha256
    assert seed_clone.semantic_sha256 != frozen_member.semantic_variant_sha256

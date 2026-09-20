from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.agent_loop import AgentLoopRuntime
from autosport.learning_environment import (
    CausalLearningEnvironment,
    EnvironmentIdentity,
    EvidenceTruth,
    Observation,
    Outcome,
    RewardEvidence,
)
from autosport.paper_abstention_learning import (
    PaperAbstentionLearningError,
    PaperAbstentionLearningRuntime,
)


T0 = "2026-09-20T03:00:00+00:00"
T1 = "2026-09-20T03:00:01+00:00"
T2 = "2026-09-20T03:00:05+00:00"
T3 = "2026-09-20T03:00:20+00:00"
T4 = "2026-09-20T03:00:30+00:00"


def _runtime(root: Path):
    identity = EnvironmentIdentity(
        source_id="abstention-atomicity-source",
        config_id="abstention-atomicity-config",
        data_id="abstention-atomicity-data",
        protocol_id="abstention-atomicity-protocol",
        cutoff_ts="2026-09-20T04:00:00+00:00",
        seed=29,
    )
    environment = CausalLearningEnvironment(
        identity,
        episode_key="abstention-atomicity-episode",
        policy_id="abstention-atomicity-policy",
        admissible_actions=frozenset({"WAIT"}),
    )
    loop = AgentLoopRuntime.initialize_pristine(
        root / "agent-loop.json",
        loop_id="abstention-atomicity-loop",
        environment_checkpoint=environment.checkpoint(),
        policy_id=environment.episode.policy_id,
        economic_goal_fingerprint="a" * 64,
        risk_fingerprint="b" * 64,
        source_sha256="c" * 64,
        config_sha256="d" * 64,
        at=T0,
    )
    observation = Observation(
        environment_id=environment.environment_id,
        observed_at=T0,
        available_at=T1,
        evidence=(("opportunity", "none-clears-threshold"),),
    )
    runtime = PaperAbstentionLearningRuntime(
        environment=environment,
        agent_loop=loop,
    )
    action = runtime.begin_abstention(
        observation=observation,
        action_type="WAIT",
        decision_at=T2,
        parameters=(("reason", "no-edge"),),
        at=T2,
    )
    return environment, loop, observation, action, runtime


def _environment_state(environment: CausalLearningEnvironment):
    return (
        environment._step_index,
        environment._chain_sha256,
        environment._last_transition_id,
        frozenset(environment._committed_action_ids),
        tuple(sorted(environment._committed_decision_intents.items())),
        tuple(
            sorted(
                (
                    action_id,
                    pending.observation.observation_id,
                    pending.action.action_id,
                    pending.decision_intent_id,
                    pending.decision_payload_id,
                )
                for action_id, pending in environment._pending.items()
            )
        ),
        tuple(sorted(environment._pending_by_decision_intent.items())),
    )


def _outcome(action, *, truth: EvidenceTruth, model_id: str | None):
    return Outcome(
        environment_id=action.environment_id,
        action_id=action.action_id,
        revealed_at=T3,
        truth=truth,
        evidence=(("market_resolution", "no-position-outcome"),),
        simulation_model_id=model_id,
    )


def _reward(
    action,
    outcome: Outcome,
    *,
    truth: EvidenceTruth,
    model_id: str | None,
):
    return RewardEvidence(
        environment_id=action.environment_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward=Decimal("-0.25"),
        available_at=T4,
        truth=truth,
        evidence=(("reward_basis", "explicit-opportunity-cost-evidence"),),
        simulation_model_id=model_id,
    )


class PaperAbstentionEvidenceAtomicityTests(unittest.TestCase):
    def test_truth_mismatch_is_rejected_before_either_authority_advances(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, _loop, observation, action, runtime = _runtime(root)
            outcome = _outcome(
                action,
                truth=EvidenceTruth.OBSERVED,
                model_id=None,
            )
            mismatched_reward = _reward(
                action,
                outcome,
                truth=EvidenceTruth.SIMULATED,
                model_id="abstention-sim-v1",
            )
            before_environment = _environment_state(environment)
            before_loop = (root / "agent-loop.json").read_bytes()

            with self.assertRaisesRegex(
                PaperAbstentionLearningError,
                "truth labels differ",
            ):
                runtime.finalize_abstention(
                    observation=observation,
                    action=action,
                    outcome=outcome,
                    reward=mismatched_reward,
                    at=T4,
                )

            self.assertEqual(_environment_state(environment), before_environment)
            self.assertEqual((root / "agent-loop.json").read_bytes(), before_loop)

            valid_reward = _reward(
                action,
                outcome,
                truth=EvidenceTruth.OBSERVED,
                model_id=None,
            )
            receipt = runtime.finalize_abstention(
                observation=observation,
                action=action,
                outcome=outcome,
                reward=valid_reward,
                at=T4,
            )
            self.assertEqual(receipt.reward_id, valid_reward.reward_id)

    def test_simulation_model_mismatch_is_rejected_before_either_authority_advances(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, _loop, observation, action, runtime = _runtime(root)
            outcome = _outcome(
                action,
                truth=EvidenceTruth.SIMULATED,
                model_id="abstention-sim-v1",
            )
            mismatched_reward = _reward(
                action,
                outcome,
                truth=EvidenceTruth.SIMULATED,
                model_id="abstention-sim-v2",
            )
            before_environment = _environment_state(environment)
            before_loop = (root / "agent-loop.json").read_bytes()

            with self.assertRaisesRegex(
                PaperAbstentionLearningError,
                "simulation models differ",
            ):
                runtime.finalize_abstention(
                    observation=observation,
                    action=action,
                    outcome=outcome,
                    reward=mismatched_reward,
                    at=T4,
                )

            self.assertEqual(_environment_state(environment), before_environment)
            self.assertEqual((root / "agent-loop.json").read_bytes(), before_loop)

            valid_reward = _reward(
                action,
                outcome,
                truth=EvidenceTruth.SIMULATED,
                model_id="abstention-sim-v1",
            )
            receipt = runtime.finalize_abstention(
                observation=observation,
                action=action,
                outcome=outcome,
                reward=valid_reward,
                at=T4,
            )
            self.assertEqual(receipt.reward_id, valid_reward.reward_id)


if __name__ == "__main__":
    unittest.main()

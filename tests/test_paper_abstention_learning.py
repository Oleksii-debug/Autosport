from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.agent_loop import AgentLoopPhase, AgentLoopRuntime, ExternalEffectState
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


def _runtime(
    root: Path,
    *,
    admissible_actions: frozenset[str] = frozenset({"NO_BET", "WAIT"}),
):
    identity = EnvironmentIdentity(
        source_id="abstention-source",
        config_id="abstention-config",
        data_id="abstention-data",
        protocol_id="abstention-protocol",
        cutoff_ts="2026-09-20T04:00:00+00:00",
        seed=23,
    )
    environment = CausalLearningEnvironment(
        identity,
        episode_key="abstention-episode",
        policy_id="abstention-policy",
        admissible_actions=admissible_actions,
    )
    baseline = environment.checkpoint()
    loop = AgentLoopRuntime.initialize_pristine(
        root / "agent-loop.json",
        loop_id="abstention-loop",
        environment_checkpoint=baseline,
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
    return (
        environment,
        baseline,
        loop,
        observation,
        PaperAbstentionLearningRuntime(environment=environment, agent_loop=loop),
    )


def _evidence(action, *, reward_value: str, truth: EvidenceTruth):
    model_id = "abstention-counterfactual-v1" if truth is EvidenceTruth.SIMULATED else None
    outcome = Outcome(
        environment_id=action.environment_id,
        action_id=action.action_id,
        revealed_at=T3,
        truth=truth,
        evidence=(("market_resolution", "no-position-outcome"),),
        simulation_model_id=model_id,
    )
    reward = RewardEvidence(
        environment_id=action.environment_id,
        action_id=action.action_id,
        outcome_id=outcome.outcome_id,
        reward=Decimal(reward_value),
        available_at=T4,
        truth=truth,
        evidence=(("reward_basis", "explicit-opportunity-cost-evidence"),),
        simulation_model_id=model_id,
    )
    return outcome, reward


class PaperAbstentionLearningTests(unittest.TestCase):
    def test_negative_wait_reward_is_preserved_and_no_effect_authority_is_added(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _environment, _baseline, loop, observation, runtime = _runtime(root)
            action = runtime.begin_abstention(
                observation=observation,
                action_type="WAIT",
                decision_at=T2,
                parameters=(("reason", "edge-below-threshold"),),
                at=T2,
            )
            outcome, reward = _evidence(
                action,
                reward_value="-0.25",
                truth=EvidenceTruth.OBSERVED,
            )

            receipt = runtime.finalize_abstention(
                observation=observation,
                action=action,
                outcome=outcome,
                reward=reward,
                at=T4,
            )

            self.assertEqual(receipt.reward_id, reward.reward_id)
            self.assertIs(receipt.reward_truth, EvidenceTruth.OBSERVED)
            snapshot = loop.snapshot()
            self.assertIs(snapshot.phase, AgentLoopPhase.CHECKPOINT)
            self.assertIs(snapshot.external_effect_state, ExternalEffectState.NONE)
            raw = json.loads((root / "agent-loop.json").read_text(encoding="utf-8"))
            self.assertEqual(raw["resolutions"][0]["reward_value"], "-0.25")
            self.assertEqual(raw["resolutions"][0]["truth"], "observed")
            self.assertEqual(raw["attributions"][0]["reward_value"], "-0.25")
            self.assertEqual(
                raw["attributions"][0]["findings"][0]["evidence_sha256"],
                reward.reward_id,
            )
            self.assertEqual(raw["research_handoffs"], [])

    def test_simulated_no_bet_remains_simulated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _environment, _baseline, _loop, observation, runtime = _runtime(root)
            action = runtime.begin_abstention(
                observation=observation,
                action_type="NO_BET",
                decision_at=T2,
                at=T2,
            )
            outcome, reward = _evidence(
                action,
                reward_value="0.10",
                truth=EvidenceTruth.SIMULATED,
            )

            receipt = runtime.finalize_abstention(
                observation=observation,
                action=action,
                outcome=outcome,
                reward=reward,
                at=T4,
            )

            self.assertIs(receipt.reward_truth, EvidenceTruth.SIMULATED)
            raw = json.loads((root / "agent-loop.json").read_text(encoding="utf-8"))
            self.assertEqual(raw["resolutions"][0]["truth"], "simulated")
            self.assertEqual(
                raw["resolutions"][0]["simulation_model_id"],
                "abstention-counterfactual-v1",
            )
            self.assertEqual(raw["attributions"][0]["truth"], "simulated")
            self.assertEqual(
                raw["attributions"][0]["simulation_model_id"],
                "abstention-counterfactual-v1",
            )

    def test_wait_cannot_bypass_externally_admissible_action_set(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _environment, _baseline, _loop, observation, runtime = _runtime(
                root,
                admissible_actions=frozenset({"PAPER_PROPOSAL"}),
            )
            with self.assertRaisesRegex(
                PaperAbstentionLearningError,
                "canonical authorities rejected abstention action",
            ):
                runtime.begin_abstention(
                    observation=observation,
                    action_type="WAIT",
                    decision_at=T2,
                    at=T2,
                )

    def test_restart_after_resolution_replays_same_transition_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment, baseline, loop, observation, runtime = _runtime(root)
            action = runtime.begin_abstention(
                observation=observation,
                action_type="WAIT",
                decision_at=T2,
                at=T2,
            )
            outcome, reward = _evidence(
                action,
                reward_value="0",
                truth=EvidenceTruth.OBSERVED,
            )
            transition = environment.resolve(
                action.action_id,
                outcome=outcome,
                reward=reward,
                resolved_at=T4,
            )
            loop.record_resolution(
                transition,
                outcome=outcome,
                reward=reward,
                at=T4,
            )
            self.assertIs(loop.snapshot().phase, AgentLoopPhase.EVALUATE)

            resumed_environment = CausalLearningEnvironment.resume(
                environment.identity,
                episode_key=environment.episode.episode_key,
                policy_id=environment.episode.policy_id,
                admissible_actions=frozenset(environment.episode.admissible_actions),
                checkpoint=baseline,
            )
            recovered = PaperAbstentionLearningRuntime(
                environment=resumed_environment,
                agent_loop=AgentLoopRuntime(root / "agent-loop.json"),
            )
            first = recovered.finalize_abstention(
                observation=observation,
                action=action,
                outcome=outcome,
                reward=reward,
                at=T4,
            )
            second = recovered.finalize_abstention(
                observation=observation,
                action=action,
                outcome=outcome,
                reward=reward,
                at=T4,
            )

            self.assertEqual(first, second)
            self.assertEqual(first.transition_id, transition.transition_id)
            raw = json.loads((root / "agent-loop.json").read_text(encoding="utf-8"))
            self.assertEqual(len(raw["resolutions"]), 1)
            self.assertEqual(len(raw["attributions"]), 1)
            self.assertEqual(len(raw["postmortems"]), 1)
            self.assertEqual(raw["phase"], AgentLoopPhase.CHECKPOINT.value)

    def test_finalize_requires_explicit_reward_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _environment, _baseline, _loop, observation, runtime = _runtime(root)
            action = runtime.begin_abstention(
                observation=observation,
                action_type="WAIT",
                decision_at=T2,
                at=T2,
            )
            outcome, _reward = _evidence(
                action,
                reward_value="1",
                truth=EvidenceTruth.OBSERVED,
            )
            with self.assertRaisesRegex(
                TypeError,
                "outcome and reward must be canonical environment evidence",
            ):
                runtime.finalize_abstention(
                    observation=observation,
                    action=action,
                    outcome=outcome,
                    reward=None,  # type: ignore[arg-type]
                    at=T4,
                )


if __name__ == "__main__":
    unittest.main()

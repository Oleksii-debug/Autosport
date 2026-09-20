from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.agent_loop import AgentLoopPhase, AgentLoopRuntime
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


class PaperAbstentionResolutionSameProcessRetryTests(unittest.TestCase):
    def test_record_resolution_publish_failure_recovers_exact_live_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = EnvironmentIdentity(
                source_id="abstention-resolution-retry-source",
                config_id="abstention-resolution-retry-config",
                data_id="abstention-resolution-retry-data",
                protocol_id="abstention-resolution-retry-protocol",
                cutoff_ts="2026-09-20T04:00:00+00:00",
                seed=37,
            )
            environment = CausalLearningEnvironment(
                identity,
                episode_key="abstention-resolution-retry-episode",
                policy_id="abstention-resolution-retry-policy",
                admissible_actions=frozenset({"WAIT"}),
            )
            loop = AgentLoopRuntime.initialize_pristine(
                root / "agent-loop.json",
                loop_id="abstention-resolution-retry-loop",
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
            outcome = Outcome(
                environment_id=environment.environment_id,
                action_id=action.action_id,
                revealed_at=T3,
                truth=EvidenceTruth.OBSERVED,
                evidence=(("market_resolution", "no-position-outcome"),),
            )
            reward = RewardEvidence(
                environment_id=environment.environment_id,
                action_id=action.action_id,
                outcome_id=outcome.outcome_id,
                reward=Decimal("0"),
                available_at=T4,
                truth=EvidenceTruth.OBSERVED,
                evidence=(("reward_basis", "explicit-opportunity-cost-evidence"),),
            )
            before_loop = (root / "agent-loop.json").read_bytes()

            with patch.object(
                loop,
                "record_resolution",
                side_effect=OSError("simulated resolution journal failure"),
            ):
                with self.assertRaisesRegex(
                    OSError,
                    "simulated resolution journal failure",
                ):
                    runtime.finalize_abstention(
                        observation=observation,
                        action=action,
                        outcome=outcome,
                        reward=reward,
                        at=T4,
                    )

            self.assertEqual((root / "agent-loop.json").read_bytes(), before_loop)
            resolved_checkpoint = environment.checkpoint()
            self.assertIsNotNone(resolved_checkpoint.last_transition_id)

            changed_reward = RewardEvidence(
                environment_id=environment.environment_id,
                action_id=action.action_id,
                outcome_id=outcome.outcome_id,
                reward=Decimal("1"),
                available_at=T4,
                truth=EvidenceTruth.OBSERVED,
                evidence=(("reward_basis", "changed"),),
            )
            with self.assertRaisesRegex(
                PaperAbstentionLearningError,
                "environment checkpoint drifted from AgentLoop recovery boundary",
            ):
                runtime.finalize_abstention(
                    observation=observation,
                    action=action,
                    outcome=outcome,
                    reward=changed_reward,
                    at=T4,
                )

            self.assertEqual((root / "agent-loop.json").read_bytes(), before_loop)
            self.assertEqual(environment.checkpoint(), resolved_checkpoint)

            first = runtime.finalize_abstention(
                observation=observation,
                action=action,
                outcome=outcome,
                reward=reward,
                at=T4,
            )
            second = runtime.finalize_abstention(
                observation=observation,
                action=action,
                outcome=outcome,
                reward=reward,
                at=T4,
            )

            self.assertEqual(first, second)
            self.assertEqual(first.transition_id, resolved_checkpoint.last_transition_id)
            self.assertIs(loop.snapshot().phase, AgentLoopPhase.CHECKPOINT)
            raw = json.loads((root / "agent-loop.json").read_text(encoding="utf-8"))
            self.assertEqual(len(raw["resolutions"]), 1)
            self.assertEqual(len(raw["attributions"]), 1)
            self.assertEqual(len(raw["postmortems"]), 1)


if __name__ == "__main__":
    unittest.main()

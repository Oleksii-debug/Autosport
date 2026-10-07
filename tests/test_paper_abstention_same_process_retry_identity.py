from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.agent_loop import AgentLoopRuntime
from autosport.learning_environment import (
    CausalLearningEnvironment,
    EnvironmentIdentity,
    Observation,
)
from autosport.paper_abstention_learning import (
    PaperAbstentionLearningError,
    PaperAbstentionLearningRuntime,
)


T0 = "2026-09-20T03:00:00+00:00"
T1 = "2026-09-20T03:00:01+00:00"
T2 = "2026-09-20T03:00:05+00:00"
T3 = "2026-09-20T03:00:06+00:00"


class PaperAbstentionSameProcessRetryIdentityTests(unittest.TestCase):
    def test_only_exact_pending_action_can_resume_after_intent_write_failure(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            identity = EnvironmentIdentity(
                source_id="abstention-retry-source",
                config_id="abstention-retry-config",
                data_id="abstention-retry-data",
                protocol_id="abstention-retry-protocol",
                cutoff_ts="2026-09-20T04:00:00+00:00",
                seed=31,
            )
            environment = CausalLearningEnvironment(
                identity,
                episode_key="abstention-retry-episode",
                policy_id="abstention-retry-policy",
                admissible_actions=frozenset({"WAIT"}),
            )
            loop = AgentLoopRuntime.initialize_pristine(
                root / "agent-loop.json",
                loop_id="abstention-retry-loop",
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
            before_loop = (root / "agent-loop.json").read_bytes()

            with patch(
                "autosport.paper_abstention_learning.atomic_write_json",
                side_effect=OSError("simulated intent journal failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated intent journal failure"):
                    runtime.begin_abstention(
                        observation=observation,
                        action_type="WAIT",
                        decision_at=T2,
                        parameters=(("reason", "no-edge"),),
                        at=T2,
                    )

            self.assertEqual((root / "agent-loop.json").read_bytes(), before_loop)
            self.assertEqual(len(environment._pending), 1)
            pending_ids = tuple(sorted(environment._pending))
            self.assertFalse(runtime._intent_path.exists())

            with self.assertRaisesRegex(
                PaperAbstentionLearningError,
                "starting abstention conflicts with unresolved environment action",
            ):
                runtime.begin_abstention(
                    observation=observation,
                    action_type="WAIT",
                    decision_at=T3,
                    parameters=(("reason", "no-edge"),),
                    at=T3,
                )

            self.assertEqual((root / "agent-loop.json").read_bytes(), before_loop)
            self.assertEqual(tuple(sorted(environment._pending)), pending_ids)
            self.assertFalse(runtime._intent_path.exists())

            unrelated_observation = Observation(
                environment_id=environment.environment_id,
                observed_at=T0,
                available_at=T1,
                evidence=(("opportunity", "different-observation"),),
            )
            with self.assertRaisesRegex(
                PaperAbstentionLearningError,
                "starting abstention conflicts with unresolved environment action",
            ):
                runtime.begin_abstention(
                    observation=unrelated_observation,
                    action_type="WAIT",
                    decision_at=T2,
                    parameters=(("reason", "no-edge"),),
                    at=T3,
                )

            self.assertEqual((root / "agent-loop.json").read_bytes(), before_loop)
            self.assertEqual(tuple(sorted(environment._pending)), pending_ids)
            self.assertFalse(runtime._intent_path.exists())

            exact = runtime.begin_abstention(
                observation=observation,
                action_type="WAIT",
                decision_at=T2,
                parameters=(("reason", "no-edge"),),
                at=T3,
            )
            self.assertEqual(loop.snapshot().action_id, exact.action_id)
            self.assertTrue(runtime._intent_path.exists())


if __name__ == "__main__":
    unittest.main()

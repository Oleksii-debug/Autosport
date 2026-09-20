from __future__ import annotations

import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.agent_loop import AgentLoopRuntime
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.domain import TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_provenance import provenance_for
from autosport.learning_environment import (
    CausalLearningEnvironment,
    EnvironmentIdentity,
    Observation,
)
from autosport.paper import PaperBook
from autosport.paper_campaign_admission import (
    PaperCampaignAdmissionCoordinator,
    PaperCampaignAdmissionError,
)
from autosport.paper_campaign_runtime import PaperCampaignRuntime
from autosport.paper_settlement_learning import PaperSettlementLearningBridge
from autosport.risk import PaperRiskPolicy


T0 = "2026-09-20T04:00:00+00:00"
T1 = "2026-09-20T04:00:01+00:00"
T2 = "2026-09-20T04:00:05+00:00"
T3 = "2026-09-20T04:00:10+00:00"


class CommittedBindingRecoveryTests(unittest.TestCase):
    def _setup(self, base: Path):
        workspace = base / "workspace"
        workspace.mkdir()
        authority = base / "authority"
        goal = EconomicGoalContract(
            goal_id="binding-goal",
            revision=1,
            bankroll_id="binding-bankroll",
            currency="USD",
        )
        risk = PaperRiskPolicy(economic_goal=goal)
        book = PaperBook("100")
        book.save(workspace / "paper_book.json")
        (workspace / "decisions.jsonl").touch()
        identity = EnvironmentIdentity(
            source_id="binding-source",
            config_id="binding-config",
            data_id="binding-data",
            protocol_id="binding-protocol",
            cutoff_ts="2026-09-20T04:01:00+00:00",
            seed=37,
        )
        environment = CausalLearningEnvironment(
            identity,
            episode_key="binding-episode",
            policy_id="binding-policy",
            admissible_actions=frozenset({"PAPER_PROPOSAL"}),
        )
        observation = Observation(
            environment_id=environment.environment_id,
            observed_at=T0,
            available_at=T1,
            evidence=(("market_state", "binding-snapshot"),),
        )
        baseline = environment.checkpoint()
        AgentLoopRuntime.initialize_pristine(
            workspace / "agent-loop.json",
            loop_id="binding-loop",
            environment_checkpoint=baseline,
            policy_id=environment.episode.policy_id,
            economic_goal_fingerprint=provenance_for(goal).contract_sha256,
            risk_fingerprint=risk.provenance_sha256,
            source_sha256="c" * 64,
            config_sha256="d" * 64,
            at=T0,
        )
        leg = TicketLeg(
            event_id="binding-event",
            market_id="winner",
            selection_id="home",
            locked_odds=Decimal("2.10"),
            sport="table_tennis",
        )

        def runtime(*, resumed: bool):
            active_environment = (
                CausalLearningEnvironment.resume(
                    identity,
                    episode_key="binding-episode",
                    policy_id="binding-policy",
                    admissible_actions=frozenset({"PAPER_PROPOSAL"}),
                    checkpoint=baseline,
                )
                if resumed
                else environment
            )
            ledger = JsonlDecisionLedger(workspace / "decisions.jsonl")
            bridge = PaperSettlementLearningBridge(
                workspace / "paper-learning-bridge.json",
                paper_book_path=workspace / "paper_book.json",
                decision_ledger=ledger,
                agent_loop=AgentLoopRuntime(workspace / "agent-loop.json"),
                economic_goal=goal,
                risk_policy=risk,
            )
            return ledger, bridge, PaperCampaignRuntime(
                environment=active_environment,
                settlement_bridge=bridge,
            )

        def coordinator(*, resumed: bool):
            ledger, bridge, campaign = runtime(resumed=resumed)
            with patch.dict(
                os.environ,
                {"AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR": str(authority)},
            ):
                value = PaperCampaignAdmissionCoordinator(
                    workspace / "paper-campaign-admission.json",
                    paper_book_path=workspace / "paper_book.json",
                    decision_ledger=ledger,
                    runtime=campaign,
                )
            return value, bridge

        def admit(value: PaperCampaignAdmissionCoordinator):
            return value.admit(
                admission_id="binding-admission",
                observation=observation,
                action_type="PAPER_PROPOSAL",
                decision_action="OPEN_PAPER_TICKET",
                decision_at=T2,
                at=T2,
                legs=(leg,),
                stake=Decimal("9"),
                placed_at=T3,
                replay_run_id="binding-run",
                agent="binding-test",
            )

        return workspace, coordinator, admit

    def test_valid_empty_bridge_rollback_is_rejected_without_rebinding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace, coordinator, admit = self._setup(Path(directory))
            first, bridge = coordinator(resumed=False)
            admit(first)
            bridge._write({"bindings": {}})

            retry, rolled_back_bridge = coordinator(resumed=True)
            with self.assertRaisesRegex(
                PaperCampaignAdmissionError,
                "binding is missing",
            ):
                admit(retry)
            self.assertEqual(rolled_back_bridge._read()["bindings"], {})
            self.assertEqual(len(PaperBook.load(workspace / "paper_book.json").tickets), 1)
            self.assertEqual(
                JsonlDecisionLedger(workspace / "decisions.jsonl").verify_integrity(),
                1,
            )

    def test_valid_binding_action_substitution_is_rejected_before_runtime_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace, coordinator, admit = self._setup(Path(directory))
            first, bridge = coordinator(resumed=False)
            admit(first)
            state = bridge._read()
            binding = next(iter(state["bindings"].values()))
            binding["action_id"] = "0" * 64
            bridge._write(state)

            retry, substituted_bridge = coordinator(resumed=True)
            with self.assertRaisesRegex(
                PaperCampaignAdmissionError,
                "binding action_id differs",
            ):
                admit(retry)
            current = next(iter(substituted_bridge._read()["bindings"].values()))
            self.assertEqual(current["action_id"], "0" * 64)
            self.assertEqual(len(PaperBook.load(workspace / "paper_book.json").tickets), 1)


if __name__ == "__main__":
    unittest.main()

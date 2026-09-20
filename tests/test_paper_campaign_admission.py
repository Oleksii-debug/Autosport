from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.agent_loop import AgentLoopPhase, AgentLoopRuntime
from autosport.decision_ledger import JsonlDecisionLedger
from autosport.domain import TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_provenance import provenance_for
from autosport.learning_environment import (
    CausalLearningEnvironment,
    EnvironmentCheckpoint,
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


T0 = "2026-09-20T03:00:00+00:00"
T1 = "2026-09-20T03:00:01+00:00"
T2 = "2026-09-20T03:00:05+00:00"
T3 = "2026-09-20T03:00:10+00:00"


class _Fixture:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.goal = EconomicGoalContract(
            goal_id="admission-goal",
            revision=1,
            bankroll_id="admission-bankroll",
            currency="USD",
        )
        self.risk = PaperRiskPolicy(economic_goal=self.goal)
        self.leg = TicketLeg(
            event_id="admission-event",
            market_id="winner",
            selection_id="home",
            locked_odds=Decimal("2.00"),
            sport="table_tennis",
        )
        book = PaperBook("100")
        book.save(root / "paper_book.json")
        (root / "decisions.jsonl").touch()
        self.identity = EnvironmentIdentity(
            source_id="admission-source",
            config_id="admission-config",
            data_id="admission-data",
            protocol_id="admission-protocol",
            cutoff_ts="2026-09-20T03:01:00+00:00",
            seed=31,
        )
        self.environment = CausalLearningEnvironment(
            self.identity,
            episode_key="admission-episode",
            policy_id="admission-policy",
            admissible_actions=frozenset({"PAPER_PROPOSAL"}),
        )
        self.observation = Observation(
            environment_id=self.environment.environment_id,
            observed_at=T0,
            available_at=T1,
            evidence=(("market_state", "admission-snapshot"),),
        )
        self.baseline = self.environment.checkpoint()
        AgentLoopRuntime.initialize_pristine(
            root / "agent-loop.json",
            loop_id="admission-loop",
            environment_checkpoint=self.baseline,
            policy_id=self.environment.episode.policy_id,
            economic_goal_fingerprint=provenance_for(self.goal).contract_sha256,
            risk_fingerprint=self.risk.provenance_sha256,
            source_sha256="a" * 64,
            config_sha256="b" * 64,
            at=T0,
        )

    def runtime(self, *, resumed: bool = False) -> tuple[JsonlDecisionLedger, PaperCampaignRuntime]:
        if resumed:
            environment = CausalLearningEnvironment.resume(
                self.identity,
                episode_key="admission-episode",
                policy_id="admission-policy",
                admissible_actions=frozenset({"PAPER_PROPOSAL"}),
                checkpoint=self.baseline,
            )
        else:
            environment = self.environment
        ledger = JsonlDecisionLedger(self.root / "decisions.jsonl")
        bridge = PaperSettlementLearningBridge(
            self.root / "paper-learning-bridge.json",
            paper_book_path=self.root / "paper_book.json",
            decision_ledger=ledger,
            agent_loop=AgentLoopRuntime(self.root / "agent-loop.json"),
            economic_goal=self.goal,
            risk_policy=self.risk,
        )
        return ledger, PaperCampaignRuntime(
            environment=environment,
            settlement_bridge=bridge,
        )

    def coordinator(self, *, resumed: bool = False) -> PaperCampaignAdmissionCoordinator:
        ledger, runtime = self.runtime(resumed=resumed)
        return PaperCampaignAdmissionCoordinator(
            self.root / "paper-campaign-admission.json",
            paper_book_path=self.root / "paper_book.json",
            decision_ledger=ledger,
            runtime=runtime,
        )

    def admit(self, coordinator: PaperCampaignAdmissionCoordinator):
        return coordinator.admit(
            admission_id="admission-1",
            observation=self.observation,
            action_type="PAPER_PROPOSAL",
            decision_action="OPEN_PAPER_TICKET",
            decision_at=T2,
            at=T2,
            legs=(self.leg,),
            stake=Decimal("10"),
            placed_at=T3,
            replay_run_id="admission-run",
            agent="admission-test",
        )


class PaperCampaignAdmissionTests(unittest.TestCase):
    def test_admission_commits_exact_ticket_decision_action_and_restart_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = _Fixture(root)
            first = fixture.admit(fixture.coordinator())

            book = PaperBook.load(root / "paper_book.json")
            ledger = JsonlDecisionLedger(root / "decisions.jsonl")
            state = json.loads((root / "paper-campaign-admission.json").read_text(encoding="utf-8"))
            loop = AgentLoopRuntime(root / "agent-loop.json").snapshot()
            self.assertEqual(len(book.tickets), 1)
            self.assertEqual(ledger.verify_integrity(), 1)
            self.assertEqual(state["admissions"]["admission-1"]["phase"], "COMMITTED")
            self.assertEqual(loop.phase, AgentLoopPhase.WAIT_OUTCOME)

            second = fixture.admit(fixture.coordinator(resumed=True))
            self.assertEqual(first, second)
            self.assertEqual(len(PaperBook.load(root / "paper_book.json").tickets), 1)
            self.assertEqual(JsonlDecisionLedger(root / "decisions.jsonl").verify_integrity(), 1)

    def test_retry_after_ticket_save_does_not_create_second_ticket(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = _Fixture(root)
            coordinator = fixture.coordinator()
            with patch.object(
                coordinator.decision_ledger,
                "append_economic",
                side_effect=RuntimeError("crash after ticket durability"),
            ):
                with self.assertRaisesRegex(RuntimeError, "crash after ticket durability"):
                    fixture.admit(coordinator)
            first_ticket_id = next(iter(PaperBook.load(root / "paper_book.json").tickets))

            receipt = fixture.admit(fixture.coordinator(resumed=True))
            self.assertEqual(receipt.ticket_id, first_ticket_id)
            self.assertEqual(len(PaperBook.load(root / "paper_book.json").tickets), 1)
            self.assertEqual(JsonlDecisionLedger(root / "decisions.jsonl").verify_integrity(), 1)

    def test_retry_after_decision_append_reuses_material_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = _Fixture(root)
            coordinator = fixture.coordinator()
            with patch.object(
                coordinator.runtime,
                "begin_and_bind_paper_ticket",
                side_effect=RuntimeError("crash before action"),
            ):
                with self.assertRaisesRegex(RuntimeError, "crash before action"):
                    fixture.admit(coordinator)
            self.assertEqual(JsonlDecisionLedger(root / "decisions.jsonl").verify_integrity(), 1)

            fixture.admit(fixture.coordinator(resumed=True))
            self.assertEqual(JsonlDecisionLedger(root / "decisions.jsonl").verify_integrity(), 1)
            self.assertEqual(len(PaperBook.load(root / "paper_book.json").tickets), 1)

    def test_retry_after_bound_action_before_journal_commit_converges(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = _Fixture(root)
            coordinator = fixture.coordinator()
            original_write = coordinator._write

            def fail_committed(admissions):
                if any(record.get("phase") == "COMMITTED" for record in admissions.values()):
                    raise RuntimeError("crash before admission commit")
                original_write(admissions)

            with patch.object(coordinator, "_write", side_effect=fail_committed):
                with self.assertRaisesRegex(RuntimeError, "crash before admission commit"):
                    fixture.admit(coordinator)
            self.assertEqual(AgentLoopRuntime(root / "agent-loop.json").snapshot().phase, AgentLoopPhase.WAIT_OUTCOME)

            receipt = fixture.admit(fixture.coordinator(resumed=True))
            self.assertTrue(receipt.action_id)
            self.assertEqual(len(PaperBook.load(root / "paper_book.json").tickets), 1)
            self.assertEqual(JsonlDecisionLedger(root / "decisions.jsonl").verify_integrity(), 1)
            state = json.loads((root / "paper-campaign-admission.json").read_text(encoding="utf-8"))
            self.assertEqual(state["admissions"]["admission-1"]["phase"], "COMMITTED")

    def test_digest_corruption_and_retry_substitution_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = _Fixture(root)
            fixture.admit(fixture.coordinator())
            state_path = root / "paper-campaign-admission.json"
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            raw["admissions"]["admission-1"]["action_id"] = "substituted"
            state_path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(PaperCampaignAdmissionError, "digest mismatch"):
                fixture.coordinator(resumed=True)


if __name__ == "__main__":
    unittest.main()

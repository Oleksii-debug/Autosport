from __future__ import annotations

import json
import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import autosport.paper_campaign_admission as admission_module
from autosport.agent_loop import AgentLoopPhase, AgentLoopRuntime
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


T0 = "2026-09-20T05:00:00+00:00"
T1 = "2026-09-20T05:00:01+00:00"
T2 = "2026-09-20T05:00:05+00:00"
T3 = "2026-09-20T05:00:10+00:00"


class _Fixture:
    def __init__(self, base: Path) -> None:
        self.workspace = base / "workspace"
        self.workspace.mkdir()
        self.authority = base / "authority"
        self.goal = EconomicGoalContract(
            goal_id="witness-goal",
            revision=1,
            bankroll_id="witness-bankroll",
            currency="USD",
        )
        self.risk = PaperRiskPolicy(economic_goal=self.goal)
        self.leg = TicketLeg(
            event_id="witness-event",
            market_id="winner",
            selection_id="home",
            locked_odds=Decimal("2.25"),
            sport="table_tennis",
        )
        PaperBook("100").save(self.workspace / "paper_book.json")
        (self.workspace / "decisions.jsonl").touch()
        self.identity = EnvironmentIdentity(
            source_id="witness-source",
            config_id="witness-config",
            data_id="witness-data",
            protocol_id="witness-protocol",
            cutoff_ts="2026-09-20T05:01:00+00:00",
            seed=41,
        )
        self.environment = CausalLearningEnvironment(
            self.identity,
            episode_key="witness-episode",
            policy_id="witness-policy",
            admissible_actions=frozenset({"PAPER_PROPOSAL"}),
        )
        self.observation = Observation(
            environment_id=self.environment.environment_id,
            observed_at=T0,
            available_at=T1,
            evidence=(("market_state", "witness-snapshot"),),
        )
        self.baseline = self.environment.checkpoint()
        AgentLoopRuntime.initialize_pristine(
            self.workspace / "agent-loop.json",
            loop_id="witness-loop",
            environment_checkpoint=self.baseline,
            policy_id=self.environment.episode.policy_id,
            economic_goal_fingerprint=provenance_for(self.goal).contract_sha256,
            risk_fingerprint=self.risk.provenance_sha256,
            source_sha256="e" * 64,
            config_sha256="f" * 64,
            at=T0,
        )

    def coordinator(self, *, resumed: bool = False) -> PaperCampaignAdmissionCoordinator:
        environment = (
            CausalLearningEnvironment.resume(
                self.identity,
                episode_key="witness-episode",
                policy_id="witness-policy",
                admissible_actions=frozenset({"PAPER_PROPOSAL"}),
                checkpoint=self.baseline,
            )
            if resumed
            else self.environment
        )
        ledger = JsonlDecisionLedger(self.workspace / "decisions.jsonl")
        bridge = PaperSettlementLearningBridge(
            self.workspace / "paper-learning-bridge.json",
            paper_book_path=self.workspace / "paper_book.json",
            decision_ledger=ledger,
            agent_loop=AgentLoopRuntime(self.workspace / "agent-loop.json"),
            economic_goal=self.goal,
            risk_policy=self.risk,
        )
        runtime = PaperCampaignRuntime(
            environment=environment,
            settlement_bridge=bridge,
        )
        with patch.dict(
            os.environ,
            {"AUTOSPORT_PAPER_EXECUTION_WITNESS_DIR": str(self.authority)},
        ):
            return PaperCampaignAdmissionCoordinator(
                self.workspace / "paper-campaign-admission.json",
                paper_book_path=self.workspace / "paper_book.json",
                decision_ledger=ledger,
                runtime=runtime,
            )

    def admit(self, coordinator: PaperCampaignAdmissionCoordinator):
        return coordinator.admit(
            admission_id="witness-admission",
            observation=self.observation,
            action_type="PAPER_PROPOSAL",
            decision_action="OPEN_PAPER_TICKET",
            decision_at=T2,
            at=T2,
            legs=(self.leg,),
            stake=Decimal("11"),
            placed_at=T3,
            replay_run_id="witness-run",
            agent="witness-test",
        )

    def assert_one_effect(self, testcase: unittest.TestCase, action_id: str) -> None:
        testcase.assertEqual(
            len(PaperBook.load(self.workspace / "paper_book.json").tickets),
            1,
        )
        testcase.assertEqual(
            JsonlDecisionLedger(self.workspace / "decisions.jsonl").verify_integrity(),
            1,
        )
        snapshot = AgentLoopRuntime(self.workspace / "agent-loop.json").snapshot()
        testcase.assertEqual(snapshot.phase, AgentLoopPhase.WAIT_OUTCOME)
        testcase.assertEqual(snapshot.action_id, action_id)


class PaperCampaignAdmissionWitnessRecoveryTests(unittest.TestCase):
    def test_prepared_witness_fsync_before_local_replace_recovers_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _Fixture(Path(directory))
            coordinator = fixture.coordinator()
            with patch.object(
                admission_module,
                "atomic_write_json",
                side_effect=RuntimeError("crash after PREPARED witness fsync"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "crash after PREPARED witness fsync",
                ):
                    fixture.admit(coordinator)

            local = json.loads(
                (fixture.workspace / "paper-campaign-admission.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(local["generation"], 1)
            self.assertEqual(local["admissions"], {})
            self.assertEqual(len(PaperBook.load(fixture.workspace / "paper_book.json").tickets), 0)
            self.assertEqual(
                JsonlDecisionLedger(fixture.workspace / "decisions.jsonl").verify_integrity(),
                0,
            )

            receipt = fixture.admit(fixture.coordinator(resumed=True))
            fixture.assert_one_effect(self, receipt.action_id)
            state = json.loads(
                (fixture.workspace / "paper-campaign-admission.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["admissions"]["witness-admission"]["phase"], "COMMITTED")

    @staticmethod
    def _partial_candidate_then_error(path: Path, payload: str) -> None:
        prefix = payload[: max(1, len(payload) // 2)]
        with path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(prefix)
            handle.flush()
            os.fsync(handle.fileno())
        raise OSError("simulated short witness candidate write")

    def test_partial_prepared_witness_candidate_never_corrupts_live_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _Fixture(Path(directory))
            coordinator = fixture.coordinator()
            witness_before = coordinator._witness_path.read_bytes()

            with patch.object(
                coordinator,
                "_write_witness_candidate",
                side_effect=self._partial_candidate_then_error,
            ):
                with self.assertRaisesRegex(
                    PaperCampaignAdmissionError,
                    "durability barrier failed",
                ):
                    fixture.admit(coordinator)

            self.assertEqual(coordinator._witness_path.read_bytes(), witness_before)
            receipt = fixture.admit(fixture.coordinator(resumed=True))
            fixture.assert_one_effect(self, receipt.action_id)

    def test_partial_committed_witness_candidate_recovers_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _Fixture(Path(directory))
            coordinator = fixture.coordinator()
            original = coordinator._write_witness_candidate

            def fail_only_commit(path: Path, payload: str) -> None:
                last = json.loads(payload.rstrip().splitlines()[-1])
                if last["event"] == "COMMIT" and last["generation"] > 1:
                    self._partial_candidate_then_error(path, payload)
                original(path, payload)

            with patch.object(
                coordinator,
                "_write_witness_candidate",
                side_effect=fail_only_commit,
            ):
                with self.assertRaisesRegex(
                    PaperCampaignAdmissionError,
                    "durability barrier failed",
                ):
                    fixture.admit(coordinator)

            durable_action_id = AgentLoopRuntime(
                fixture.workspace / "agent-loop.json"
            ).snapshot().action_id
            self.assertIsNotNone(durable_action_id)
            fixture.assert_one_effect(self, durable_action_id)

            receipt = fixture.admit(fixture.coordinator(resumed=True))
            self.assertEqual(receipt.action_id, durable_action_id)
            fixture.assert_one_effect(self, durable_action_id)

    def test_complete_witness_record_corruption_still_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _Fixture(Path(directory))
            coordinator = fixture.coordinator()
            lines = coordinator._witness_path.read_text(encoding="utf-8").splitlines()
            first = json.loads(lines[0])
            first["state_sha256"] = "0" * 64
            lines[0] = json.dumps(first, sort_keys=True, separators=(",", ":"))
            coordinator._witness_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(
                PaperCampaignAdmissionError,
                "witness digest mismatch",
            ):
                fixture.coordinator(resumed=True)

    def test_valid_old_complete_witness_prefix_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _Fixture(Path(directory))
            coordinator = fixture.coordinator()
            old_witness = coordinator._witness_path.read_bytes()
            fixture.admit(coordinator)
            coordinator._witness_path.write_bytes(old_witness)

            with self.assertRaisesRegex(
                PaperCampaignAdmissionError,
                "older than monotonic authority",
            ):
                fixture.coordinator(resumed=True)

    def test_committed_witness_fsync_before_local_replace_recovers_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fixture = _Fixture(Path(directory))
            coordinator = fixture.coordinator()
            original_atomic_write = admission_module.atomic_write_json

            def fail_committed_local_write(path, value):
                admissions = value.get("admissions", {}) if isinstance(value, dict) else {}
                if any(
                    isinstance(record, dict) and record.get("phase") == "COMMITTED"
                    for record in admissions.values()
                ):
                    raise RuntimeError("crash after COMMITTED witness fsync")
                return original_atomic_write(path, value)

            with patch.object(
                admission_module,
                "atomic_write_json",
                side_effect=fail_committed_local_write,
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "crash after COMMITTED witness fsync",
                ):
                    fixture.admit(coordinator)

            local = json.loads(
                (fixture.workspace / "paper-campaign-admission.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(local["admissions"]["witness-admission"]["phase"], "PREPARED")
            durable_action_id = AgentLoopRuntime(
                fixture.workspace / "agent-loop.json"
            ).snapshot().action_id
            self.assertIsNotNone(durable_action_id)
            fixture.assert_one_effect(self, durable_action_id)

            receipt = fixture.admit(fixture.coordinator(resumed=True))
            self.assertEqual(receipt.action_id, durable_action_id)
            fixture.assert_one_effect(self, durable_action_id)
            state = json.loads(
                (fixture.workspace / "paper-campaign-admission.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(state["admissions"]["witness-admission"]["phase"], "COMMITTED")


if __name__ == "__main__":
    unittest.main()

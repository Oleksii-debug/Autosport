from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from autosport.dataset import load_dataset
from autosport.decision_ledger import (
    ECONOMIC_DECISION_KIND,
    DecisionLedgerIntegrityError,
    DecisionRecord,
    JsonlDecisionLedger,
)
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_provenance import provenance_for
from autosport.economic_goal_store import EconomicGoalStore
from autosport.risk import PaperRiskPolicy
from autosport.session import AutosportSession


class SessionEconomicGoalRuntimeTests(unittest.TestCase):
    @staticmethod
    def _goal(*, revision: int = 1, emergency_stop: bool = False) -> EconomicGoalContract:
        return EconomicGoalContract(
            goal_id="owner-goal",
            revision=revision,
            bankroll_id="paper-bankroll",
            currency="USD",
            emergency_stop=emergency_stop,
        )

    def test_active_goal_rejects_baseline_before_economic_run_state(self) -> None:
        dataset = load_dataset(Path("examples/tt_demo"))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            EconomicGoalStore(workspace).initialize_owner(self._goal())
            session = AutosportSession(workspace, "10000", strategy_id="baseline-v1")
            try:
                with self.assertRaisesRegex(
                    ValueError,
                    "does not have proven EconomicGoal-aware sizing semantics",
                ):
                    session.run_dataset(dataset)

                self.assertEqual(session.registry.strategy_ids(), ())
                self.assertFalse(session.registry.in_progress())
                self.assertFalse((workspace / "paper_book.json").exists())
                self.assertEqual(tuple(workspace.glob("run-*.json")), ())
            finally:
                session.close()

    def test_observe_only_run_binds_goal_and_policy_into_runtime_identity(self) -> None:
        dataset = load_dataset(Path("examples/tt_demo"))
        goal = self._goal()
        expected_goal = provenance_for(goal)
        expected_policy = PaperRiskPolicy(economic_goal=goal)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            EconomicGoalStore(workspace).initialize_owner(goal)
            session = AutosportSession(
                workspace,
                "10000",
                strategy_id="observe-only-v1",
            )
            try:
                result = session.run_dataset(dataset)
                summary = json.loads(Path(result.result_path).read_text(encoding="utf-8"))
                runtime = summary["strategy_runtime"]

                self.assertTrue(
                    summary["strategy_id"].startswith("observe-only-v1::economic:")
                )
                self.assertEqual(
                    runtime["economic_goal_provenance"]["contract_sha256"],
                    expected_goal.contract_sha256,
                )
                self.assertEqual(
                    runtime["risk_policy_provenance"]["sha256"],
                    expected_policy.provenance_sha256,
                )
                self.assertEqual(
                    runtime["risk_policy_provenance"]["economic_goal_contract_sha256"],
                    expected_goal.contract_sha256,
                )
                self.assertEqual(session.registry.strategy_ids(), (summary["strategy_id"],))
            finally:
                session.close()

    def test_economic_decision_restart_requires_exact_policy_identity(self) -> None:
        goal = self._goal()
        policy = PaperRiskPolicy(economic_goal=goal)
        changed_policy = PaperRiskPolicy(
            max_ticket_fraction=Decimal("0.01"),
            economic_goal=goal,
        )
        with tempfile.TemporaryDirectory() as tmp:
            ledger = JsonlDecisionLedger(Path(tmp) / "decisions.jsonl")
            record = DecisionRecord(
                replay_run_id="run-1",
                agent="test-agent",
                observed_ts="2026-09-18T12:00:00+00:00",
                action="OPEN_PAPER_TICKET",
                payload={"material_action_id": "action-1"},
                context_hash="context-1",
                decision_kind=ECONOMIC_DECISION_KIND,
            )
            ledger.append_economic(record, goal, risk_policy=policy)

            persisted = ledger.verified_economic_decision(
                record.decision_id,
                goal,
                risk_policy=policy,
            )
            self.assertEqual(
                persisted.payload["risk_policy_provenance"]["sha256"],
                policy.provenance_sha256,
            )
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "risk-policy provenance mismatch",
            ):
                ledger.verified_economic_decision(
                    record.decision_id,
                    goal,
                    risk_policy=changed_policy,
                )


if __name__ == "__main__":
    unittest.main()

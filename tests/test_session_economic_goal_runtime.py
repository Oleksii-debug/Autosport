from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from autosport.dataset import load_dataset
from autosport.decision_ledger import (
    ECONOMIC_DECISION_KIND,
    DecisionLedgerIntegrityError,
    DecisionRecord,
    JsonlDecisionLedger,
)
from autosport.economic_goal import EconomicGoalContract, EconomicGoalContractError
from autosport.economic_goal_provenance import provenance_for
from autosport.economic_goal_store import EconomicGoalStore
from autosport.research_strategy import (
    RESEARCH_STRATEGY_ID,
    ResearchStrategyPlan,
    market_event_evidence_hash,
    research_market_snapshot_hash,
)
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

    def _research_plan(self) -> ResearchStrategyPlan:
        dataset = load_dataset(Path("examples/tt_demo"))
        latest = {}
        trigger = None
        for event in sorted(
            dataset.load_market_events(),
            key=lambda item: (item.observed_ts, item.sequence, item.dedupe_key),
        ):
            latest[event.quote_key] = event
            if event.sequence == 4:
                trigger = event
                break
        self.assertIsNotNone(trigger)
        assert trigger is not None
        probability = Decimal("0.60")
        evidence_hash = market_event_evidence_hash(trigger)
        snapshot_hash = research_market_snapshot_hash(latest, [trigger.quote_key])
        other_quote = "tt-demo-1|winner|player-a"
        return ResearchStrategyPlan.from_dict(
            {
                "schema_version": 1,
                "strategy_id": RESEARCH_STRATEGY_ID,
                "decisions": [
                    {
                        "decision_id": "goal-runtime-research-demo",
                        "trigger_quote_key": trigger.quote_key,
                        "decision_ts": trigger.observed_ts,
                        "stake": "10",
                        "candidate": {
                            "legs": [
                                {
                                    "quote_key": trigger.quote_key,
                                    "decimal_odds": str(trigger.decimal_odds),
                                    "probability": str(probability),
                                }
                            ]
                        },
                        "scenario_groups": [
                            {
                                "group_id": "tt-demo-1-winner",
                                "outcomes": [
                                    {
                                        "quote_key": other_quote,
                                        "probability": str(Decimal("1") - probability),
                                    },
                                    {
                                        "quote_key": trigger.quote_key,
                                        "probability": str(probability),
                                    },
                                ],
                            }
                        ],
                        "forecasts": [
                            {
                                "forecast_id": "goal-runtime-forecast",
                                "quote_key": trigger.quote_key,
                                "probability": str(probability),
                                "model_id": "typed-demo-model",
                                "model_version": "1.0.0",
                                "strategy_version": RESEARCH_STRATEGY_ID,
                                "model_training_cutoff_ts": "2026-09-12T09:00:00+00:00",
                                "input_cutoff_ts": trigger.observed_ts,
                                "generated_at": trigger.observed_ts,
                                "uncertainty": "0.10",
                                "evidence_hashes": [evidence_hash],
                                "market_snapshot_hash": snapshot_hash,
                                "provenance": {"source": "goal-runtime-test"},
                            }
                        ],
                        "evidence": [
                            {
                                "evidence_id": "goal-runtime-evidence",
                                "quote_key": trigger.quote_key,
                                "source_id": trigger.source_id,
                                "observed_at": trigger.observed_ts,
                                "available_at": trigger.observed_ts,
                                "decimal_odds": str(trigger.decimal_odds),
                                "content_sha256": evidence_hash,
                                "quality_flags": [],
                                "market_snapshot_hash": snapshot_hash,
                            }
                        ],
                    }
                ],
            }
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

                # Once this workspace has goal-bound economic history, disappearance
                # of the durable owner contract must not silently restore legacy mode.
                (workspace / EconomicGoalStore.FILE_NAME).unlink()
                with self.assertRaisesRegex(
                    ValueError,
                    "persisted EconomicGoal authority is missing",
                ):
                    session.run_dataset(dataset, allow_repeat=True)
                self.assertEqual(session.registry.strategy_ids(), (summary["strategy_id"],))
            finally:
                session.close()

    def test_research_runtime_uses_goal_policy_instead_of_plan_stake(self) -> None:
        dataset = load_dataset(Path("examples/tt_demo"))
        plan = self._research_plan()
        goal = self._goal()
        policy = PaperRiskPolicy(economic_goal=goal)
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            EconomicGoalStore(workspace).initialize_owner(goal)
            session = AutosportSession(
                workspace,
                "1000",
                strategy_id=RESEARCH_STRATEGY_ID,
                research_plan=plan,
            )
            try:
                result = session.run_dataset(dataset)
                self.assertEqual(result.balance, Decimal("1000"))
                self.assertEqual(result.settled_ticket_ids, ())
                records = session.ledger.verified_records()
                self.assertEqual(len(records), 1)
                record = records[0]
                self.assertEqual(record.action, "REJECT_PAPER_RESEARCH_CANDIDATE")
                self.assertEqual(record.payload["stake"], "0")
                self.assertEqual(record.payload["stake_source"], "economic-goal-derived")
                self.assertEqual(
                    record.payload["economic_goal_provenance"]["contract_sha256"],
                    provenance_for(goal).contract_sha256,
                )
                self.assertEqual(
                    record.payload["risk_policy_provenance"]["sha256"],
                    policy.provenance_sha256,
                )
            finally:
                session.close()

    def test_corrupt_goal_fails_before_registry_or_paper_mutation(self) -> None:
        dataset = load_dataset(Path("examples/tt_demo"))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session = AutosportSession(
                workspace,
                "1000",
                strategy_id="observe-only-v1",
            )
            try:
                (workspace / EconomicGoalStore.FILE_NAME).write_text(
                    "{not-json",
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    EconomicGoalContractError,
                    "invalid economic goal JSON",
                ):
                    session.run_dataset(dataset)
                self.assertEqual(session.registry.strategy_ids(), ())
                self.assertFalse(session.registry.in_progress())
                self.assertFalse((workspace / "paper_book.json").exists())
            finally:
                session.close()

    def test_goal_revision_changes_runtime_experiment_identity(self) -> None:
        dataset = load_dataset(Path("examples/tt_demo"))
        first_goal = self._goal()
        second_goal = replace(
            first_goal,
            revision=2,
            max_stake_fraction=Decimal("0.01"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            store = EconomicGoalStore(workspace)
            store.initialize_owner(first_goal)
            session = AutosportSession(
                workspace,
                "1000",
                strategy_id="observe-only-v1",
            )
            try:
                first = session.run_dataset(dataset)
                first_summary = json.loads(
                    Path(first.result_path).read_text(encoding="utf-8")
                )
                store.persist_automatic_successor(second_goal)
                second = session.run_dataset(dataset)
                second_summary = json.loads(
                    Path(second.result_path).read_text(encoding="utf-8")
                )
                self.assertNotEqual(
                    first_summary["strategy_id"],
                    second_summary["strategy_id"],
                )
                self.assertEqual(
                    first_summary["strategy_runtime"]["economic_goal_provenance"][
                        "contract_sha256"
                    ],
                    provenance_for(first_goal).contract_sha256,
                )
                self.assertEqual(
                    second_summary["strategy_runtime"]["economic_goal_provenance"][
                        "contract_sha256"
                    ],
                    provenance_for(second_goal).contract_sha256,
                )
                self.assertEqual(len(session.registry.strategy_ids()), 2)
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

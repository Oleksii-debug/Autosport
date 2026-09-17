import json
import math
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from autosport.decision_ledger import (
    ECONOMIC_DECISION_KIND,
    ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY,
    DecisionLedgerIntegrityError,
    DecisionRecord,
    JsonlDecisionLedger,
)
from autosport.economic_goal import AutomationLevel, EconomicGoalContract


class DecisionLedgerTests(unittest.TestCase):
    @staticmethod
    def _record(*, decision_id: str | None = None) -> DecisionRecord:
        kwargs = {} if decision_id is None else {"decision_id": decision_id}
        return DecisionRecord(
            "run-1",
            "agent",
            "2026-01-01T00:00:00+00:00",
            "OBSERVE",
            {"x": 1},
            "ctx",
            **kwargs,
        )

    @staticmethod
    def _economic_record(*, decision_id: str | None = None) -> DecisionRecord:
        kwargs = {} if decision_id is None else {"decision_id": decision_id}
        return DecisionRecord(
            "run-1",
            "agent",
            "2026-01-01T00:00:00+00:00",
            "PROPOSE_STAKE",
            {"x": 1},
            "ctx",
            decision_kind=ECONOMIC_DECISION_KIND,
            **kwargs,
        )

    @staticmethod
    def _economic_goal(**changes: object) -> EconomicGoalContract:
        values: dict[str, object] = {
            "goal_id": "owner-goal-v1",
            "revision": 1,
            "bankroll_id": "paper-main",
            "currency": "EUR",
            "max_stake_fraction": Decimal("0.02"),
            "max_session_loss_fraction": Decimal("0.05"),
            "max_day_loss_fraction": Decimal("0.05"),
            "max_drawdown_fraction": Decimal("0.20"),
            "max_capital_at_risk_fraction": Decimal("0.20"),
            "max_event_concentration_fraction": Decimal("0.50"),
            "max_market_concentration_fraction": Decimal("0.50"),
            "max_provider_concentration_fraction": Decimal("0.50"),
            "max_sport_concentration_fraction": Decimal("0.50"),
            "max_turnover_fraction": Decimal("1.5"),
            "max_risk_of_ruin": Decimal("0.01"),
            "max_execution_slippage_fraction": Decimal("0.01"),
            "max_quote_age_seconds": Decimal("5"),
            "minimum_data_quality": Decimal("0.70"),
            "max_concurrent_positions": 5,
            "max_parlay_legs": 4,
            "automation_level": AutomationLevel.SUPERVISED_EXECUTION,
            "blocked_sports": frozenset({"football"}),
            "blocked_providers": frozenset({"provider:a"}),
            "blocked_markets": frozenset({"market:test"}),
        }
        values.update(changes)
        return EconomicGoalContract(**values)  # type: ignore[arg-type]

    def test_append_only_record_is_hashed_and_has_no_outcome_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            digest = ledger.append(self._record())
            envelope = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertEqual(envelope["sha256"], digest)
            self.assertNotIn("result", envelope["record"])
            self.assertNotIn("outcome", envelope["record"])
            self.assertEqual(ledger.verify_integrity(), 1)

    def test_append_economic_binds_goal_provenance_and_restart_verifies(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            goal = self._economic_goal()
            record = self._economic_record(decision_id="economic-decision-1")

            ledger = JsonlDecisionLedger(path)
            ledger.append_economic(record, goal)

            restarted = JsonlDecisionLedger(path)
            restored = restarted.verified_economic_decision(record.decision_id, goal)
            self.assertEqual(restored.decision_id, record.decision_id)
            self.assertEqual(restored.payload["x"], 1)
            evidence = restored.payload[ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY]
            self.assertEqual(evidence["goal_id"], goal.goal_id)
            self.assertEqual(evidence["revision"], goal.revision)
            self.assertEqual(evidence["bankroll_id"], goal.bankroll_id)
            self.assertEqual(restarted.verify_integrity(), 1)

    def test_economic_restart_readback_fails_closed_when_binding_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            goal = self._economic_goal()
            record = self._economic_record(decision_id="generic-not-economic")
            JsonlDecisionLedger(path).append(record)

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "missing EconomicGoal provenance",
            ):
                JsonlDecisionLedger(path).verified_economic_decision(
                    record.decision_id,
                    goal,
                )

    def test_economic_restart_readback_rejects_goal_revision_rebinding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            goal = self._economic_goal()
            record = self._economic_record(decision_id="economic-revision-bound")
            JsonlDecisionLedger(path).append_economic(record, goal)

            changed = replace(goal, revision=2)
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "provenance mismatch: provenance revision mismatch",
            ):
                JsonlDecisionLedger(path).verified_economic_decision(
                    record.decision_id,
                    changed,
                )

    def test_append_economic_rejects_caller_supplied_provenance(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            goal = self._economic_goal()
            record = DecisionRecord(
                "run-1",
                "agent",
                "2026-01-01T00:00:00+00:00",
                "PROPOSE_STAKE",
                {
                    "stake": "1.00",
                    ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY: {"spoofed": True},
                },
                "ctx",
                decision_id="caller-spoof",
                decision_kind=ECONOMIC_DECISION_KIND,
            )

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "must be derived, not caller supplied",
            ):
                JsonlDecisionLedger(path).append_economic(record, goal)
            self.assertFalse(path.exists())

    def test_verify_integrity_rejects_record_tamper_even_when_file_is_stable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            ledger.append(self._record())
            envelope = json.loads(path.read_text(encoding="utf-8"))
            envelope["record"]["payload"]["x"] = 2
            path.write_text(json.dumps(envelope, sort_keys=True) + "\n", encoding="utf-8")

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "SHA-256 mismatch at line 1",
            ):
                ledger.verify_integrity()

    def test_verify_integrity_rejects_duplicate_decision_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            record = self._record(decision_id="duplicate-id")
            ledger.append(record)
            ledger.append(record)

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "duplicate decision_id at line 2",
            ):
                ledger.verify_integrity()

    def test_verify_integrity_rejects_unterminated_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            ledger.append(self._record())
            path.write_bytes(path.read_bytes().removesuffix(b"\n"))

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "unterminated final record",
            ):
                ledger.verify_integrity()

    def test_append_rejects_non_finite_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            record = DecisionRecord(
                "run-1",
                "agent",
                "2026-01-01T00:00:00+00:00",
                "OBSERVE",
                {"probability": math.nan},
                "ctx",
            )

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "non-finite",
            ):
                ledger.append(record)
            self.assertFalse(path.exists())

    def test_append_rejects_non_string_mapping_keys_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            record = DecisionRecord(
                "run-1",
                "agent",
                "2026-01-01T00:00:00+00:00",
                "OBSERVE",
                {1: "numeric", "1": "string"},
                "ctx",
            )

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "object keys at payload must be strings",
            ):
                ledger.append(record)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()

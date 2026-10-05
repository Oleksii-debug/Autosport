import hashlib
import json
import math
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from autosport.decision_ledger import (
    ECONOMIC_DECISION_KIND,
    ECONOMIC_GOAL_PROVENANCE_PAYLOAD_KEY,
    MATERIAL_ACTION_ID_PAYLOAD_KEY,
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

    def test_first_append_establishes_path_durability_before_record_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            original_sync = ledger._sync_parent_directory
            calls: list[bool] = []

            def observed_sync() -> None:
                calls.append(path.exists())
                original_sync()

            with patch.object(
                ledger,
                "_sync_parent_directory",
                side_effect=observed_sync,
            ):
                ledger.append(self._record(decision_id="durable-first"))

            self.assertEqual(calls, [True])
            self.assertEqual(ledger.verify_integrity(), 1)

    def test_first_append_directory_durability_failure_writes_no_record_and_releases_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)

            with patch.object(
                ledger,
                "_sync_parent_directory",
                side_effect=DecisionLedgerIntegrityError(
                    "Decision Ledger parent-directory durability barrier failed"
                ),
            ):
                with self.assertRaisesRegex(
                    DecisionLedgerIntegrityError,
                    "parent-directory durability barrier failed",
                ):
                    ledger.append(self._record(decision_id="not-published"))

            self.assertTrue(path.exists())
            self.assertEqual(path.read_bytes(), b"")
            lock_path = path.resolve(strict=False).with_name(path.name + ".writer.lock")
            self.assertTrue(lock_path.exists())

            ledger.append(self._record(decision_id="published-after-retry"))
            self.assertEqual(ledger.verify_integrity(), 1)

    def test_verified_snapshot_if_exists_is_empty_before_first_creation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)

            snapshot = ledger.verified_snapshot_if_exists()

            self.assertEqual(snapshot.payload, b"")
            self.assertEqual(snapshot.record_count, 0)
            self.assertFalse(path.exists())

    def test_verified_snapshot_if_exists_rejects_externally_appeared_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            path.write_text("", encoding="utf-8")

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "appeared outside this persistence authority",
            ):
                ledger.verified_snapshot_if_exists()

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

    def test_append_economic_rejects_duplicate_material_action_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            goal = self._economic_goal()
            ledger = JsonlDecisionLedger(path)
            first = replace(
                self._economic_record(decision_id="decision-a"),
                payload={
                    "x": 1,
                    MATERIAL_ACTION_ID_PAYLOAD_KEY: "material-action-1",
                },
            )
            second = replace(
                self._economic_record(decision_id="decision-b"),
                payload={
                    "x": 2,
                    MATERIAL_ACTION_ID_PAYLOAD_KEY: "material-action-1",
                },
            )

            ledger.append_economic(first, goal)
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "material_action_id already exists",
            ):
                ledger.append_economic(second, goal)

            records = ledger.verified_records()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].decision_id, "decision-a")

    def test_append_economic_rejects_invalid_material_action_identity_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            record = replace(
                self._economic_record(decision_id="invalid-material-action"),
                payload={"x": 1, MATERIAL_ACTION_ID_PAYLOAD_KEY: "   "},
            )

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "material_action_id is invalid",
            ):
                JsonlDecisionLedger(path).append_economic(
                    record,
                    self._economic_goal(),
                )

            self.assertFalse(path.exists())

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
            record = self._economic_record(decision_id="economic-missing-binding")
            persisted = record.to_dict()
            canonical = json.dumps(
                persisted,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            envelope = json.dumps(
                {
                    "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                    "record": persisted,
                },
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
            path.write_text(envelope + "\n", encoding="utf-8")

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

    def test_append_rejects_duplicate_decision_identity_without_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            record = self._record(decision_id="duplicate-id")
            ledger.append(record)

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "decision_id already exists",
            ):
                ledger.append(record)

            self.assertEqual(ledger.verify_integrity(), 1)
            self.assertEqual(
                [item.decision_id for item in ledger.verified_records()],
                ["duplicate-id"],
            )

    def test_symlinked_parent_aliases_share_one_writer_lock_namespace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            real = root / "real"
            alias = root / "alias"
            real.mkdir()
            try:
                alias.symlink_to(real, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks are unavailable")

            first = JsonlDecisionLedger(real / "decisions.jsonl")
            second = JsonlDecisionLedger(alias / "decisions.jsonl")
            self.assertEqual(
                first._writer_lock_path_authority,
                second._writer_lock_path_authority,
            )
            with first._writer_guard():
                with self.assertRaisesRegex(
                    DecisionLedgerIntegrityError,
                    "transaction authority is unavailable",
                ):
                    second.assert_transaction_authority()

    def test_transaction_authority_fails_closed_while_writer_lock_is_owned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            first = JsonlDecisionLedger(path)
            second = JsonlDecisionLedger(path)

            with first._writer_guard():
                with self.assertRaisesRegex(
                    DecisionLedgerIntegrityError,
                    "transaction authority is unavailable",
                ):
                    second.assert_transaction_authority()

            second.assert_transaction_authority()

    def test_abandoned_persistent_sidecar_does_not_brick_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            first = JsonlDecisionLedger(path)
            first.append(self._record(decision_id="first"))
            self.assertTrue(first._writer_lock_path_authority.exists())

            restarted = JsonlDecisionLedger(path)
            restarted.assert_transaction_authority()
            restarted.append(self._record(decision_id="second"))

            self.assertEqual(restarted.verify_integrity(), 2)

    def test_writer_lock_replacement_after_open_fails_before_append(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            ledger.append(self._record(decision_id="first"))
            lock_path = ledger._writer_lock_path_authority
            replacement = root / "replacement-lock"
            replacement.write_bytes(b"replacement")
            real_open = os.open
            injected = False

            def racing_open(target, flags, mode=0o777):
                nonlocal injected
                fd = real_open(target, flags, mode)
                if Path(target) == lock_path and not injected:
                    injected = True
                    lock_path.unlink()
                    replacement.replace(lock_path)
                return fd

            with patch("autosport.decision_ledger.os.open", side_effect=racing_open):
                with self.assertRaisesRegex(
                    DecisionLedgerIntegrityError,
                    "writer-lock path changed during acquisition",
                ):
                    ledger.append(self._record(decision_id="blocked"))

            self.assertTrue(injected)
            self.assertEqual(lock_path.read_bytes(), b"replacement")
            self.assertEqual(
                [item.decision_id for item in JsonlDecisionLedger(path).verified_records()],
                ["first"],
            )

    def test_writer_lock_replacement_after_os_lock_fails_final_identity_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            ledger.append(self._record(decision_id="first"))
            lock_path = ledger._writer_lock_path_authority
            replacement = root / "replacement-lock"
            replacement.write_bytes(b"replacement")
            from autosport import decision_ledger as decision_ledger_module

            original = decision_ledger_module._DecisionLedgerPathLock._assert_open_path_identity
            calls = 0

            def racing_identity(lock, handle):
                nonlocal calls
                calls += 1
                if calls == 2:
                    lock_path.unlink()
                    replacement.replace(lock_path)
                return original(lock, handle)

            with patch.object(
                decision_ledger_module._DecisionLedgerPathLock,
                "_assert_open_path_identity",
                autospec=True,
                side_effect=racing_identity,
            ):
                with self.assertRaisesRegex(
                    DecisionLedgerIntegrityError,
                    "writer-lock path changed during acquisition",
                ):
                    ledger.append(self._record(decision_id="blocked"))

            self.assertGreaterEqual(calls, 2)
            self.assertEqual(lock_path.read_bytes(), b"replacement")
            self.assertEqual(
                [item.decision_id for item in JsonlDecisionLedger(path).verified_records()],
                ["first"],
            )

    def test_two_ledger_instances_cannot_append_same_decision_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            first = JsonlDecisionLedger(path)
            second = JsonlDecisionLedger(path)
            record = self._record(decision_id="shared-id")
            first.append(record)

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "decision_id already exists",
            ):
                second.append(record)

            self.assertEqual(first.verify_integrity(), 1)

    def test_verified_snapshot_rejects_pathname_replacement_after_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            ledger.append(self._record(decision_id="original"))
            original_bytes = path.read_bytes()
            path.unlink()
            path.write_bytes(original_bytes)

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "file identity changed",
            ):
                ledger.verified_snapshot()

    def test_append_rejects_pathname_replacement_after_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            ledger.append(self._record(decision_id="original"))
            original_bytes = path.read_bytes()
            path.unlink()
            path.write_bytes(original_bytes)

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "file identity changed",
            ):
                ledger.append(self._record(decision_id="must-not-append"))

            restarted = JsonlDecisionLedger(path)
            self.assertEqual(restarted.verify_integrity(), 1)

    def test_persistent_unowned_writer_sidecar_does_not_block_verified_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            ledger.append(self._record(decision_id="stable"))
            self.assertTrue(ledger._writer_lock_path_authority.exists())

            snapshot = ledger.verified_snapshot()

            self.assertEqual(snapshot.record_count, 1)

    def test_integrity_rejects_malformed_material_action_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            record = self._record(decision_id="malformed-material")
            persisted = record.to_dict()
            persisted["payload"][MATERIAL_ACTION_ID_PAYLOAD_KEY] = "  bad  "
            canonical = json.dumps(
                persisted,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            envelope = json.dumps(
                {
                    "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                    "record": persisted,
                },
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
            path.write_text(envelope + "\n", encoding="utf-8")

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "material_action_id is invalid",
            ):
                JsonlDecisionLedger(path).verify_integrity()

    def test_duplicate_rejection_releases_writer_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            record = self._record(decision_id="duplicate-release")
            ledger.append(record)

            with self.assertRaises(DecisionLedgerIntegrityError):
                ledger.append(record)

            lock_path = path.resolve(strict=False).with_name(path.name + ".writer.lock")
            self.assertTrue(lock_path.exists())
            ledger.assert_transaction_authority()
            ledger.append(self._record(decision_id="next"))
            self.assertEqual(ledger.verify_integrity(), 2)

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



    def test_verified_snapshot_rejects_path_authority_rebinding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            ledger.append(self._record())
            ledger.path = Path(tmp) / "alternate-decisions.jsonl"

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "persistence authority changed",
            ):
                ledger.verified_snapshot()

    def test_append_rejects_path_authority_rebinding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            ledger.path = Path(tmp) / "alternate-decisions.jsonl"

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "persistence authority changed",
            ):
                ledger.append(self._record())

            self.assertFalse((Path(tmp) / "alternate-decisions.jsonl").exists())

    def test_append_economic_rejects_path_authority_rebinding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            ledger.path = Path(tmp) / "alternate-decisions.jsonl"

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "persistence authority changed",
            ):
                ledger.append_economic(
                    self._economic_record(decision_id="economic-path-drift"),
                    self._economic_goal(),
                )

            self.assertFalse((Path(tmp) / "alternate-decisions.jsonl").exists())


if __name__ == "__main__":
    unittest.main()

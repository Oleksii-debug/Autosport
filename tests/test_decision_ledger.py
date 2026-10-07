import hashlib
import json
import math
import os
import tempfile
import threading
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

    def test_decision_id_rejects_noncanonical_spelling_at_construction(self):
        class _TrapStr(str):
            def strip(self, *args: object, **kwargs: object) -> str:
                raise AssertionError("decision identity must reject str subclass before dispatch")

        for decision_id in (
            " decision-1",
            "decision-1 ",
            "decision\x00forged",
            "decision\x1fforged",
            _TrapStr("decision-1"),
            "decision-\ud800",
        ):
            with self.subTest(decision_id=repr(decision_id)):
                with self.assertRaisesRegex(
                    DecisionLedgerIntegrityError,
                    "decision_id is invalid",
                ):
                    self._record(decision_id=decision_id)

    def test_forged_padded_decision_id_fails_restart_integrity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            record = self._record(decision_id="decision-1").to_dict()
            record["decision_id"] = " decision-1 "
            canonical = json.dumps(
                record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            envelope = json.dumps(
                {
                    "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
                    "record": record,
                },
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            )
            path.write_text(envelope + "\n", encoding="utf-8")

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "decision_id is invalid at line 1",
            ):
                JsonlDecisionLedger(path).verify_integrity()

    def test_economic_decision_lookup_rejects_noncanonical_decision_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            goal = self._economic_goal()
            record = self._economic_record(decision_id="decision-1")
            JsonlDecisionLedger(path).append_economic(record, goal)

            for decision_id in (" decision-1", "decision-1 ", "decision\x00forged"):
                with self.subTest(decision_id=repr(decision_id)):
                    with self.assertRaisesRegex(
                        ValueError,
                        "decision_id must be exact canonical text",
                    ):
                        JsonlDecisionLedger(path).verified_economic_decision(
                            decision_id,
                            goal,
                        )

    def test_append_rejects_duplicate_decision_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            record = self._record(decision_id="duplicate-id")
            ledger.append(record)

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "already contains decision_id",
            ):
                ledger.append(record)

            self.assertEqual(ledger.verify_integrity(), 1)

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


    def test_material_action_id_requires_exact_canonical_text(self):
        class HostileMaterialActionId(str):
            pass

        goal = self._economic_goal()
        invalid_ids = (
            " padded-action",
            "padded-action ",
            "line\nbreak",
            "nul\x00action",
            HostileMaterialActionId("subclass-action"),
        )
        for material_action_id in invalid_ids:
            with self.subTest(material_action_id=repr(material_action_id)):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "decisions.jsonl"
                    record = DecisionRecord(
                        "run-1",
                        "agent",
                        "2026-01-01T00:00:00+00:00",
                        "PROPOSE_STAKE",
                        {
                            "x": 1,
                            "material_action_id": material_action_id,
                        },
                        "ctx",
                        decision_id="invalid-material-action",
                        decision_kind=ECONOMIC_DECISION_KIND,
                    )
                    with self.assertRaisesRegex(
                        DecisionLedgerIntegrityError,
                        "material_action_id is invalid",
                    ):
                        JsonlDecisionLedger(path).append_economic(record, goal)
                    self.assertFalse(path.exists())

    def test_material_action_id_requires_economic_record(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            record = DecisionRecord(
                "run-1",
                "agent",
                "2026-01-01T00:00:00+00:00",
                "OBSERVE",
                {"x": 1, "material_action_id": "paper-action-1"},
                "ctx",
                decision_id="general-material-action",
            )
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "attached to a non-economic decision",
            ):
                JsonlDecisionLedger(path).append(record)
            self.assertFalse(path.exists())

    def test_verified_material_action_lookup_rejects_noncanonical_query(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            goal = self._economic_goal()
            record = DecisionRecord(
                "run-1",
                "agent",
                "2026-01-01T00:00:00+00:00",
                "PROPOSE_STAKE",
                {"x": 1, "material_action_id": "paper-action-lookup"},
                "ctx",
                decision_id="lookup-action",
                decision_kind=ECONOMIC_DECISION_KIND,
            )
            ledger.append_economic(record, goal)

            for query in (" paper-action-lookup", "paper-action-lookup\n"):
                with self.subTest(query=repr(query)):
                    with self.assertRaisesRegex(
                        ValueError,
                        "exact canonical text",
                    ):
                        ledger.verified_economic_decision_for_material_action(
                            query,
                            goal,
                        )

    def test_verify_integrity_rejects_duplicate_material_action_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            goal = self._economic_goal()
            first = DecisionRecord(
                "run-1",
                "agent",
                "2026-01-01T00:00:00+00:00",
                "PROPOSE_STAKE",
                {"x": 1, "material_action_id": "paper-action-duplicate"},
                "ctx",
                decision_id="material-action-first",
                decision_kind=ECONOMIC_DECISION_KIND,
            )
            ledger.append_economic(first, goal)

            first_envelope = json.loads(path.read_text(encoding="utf-8"))
            second_record = dict(first_envelope["record"])
            second_record["decision_id"] = "material-action-second"
            canonical = json.dumps(
                second_record,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            second_envelope = {
                "sha256": hashlib.sha256(
                    canonical.encode("utf-8")
                ).hexdigest(),
                "record": second_record,
            }
            path.write_text(
                json.dumps(
                    first_envelope,
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                )
                + "\n"
                + json.dumps(
                    second_envelope,
                    ensure_ascii=False,
                    sort_keys=True,
                    allow_nan=False,
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "duplicate material_action_id at line 2",
            ):
                ledger.verify_integrity()

    def test_valid_material_action_id_round_trips_exactly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            goal = self._economic_goal()
            record = DecisionRecord(
                "run-1",
                "agent",
                "2026-01-01T00:00:00+00:00",
                "PROPOSE_STAKE",
                {"x": 1, "material_action_id": "paper-action-valid"},
                "ctx",
                decision_id="material-action-valid",
                decision_kind=ECONOMIC_DECISION_KIND,
            )
            ledger.append_economic(record, goal)

            restored = ledger.verified_economic_decision_for_material_action(
                "paper-action-valid",
                goal,
            )
            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(restored.decision_id, "material-action-valid")
            self.assertEqual(
                restored.payload["material_action_id"],
                "paper-action-valid",
            )


    def test_append_economic_rejects_sequential_duplicate_material_action_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            goal = self._economic_goal()
            first = DecisionRecord(
                "run-1",
                "agent",
                "2026-01-01T00:00:00+00:00",
                "PROPOSE_STAKE",
                {"x": 1, "material_action_id": "paper-action-retry"},
                "ctx",
                decision_id="paper-action-retry-first",
                decision_kind=ECONOMIC_DECISION_KIND,
            )
            second = replace(
                first,
                decision_id="paper-action-retry-second",
            )
            ledger.append_economic(first, goal)

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "already contains material_action_id",
            ):
                ledger.append_economic(second, goal)

            self.assertEqual(ledger.verify_integrity(), 1)
            restored = ledger.verified_economic_decision_for_material_action(
                "paper-action-retry",
                goal,
            )
            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(
                restored.decision_id,
                "paper-action-retry-first",
            )

    def test_hardlink_alias_rejects_append_and_verified_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            alias = Path(tmp) / "decisions-alias.jsonl"
            ledger = JsonlDecisionLedger(path)
            ledger.append(self._record(decision_id="hardlink-first"))
            try:
                os.link(path, alias)
            except OSError as exc:
                self.skipTest(f"hard links are unavailable on this test filesystem: {exc}")

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "must not have hard-link aliases",
            ):
                JsonlDecisionLedger(alias).append(
                    self._record(decision_id="hardlink-second")
                )
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "must not have hard-link aliases",
            ):
                ledger.verify_integrity()

            self.assertEqual(path.read_bytes(), alias.read_bytes())
            self.assertEqual(path.read_bytes().count(b"\n"), 1)

    def test_symlink_alias_converges_on_canonical_ledger_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            alias = Path(tmp) / "decisions-link.jsonl"
            goal = self._economic_goal()
            first = DecisionRecord(
                "run-1",
                "agent",
                "2026-01-01T00:00:00+00:00",
                "PROPOSE_STAKE",
                {"x": 1, "material_action_id": "paper-action-symlink"},
                "ctx",
                decision_id="symlink-first",
                decision_kind=ECONOMIC_DECISION_KIND,
            )
            JsonlDecisionLedger(path).append_economic(first, goal)
            try:
                alias.symlink_to(path)
            except OSError as exc:
                self.skipTest(f"symlinks are unavailable on this test filesystem: {exc}")

            duplicate = replace(first, decision_id="symlink-second")
            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "already contains material_action_id",
            ):
                JsonlDecisionLedger(alias).append_economic(duplicate, goal)

            self.assertEqual(JsonlDecisionLedger(path).verify_integrity(), 1)

    def test_concurrent_same_path_retry_appends_material_action_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            goal = self._economic_goal()
            first = DecisionRecord(
                "run-1",
                "agent",
                "2026-01-01T00:00:00+00:00",
                "PROPOSE_STAKE",
                {"x": 1, "material_action_id": "paper-action-concurrent"},
                "ctx",
                decision_id="concurrent-first",
                decision_kind=ECONOMIC_DECISION_KIND,
            )
            second = replace(first, decision_id="concurrent-second")
            barrier = threading.Barrier(3)
            outcomes: list[tuple[str, object]] = []
            outcome_lock = threading.Lock()

            def append(record: DecisionRecord) -> None:
                barrier.wait()
                try:
                    result: object = JsonlDecisionLedger(path).append_economic(
                        record,
                        goal,
                    )
                except BaseException as exc:
                    result = exc
                with outcome_lock:
                    outcomes.append((record.decision_id, result))

            workers = [
                threading.Thread(target=append, args=(first,)),
                threading.Thread(target=append, args=(second,)),
            ]
            for worker in workers:
                worker.start()
            barrier.wait()
            for worker in workers:
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())

            successes = [
                result for _decision_id, result in outcomes if isinstance(result, str)
            ]
            failures = [
                result
                for _decision_id, result in outcomes
                if isinstance(result, DecisionLedgerIntegrityError)
            ]
            self.assertEqual(len(successes), 1)
            self.assertEqual(len(failures), 1)
            self.assertRegex(str(failures[0]), "already contains material_action_id")
            self.assertEqual(JsonlDecisionLedger(path).verify_integrity(), 1)

    def test_verified_read_rejects_oversized_ledger_before_decode(self):
        import autosport.decision_ledger as decision_ledger_runtime

        original_limit = decision_ledger_runtime._MAX_DECISION_LEDGER_BYTES
        try:
            decision_ledger_runtime._MAX_DECISION_LEDGER_BYTES = 128
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "decisions.jsonl"
                with path.open("wb") as handle:
                    handle.seek(129)
                    handle.write(b"\n")

                with self.assertRaisesRegex(
                    DecisionLedgerIntegrityError,
                    "exceeds durable resource limit",
                ):
                    JsonlDecisionLedger(path).verify_integrity()
        finally:
            decision_ledger_runtime._MAX_DECISION_LEDGER_BYTES = original_limit

    def test_append_rejects_payload_that_would_exceed_ledger_resource_limit(self):
        import autosport.decision_ledger as decision_ledger_runtime

        original_limit = decision_ledger_runtime._MAX_DECISION_LEDGER_BYTES
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "decisions.jsonl"
                ledger = JsonlDecisionLedger(path)
                ledger.append(self._record(decision_id="resource-first"))
                existing_size = path.stat().st_size
                decision_ledger_runtime._MAX_DECISION_LEDGER_BYTES = (
                    existing_size + 1
                )

                with self.assertRaisesRegex(
                    DecisionLedgerIntegrityError,
                    "append exceeds durable resource limit",
                ):
                    ledger.append(self._record(decision_id="resource-second"))

                self.assertEqual(path.stat().st_size, existing_size)
                self.assertEqual(ledger.verify_integrity(), 1)
        finally:
            decision_ledger_runtime._MAX_DECISION_LEDGER_BYTES = original_limit

    def test_append_refuses_to_extend_corrupt_existing_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "decisions.jsonl"
            ledger = JsonlDecisionLedger(path)
            ledger.append(self._record(decision_id="first"))
            envelope = json.loads(path.read_text(encoding="utf-8"))
            envelope["record"]["payload"]["x"] = 99
            path.write_text(
                json.dumps(envelope, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            corrupt_bytes = path.read_bytes()

            with self.assertRaisesRegex(
                DecisionLedgerIntegrityError,
                "SHA-256 mismatch at line 1",
            ):
                ledger.append(self._record(decision_id="second"))

            self.assertEqual(path.read_bytes(), corrupt_bytes)


if __name__ == "__main__":
    unittest.main()

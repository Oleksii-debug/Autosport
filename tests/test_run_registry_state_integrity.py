import json
import tempfile
import unittest
from pathlib import Path

from autosport.run_registry import ReconciliationError, RepeatedExperimentError, RunRegistry


class RunRegistryStateIntegrityTests(unittest.TestCase):
    @staticmethod
    def _new_registry(root: Path) -> tuple[RunRegistry, Path]:
        path = root / "run_registry.json"
        return RunRegistry(path), path

    @staticmethod
    def _begin(
        registry: RunRegistry,
        *,
        run_id: str = "run-1",
        transaction_evidence: bool = False,
        allow_repeat: bool = False,
    ) -> str:
        kwargs = {}
        if transaction_evidence:
            kwargs = {
                "base_paper_book_sha256": "c" * 64,
                "base_decision_ledger_sha256": "d" * 64,
            }
        return registry.begin(
            "a" * 64,
            "b" * 64,
            "strategy",
            run_id,
            allow_repeat=allow_repeat,
            **kwargs,
        )

    def test_valid_completed_aborted_and_in_progress_entries_reopen(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry, path = self._new_registry(root)
            completed = self._begin(registry, transaction_evidence=True)
            registry.complete(
                completed,
                str(root / "run-run-1.json"),
                paper_book_sha256="e" * 64,
                decision_ledger_sha256="f" * 64,
            )
            self.assertEqual(RunRegistry(path).get(completed)["status"], "completed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry, path = self._new_registry(root)
            aborted = self._begin(registry, transaction_evidence=True)
            registry.abort_uncommitted(
                aborted,
                reason="precommit failed without economic mutation",
                paper_book_sha256="c" * 64,
                decision_ledger_sha256="d" * 64,
            )
            self.assertEqual(RunRegistry(path).get(aborted)["status"], "aborted")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry, path = self._new_registry(root)
            in_progress = self._begin(registry, transaction_evidence=True)
            self.assertEqual(RunRegistry(path).get(in_progress)["status"], "in_progress")

    def test_reused_repeat_run_id_cannot_overwrite_completed_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry, path = self._new_registry(root)
            base = self._begin(registry, transaction_evidence=True)
            registry.complete(
                base,
                str(root / "run-run-1.json"),
                paper_book_sha256="e" * 64,
                decision_ledger_sha256="f" * 64,
            )
            repeat = self._begin(
                registry,
                run_id="repeat-1",
                transaction_evidence=True,
                allow_repeat=True,
            )
            registry.complete(
                repeat,
                str(root / "run-repeat-1.json"),
                paper_book_sha256="1" * 64,
                decision_ledger_sha256="2" * 64,
            )
            before = path.read_bytes()

            with self.assertRaisesRegex(RepeatedExperimentError, "durable history"):
                self._begin(
                    registry,
                    run_id="repeat-1",
                    transaction_evidence=True,
                    allow_repeat=True,
                )

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(registry.get(repeat)["status"], "completed")

    def test_reused_retry_run_id_cannot_overwrite_aborted_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry, path = self._new_registry(root)
            base = self._begin(registry, run_id="base", transaction_evidence=True)
            registry.abort_uncommitted(
                base,
                reason="base aborted",
                paper_book_sha256="c" * 64,
                decision_ledger_sha256="d" * 64,
            )
            retry = self._begin(registry, run_id="retry-1", transaction_evidence=True)
            registry.abort_uncommitted(
                retry,
                reason="retry aborted",
                paper_book_sha256="c" * 64,
                decision_ledger_sha256="d" * 64,
            )
            before = path.read_bytes()

            with self.assertRaisesRegex(RepeatedExperimentError, "durable history"):
                self._begin(registry, run_id="retry-1", transaction_evidence=True)

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(registry.get(retry)["status"], "aborted")

    def test_transaction_aware_completed_entry_requires_terminal_evidence(self):
        for missing_field in ("result_path", "paper_book_sha256", "decision_ledger_sha256"):
            with self.subTest(missing_field=missing_field):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    registry, path = self._new_registry(root)
                    key = self._begin(registry, transaction_evidence=True)
                    registry.complete(
                        key,
                        str(root / "run-run-1.json"),
                        paper_book_sha256="e" * 64,
                        decision_ledger_sha256="f" * 64,
                    )
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    raw["runs"][key].pop(missing_field)
                    path.write_text(json.dumps(raw), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "terminal economic evidence"):
                        RunRegistry(path)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry, path = self._new_registry(root)
            key = self._begin(registry, transaction_evidence=True)
            registry.complete(
                key,
                str(root / "run-run-1.json"),
                paper_book_sha256="e" * 64,
                decision_ledger_sha256="f" * 64,
            )
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["runs"][key]["result_path"] = ""
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "result_path evidence"):
                RunRegistry(path)

    def test_constructor_rejects_non_object_bad_schema_and_root_drift(self):
        payloads = (
            "[]",
            '{"schema_version":true,"runs":{}}',
            '{"schema_version":1.0,"runs":{}}',
            '{"schema_version":"1","runs":{}}',
            '{"schema_version":1,"runs":{},"unexpected":null}',
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "run_registry.json"
                    path.write_text(payload, encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "invalid run registry"):
                        RunRegistry(path)

    def test_duplicate_keys_and_nonfinite_json_fail_closed(self):
        payloads = (
            '{"schema_version":1,"schema_version":1,"runs":{}}',
            '{"schema_version":1,"runs":{},"evidence":NaN}',
            '{"schema_version":1,"runs":{},"evidence":Infinity}',
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "run_registry.json"
                    path.write_text(payload, encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "invalid run registry"):
                        RunRegistry(path)

    def test_entry_schema_drift_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry, path = self._new_registry(root)
            key = self._begin(registry)
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["runs"][key]["unexpected"] = "value"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid run entry fields"):
                RunRegistry(path)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry, path = self._new_registry(root)
            key = self._begin(registry)
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["runs"][key].pop("strategy_id")
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid run entry fields"):
                RunRegistry(path)

    def test_persisted_optional_hashes_require_canonical_sha256(self):
        fields = (
            "base_paper_book_sha256",
            "base_decision_ledger_sha256",
            "paper_book_sha256",
            "decision_ledger_sha256",
        )
        for field_name in fields:
            with self.subTest(field_name=field_name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    registry, path = self._new_registry(root)
                    key = self._begin(registry, transaction_evidence=True)
                    registry.complete(
                        key,
                        str(root / "run-run-1.json"),
                        paper_book_sha256="e" * 64,
                        decision_ledger_sha256="f" * 64,
                    )
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    raw["runs"][key][field_name] = "G" * 64
                    path.write_text(json.dumps(raw), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "invalid .*sha256"):
                        RunRegistry(path)

    def test_in_progress_entry_cannot_carry_final_state_evidence(self):
        for field_name, value in (
            ("result_path", "run-run-1.json"),
            ("paper_book_sha256", "e" * 64),
            ("decision_ledger_sha256", "f" * 64),
            ("abort_reason", "not actually aborted"),
            ("reconciled_from_summary", True),
        ):
            with self.subTest(field_name=field_name):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    registry, path = self._new_registry(root)
                    key = self._begin(registry, transaction_evidence=True)
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    raw["runs"][key][field_name] = value
                    path.write_text(json.dumps(raw), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "final-state evidence"):
                        RunRegistry(path)

    def test_completed_entry_cannot_carry_abort_or_false_reconciliation_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry, path = self._new_registry(root)
            key = self._begin(registry)
            registry.complete(key)
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["runs"][key]["abort_reason"] = "conflicting terminal state"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "abort evidence"):
                RunRegistry(path)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry, path = self._new_registry(root)
            key = self._begin(registry)
            registry.complete(key)
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["runs"][key]["reconciled_from_summary"] = False
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid reconciliation evidence"):
                RunRegistry(path)

    def test_aborted_entry_requires_matching_rollback_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry, path = self._new_registry(root)
            key = self._begin(registry, transaction_evidence=True)
            registry.abort_uncommitted(
                key,
                reason="rolled back",
                paper_book_sha256="c" * 64,
                decision_ledger_sha256="d" * 64,
            )
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["runs"][key]["paper_book_sha256"] = "e" * 64
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "does not match transaction base"):
                RunRegistry(path)

    def test_runtime_hash_validation_happens_before_registry_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry, path = self._new_registry(root)
            baseline = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "canonical SHA-256"):
                registry.begin(
                    "a" * 64,
                    "b" * 64,
                    "strategy",
                    "run-1",
                    base_paper_book_sha256="C" * 64,
                    base_decision_ledger_sha256="d" * 64,
                )
            self.assertEqual(path.read_bytes(), baseline)

            key = self._begin(registry)
            before_complete = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "canonical SHA-256"):
                registry.complete(key, paper_book_sha256="not-a-hash")
            self.assertEqual(path.read_bytes(), before_complete)
            self.assertEqual(registry.get(key)["status"], "in_progress")

    def test_reconciliation_summary_duplicate_and_nonfinite_json_fail_closed(self):
        payloads = (
            '{"schema_version":2,"schema_version":2}',
            '{"schema_version":2,"paper_book_sha256":NaN}',
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                with tempfile.TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    registry, _path = self._new_registry(root)
                    key = self._begin(registry)
                    summary_path = root / "run-run-1.json"
                    summary_path.write_text(payload, encoding="utf-8")
                    book_path = root / "paper_book.json"
                    book_path.write_text("{}\n", encoding="utf-8")

                    with self.assertRaisesRegex(ReconciliationError, "invalid JSON"):
                        registry.reconcile_completed_summary(key, summary_path, book_path)
                    self.assertEqual(registry.get(key)["status"], "in_progress")


if __name__ == "__main__":
    unittest.main()

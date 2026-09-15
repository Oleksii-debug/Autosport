import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.cli import run_repair_workspace
from autosport.dataset import load_dataset
from autosport.integrity import sha256_file
from autosport.recovery import reconcile_late_crashes
from autosport.run_registry import (
    ReconciliationError,
    RepeatedExperimentError,
    RunRegistry,
)
from autosport.run_transaction import RunTransaction
from autosport.session import AutosportSession
from autosport.workspace_lock import WorkspaceEconomicLock


def _hold_recovery_workspace_lock(workspace: str, ready, release) -> None:
    with WorkspaceEconomicLock(workspace):
        ready.set()
        if not release.wait(20):
            raise RuntimeError("test lock holder timed out waiting for release")


class RecoveryReconciliationTests(unittest.TestCase):
    def _create_late_crash(self, workspace: str):
        dataset = load_dataset(Path("examples/tt_demo"))
        session = AutosportSession(workspace, "10000")
        with patch.object(
            session.registry,
            "complete",
            side_effect=RuntimeError("simulated crash after durable summary"),
        ):
            with self.assertRaisesRegex(RuntimeError, "simulated crash"):
                session.run_dataset(dataset)
        unresolved = session.registry.in_progress()
        self.assertEqual(len(unresolved), 1)
        key, item = unresolved[0]
        summary_path = Path(workspace) / f"run-{item['run_id']}.json"
        self.assertTrue(summary_path.is_file())
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        self.assertEqual(summary["schema_version"], 2)
        self.assertEqual(summary["experiment_key"], key)
        self.assertEqual(summary["paper_book_sha256"], sha256_file(session.book_path))
        session.close()
        return dataset, key, item, summary_path

    def _start_lock_holder(self, root: Path):
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        release = context.Event()
        process = context.Process(
            target=_hold_recovery_workspace_lock,
            args=(str(root), ready, release),
        )
        process.start()
        self.assertTrue(ready.wait(20), "child process did not acquire workspace lock")
        return process, release

    def _stop_lock_holder(self, process, release) -> None:
        release.set()
        process.join(20)
        if process.is_alive():
            process.terminate()
            process.join(10)
            self.fail("child lock-holder process did not exit")
        self.assertEqual(process.exitcode, 0)

    def _symlink_or_skip(
        self,
        link: Path,
        target: Path,
        *,
        target_is_directory: bool = False,
    ) -> None:
        try:
            link.symlink_to(target, target_is_directory=target_is_directory)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")

    def test_late_crash_reconciles_without_replaying_economic_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            dataset, key, item, _summary = self._create_late_crash(tmp)
            root = Path(tmp)
            book_hash_before = sha256_file(root / "paper_book.json")
            report = reconcile_late_crashes(tmp)
            self.assertEqual(report.reconciled_keys, (key,))
            self.assertEqual(report.unresolved_without_summary, ())
            self.assertEqual(sha256_file(root / "paper_book.json"), book_hash_before)

            registry = RunRegistry(root / "run_registry.json")
            repaired = registry.get(key)
            self.assertEqual(repaired["status"], "completed")
            self.assertTrue(repaired["reconciled_from_summary"])
            self.assertEqual(repaired["paper_book_sha256"], book_hash_before)
            manifest_path = root / RunTransaction.ROOT_NAME / item["run_id"] / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "completed")

            restored = AutosportSession(tmp, "1")
            with self.assertRaises(RepeatedExperimentError):
                restored.run_dataset(dataset)
            restored.close()

    def test_second_crash_after_registry_reconciliation_is_recoverable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _dataset, key, item, _summary = self._create_late_crash(tmp)
            manifest_path = root / RunTransaction.ROOT_NAME / item["run_id"] / "manifest.json"
            book_hash_before = sha256_file(root / "paper_book.json")
            self.assertEqual(
                json.loads(manifest_path.read_text(encoding="utf-8"))["phase"],
                "canonical_committed",
            )

            with patch.object(
                RunTransaction,
                "mark_registry_completed",
                side_effect=OSError("simulated manifest finalization failure"),
            ):
                with self.assertRaisesRegex(
                    ReconciliationError,
                    "simulated manifest finalization failure",
                ):
                    reconcile_late_crashes(tmp)

            registry = RunRegistry(root / "run_registry.json")
            self.assertEqual(registry.get(key)["status"], "completed")
            self.assertEqual(
                json.loads(manifest_path.read_text(encoding="utf-8"))["phase"],
                "canonical_committed",
            )
            self.assertEqual(sha256_file(root / "paper_book.json"), book_hash_before)

            report = reconcile_late_crashes(tmp)
            self.assertEqual(report.reconciled_keys, (key,))
            self.assertEqual(registry.get(key)["status"], "completed")
            self.assertEqual(
                json.loads(manifest_path.read_text(encoding="utf-8"))["phase"],
                "completed",
            )
            self.assertEqual(sha256_file(root / "paper_book.json"), book_hash_before)

    def test_pre_manifest_crash_directory_aborts_idempotently(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = AutosportSession(root, "10000")
            session.close()

            registry = RunRegistry(root / "run_registry.json")
            book_hash = sha256_file(root / "paper_book.json")
            ledger_hash = sha256_file(root / "decisions.jsonl")
            key = registry.begin(
                "a" * 64,
                "b" * 64,
                "strategy",
                "run-before-manifest",
                base_paper_book_sha256=book_hash,
                base_decision_ledger_sha256=ledger_hash,
            )
            transaction_dir = root / RunTransaction.ROOT_NAME / "run-before-manifest"
            transaction_dir.mkdir(parents=True)
            self.assertFalse((transaction_dir / "manifest.json").exists())

            report = reconcile_late_crashes(root)
            self.assertEqual(report.aborted_uncommitted_keys, (key,))
            self.assertEqual(registry.get(key)["status"], "aborted")
            self.assertFalse(transaction_dir.exists())
            self.assertEqual(sha256_file(root / "paper_book.json"), book_hash)
            self.assertEqual(sha256_file(root / "decisions.jsonl"), ledger_hash)

            retry = reconcile_late_crashes(root)
            self.assertEqual(retry.reconciled_keys, ())
            self.assertEqual(retry.aborted_uncommitted_keys, ())
            self.assertEqual(retry.unresolved_without_summary, ())
            self.assertEqual(registry.get(key)["status"], "aborted")
            self.assertEqual(sha256_file(root / "paper_book.json"), book_hash)
            self.assertEqual(sha256_file(root / "decisions.jsonl"), ledger_hash)

    def test_completed_registry_with_precommitted_manifest_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _dataset, key, item, _summary = self._create_late_crash(tmp)
            reconcile_late_crashes(root)

            registry_path = root / "run_registry.json"
            book_hash = sha256_file(root / "paper_book.json")
            ledger_hash = sha256_file(root / "decisions.jsonl")
            registry_bytes = registry_path.read_bytes()
            manifest_path = root / RunTransaction.ROOT_NAME / item["run_id"] / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "completed")
            manifest["phase"] = "precommitted"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            with self.assertRaisesRegex(
                ReconciliationError,
                "completed registry is incompatible with transaction phase",
            ):
                reconcile_late_crashes(root)

            self.assertEqual(registry_path.read_bytes(), registry_bytes)
            self.assertEqual(RunRegistry(registry_path).get(key)["status"], "completed")
            self.assertEqual(sha256_file(root / "paper_book.json"), book_hash)
            self.assertEqual(sha256_file(root / "decisions.jsonl"), ledger_hash)

    def test_manifest_symlink_is_not_accepted_as_durable_transaction_truth(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _dataset, key, item, _summary = self._create_late_crash(tmp)
            manifest_path = root / RunTransaction.ROOT_NAME / item["run_id"] / "manifest.json"
            target = root / "captured-manifest.json"
            target.write_bytes(manifest_path.read_bytes())
            manifest_path.unlink()
            self._symlink_or_skip(manifest_path, target)

            with self.assertRaisesRegex(
                ReconciliationError,
                "transaction manifest path is not a regular file",
            ):
                reconcile_late_crashes(root)

            self.assertTrue(manifest_path.is_symlink())
            self.assertEqual(RunRegistry(root / "run_registry.json").get(key)["status"], "in_progress")

    def test_tampered_paper_book_prevents_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            _dataset, key, _item, _summary = self._create_late_crash(tmp)
            book_path = Path(tmp) / "paper_book.json"
            book_path.write_text(book_path.read_text(encoding="utf-8") + " ", encoding="utf-8")
            with self.assertRaisesRegex(ReconciliationError, "PaperBook SHA-256"):
                reconcile_late_crashes(tmp)
            self.assertEqual(RunRegistry(Path(tmp) / "run_registry.json").get(key)["status"], "in_progress")

    def test_summary_identity_mismatch_prevents_reconciliation(self):
        with tempfile.TemporaryDirectory() as tmp:
            _dataset, key, _item, summary_path = self._create_late_crash(tmp)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            summary["strategy_id"] = "tampered-strategy"
            summary_path.write_text(json.dumps(summary), encoding="utf-8")
            with self.assertRaisesRegex(ReconciliationError, "identity mismatch"):
                reconcile_late_crashes(tmp)
            self.assertEqual(RunRegistry(Path(tmp) / "run_registry.json").get(key)["status"], "in_progress")

    def test_missing_summary_remains_unresolved_and_cli_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
            key = registry.begin("a" * 64, "b" * 64, "strategy", "run-missing-summary")
            report = reconcile_late_crashes(root)
            self.assertEqual(report.reconciled_keys, ())
            self.assertEqual(report.unresolved_without_summary, (key,))
            self.assertEqual(run_repair_workspace(root), 4)
            self.assertEqual(registry.get(key)["status"], "in_progress")

    def test_empty_workspace_repair_is_noop_without_creating_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            report = reconcile_late_crashes(root)
            self.assertEqual(report.reconciled_keys, ())
            self.assertEqual(report.unresolved_without_summary, ())
            self.assertFalse((root / "run_registry.json").exists())
            self.assertEqual(run_repair_workspace(root), 0)

    def test_recovery_fails_closed_before_active_writer_creates_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertFalse((root / "run_registry.json").exists())
            process, release = self._start_lock_holder(root)
            try:
                self.assertFalse((root / "run_registry.json").exists())
                with self.assertRaisesRegex(
                    ReconciliationError,
                    "active economic writer",
                ):
                    reconcile_late_crashes(root)
                self.assertFalse((root / "run_registry.json").exists())
            finally:
                self._stop_lock_holder(process, release)

    def test_missing_registry_with_durable_history_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transaction_dir = root / ".run-transactions" / "run-1"
            transaction_dir.mkdir(parents=True)
            (transaction_dir / "manifest.json").write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(
                ReconciliationError,
                "run registry is missing while durable run history exists",
            ):
                reconcile_late_crashes(root)
            self.assertEqual(run_repair_workspace(root), 3)
            self.assertFalse((root / "run_registry.json").exists())

    def test_broken_registry_symlink_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry_path = root / "run_registry.json"
            self._symlink_or_skip(registry_path, root / "missing-registry-target.json")

            with self.assertRaisesRegex(
                ReconciliationError,
                "run registry path is not a regular file",
            ):
                reconcile_late_crashes(root)
            self.assertTrue(registry_path.is_symlink())
            self.assertFalse((root / "missing-registry-target.json").exists())
            self.assertEqual(run_repair_workspace(root), 3)

    def test_transaction_root_symlink_counts_as_durable_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transaction_root = root / ".run-transactions"
            self._symlink_or_skip(
                transaction_root,
                root / "missing-transaction-root",
                target_is_directory=True,
            )

            with self.assertRaisesRegex(
                ReconciliationError,
                "run registry is missing while durable run history exists",
            ):
                reconcile_late_crashes(root)
            self.assertTrue(transaction_root.is_symlink())
            self.assertFalse((root / "run_registry.json").exists())
            self.assertEqual(run_repair_workspace(root), 3)

    def test_non_file_registry_path_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "run_registry.json").mkdir()

            with self.assertRaisesRegex(ReconciliationError, "run registry path is not a regular file"):
                reconcile_late_crashes(root)
            self.assertEqual(run_repair_workspace(root), 3)

    def test_invalid_registry_json_is_controlled_recovery_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry_path = root / "run_registry.json"
            registry_path.write_text('{"schema_version":1,"runs":', encoding="utf-8")

            with self.assertRaisesRegex(ReconciliationError, "invalid run registry"):
                reconcile_late_crashes(root)
            self.assertEqual(run_repair_workspace(root), 3)
            self.assertEqual(
                registry_path.read_text(encoding="utf-8"),
                '{"schema_version":1,"runs":',
            )

    def test_workspace_file_is_controlled_recovery_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.write_text("not a directory\n", encoding="utf-8")

            with self.assertRaisesRegex(ReconciliationError, "workspace recovery failed"):
                reconcile_late_crashes(workspace)
            self.assertEqual(run_repair_workspace(workspace), 3)
            self.assertEqual(workspace.read_text(encoding="utf-8"), "not a directory\n")


if __name__ == "__main__":
    unittest.main()

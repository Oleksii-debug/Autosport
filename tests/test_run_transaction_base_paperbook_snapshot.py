import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.integrity import sha256_file
from autosport.paper import PaperBook
from autosport.run_registry import RunRegistry
from autosport.run_transaction import RunTransaction, RunTransactionError


class RunTransactionBasePaperBookSnapshotTests(unittest.TestCase):
    @staticmethod
    def _prepare_book(root: Path, bankroll: str = "10000") -> tuple[Path, bytes, str]:
        # Product startup publishes the canonical registry while the workspace is
        # still pristine. Once paper_book.json exists it is durable economic
        # history, so a missing registry must correctly fail closed.
        RunRegistry.initialize_pristine(root / "run_registry.json")
        book_path = root / "paper_book.json"
        PaperBook(bankroll).save(book_path)
        payload = book_path.read_bytes()
        return book_path, payload, sha256_file(book_path)

    @staticmethod
    def _begin_registry(
        root: Path,
        *,
        run_id: str,
        base_book_sha256: str,
        base_ledger_sha256: str = "c" * 64,
    ) -> str:
        registry = RunRegistry.initialize_pristine(root / "run_registry.json")
        return registry.begin(
            "a" * 64,
            "b" * 64,
            "baseline-v1",
            run_id,
            base_paper_book_sha256=base_book_sha256,
            base_decision_ledger_sha256=base_ledger_sha256,
        )

    @classmethod
    def _start(cls, root: Path, *, run_id: str = "base-snapshot-run") -> RunTransaction:
        _book_path, _payload, book_sha = cls._prepare_book(root)
        experiment_key = cls._begin_registry(
            root,
            run_id=run_id,
            base_book_sha256=book_sha,
        )
        return RunTransaction.start(
            root,
            run_id=run_id,
            experiment_key=experiment_key,
            market_sha256="a" * 64,
            results_sha256="b" * 64,
            strategy_id="baseline-v1",
            base_paper_book_sha256=book_sha,
            base_decision_ledger_sha256="c" * 64,
        )

    def test_retained_base_snapshot_survives_canonical_book_advance_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            book_path, expected_payload, expected_sha = self._prepare_book(root)
            experiment_key = self._begin_registry(
                root,
                run_id="retained-after-advance",
                base_book_sha256=expected_sha,
            )
            tx = RunTransaction.start(
                root,
                run_id="retained-after-advance",
                experiment_key=experiment_key,
                market_sha256="a" * 64,
                results_sha256="b" * 64,
                strategy_id="baseline-v1",
                base_paper_book_sha256=expected_sha,
                base_decision_ledger_sha256="c" * 64,
            )

            self.assertEqual(tx.base_book_snapshot_path.read_bytes(), expected_payload)

            # A later canonical PaperBook generation must not rewrite the retained
            # predecessor bytes used by run-capital-path evidence.
            PaperBook("12345").save(book_path)
            detached = RunTransaction(root, "retained-after-advance")
            snapshot = detached.verified_base_paper_book_snapshot()

            self.assertEqual(snapshot.payload, expected_payload)
            self.assertEqual(snapshot.sha256, expected_sha)
            self.assertNotEqual(book_path.read_bytes(), expected_payload)

    def test_start_rejects_stale_base_hash_before_creating_transaction_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._prepare_book(root)
            experiment_key = self._begin_registry(
                root,
                run_id="stale-base",
                base_book_sha256="0" * 64,
            )

            with self.assertRaisesRegex(
                RunTransactionError,
                "base PaperBook SHA-256 canonical hash is not the expected transaction state",
            ):
                RunTransaction.start(
                    root,
                    run_id="stale-base",
                    experiment_key=experiment_key,
                    market_sha256="a" * 64,
                    results_sha256="b" * 64,
                    strategy_id="baseline-v1",
                    base_paper_book_sha256="0" * 64,
                    base_decision_ledger_sha256="c" * 64,
                )

            self.assertFalse((root / RunTransaction.ROOT_NAME / "stale-base").exists())

    def test_manifest_and_sidecar_rebinding_cannot_override_registry_base_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx = self._start(root, run_id="registry-bound-base")

            PaperBook("7777").save(tx.base_book_snapshot_path)
            rebound_sha = sha256_file(tx.base_book_snapshot_path)
            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            manifest["base"]["paper_book_sha256"] = rebound_sha
            tx.manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            detached = RunTransaction(root, "registry-bound-base")
            with self.assertRaisesRegex(
                RunTransactionError,
                "immutable identity mismatch: base.paper_book_sha256",
            ):
                detached.verified_base_paper_book_snapshot()

    def test_retained_snapshot_tamper_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx = self._start(root, run_id="tampered-retained-base")

            PaperBook("9999").save(tx.base_book_snapshot_path)

            with self.assertRaisesRegex(
                RunTransactionError,
                "retained base PaperBook SHA-256 does not match transaction BASE",
            ):
                tx.verified_base_paper_book_snapshot()

    def test_missing_retained_snapshot_fails_closed_without_affecting_legacy_recovery_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            tx = self._start(root, run_id="missing-retained-base")
            tx.base_book_snapshot_path.unlink()

            detached = RunTransaction(root, "missing-retained-base")
            with self.assertRaisesRegex(
                RunTransactionError,
                "retained base PaperBook canonical file is missing",
            ):
                detached.verified_base_paper_book_snapshot()

            # The existing transaction manifest remains schema-compatible. The new
            # evidence accessor alone fails closed when historical bytes are absent.
            manifest = json.loads(detached.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], RunTransaction.SCHEMA_VERSION)
            self.assertEqual(manifest["phase"], "staging")

    def test_retention_failure_publishes_aborted_phase_before_propagating(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _book_path, _payload, book_sha = self._prepare_book(root)
            experiment_key = self._begin_registry(
                root,
                run_id="retention-failure",
                base_book_sha256=book_sha,
            )

            with patch.object(
                RunTransaction,
                "_atomic_write_bytes",
                side_effect=OSError("simulated retention failure"),
            ):
                with self.assertRaisesRegex(OSError, "simulated retention failure"):
                    RunTransaction.start(
                        root,
                        run_id="retention-failure",
                        experiment_key=experiment_key,
                        market_sha256="a" * 64,
                        results_sha256="b" * 64,
                        strategy_id="baseline-v1",
                        base_paper_book_sha256=book_sha,
                        base_decision_ledger_sha256="c" * 64,
                    )

            tx = RunTransaction(root, "retention-failure")
            manifest = json.loads(tx.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["phase"], "aborted")
            self.assertFalse(tx.base_book_snapshot_path.exists())


if __name__ == "__main__":
    unittest.main()

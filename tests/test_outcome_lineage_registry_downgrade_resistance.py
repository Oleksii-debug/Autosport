from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autosport.run_registry as run_registry_module
from autosport.outcome_trust import (
    OutcomeLineageBinding,
    TrustedOutcomeRevision,
    outcome_lineage_payload,
)
from autosport.run_registry import RunRegistry


class OutcomeLineageRegistryDowngradeResistanceTests(unittest.TestCase):
    source_identity = "official-results:test-fixture"
    record_id = "table-tennis-results:2026-01-01"

    @staticmethod
    def _sha(label: str) -> str:
        return hashlib.sha256(label.encode("utf-8")).hexdigest()

    def _binding(
        self,
        label: str,
        *,
        root_revision_id: str = "results-r1",
    ) -> OutcomeLineageBinding:
        revision = TrustedOutcomeRevision(
            revision=1,
            revision_id=root_revision_id,
            record_sha256=self._sha(label),
        )
        return OutcomeLineageBinding(
            source_identity=self.source_identity,
            record_id=self.record_id,
            root_revision_id=revision.revision_id,
            root_record_sha256=revision.record_sha256,
            revisions=(revision,),
        )

    @staticmethod
    def _write_bound_summary(
        root: Path,
        binding: OutcomeLineageBinding,
        *,
        run_id: str = "accepted-run",
        expected_hash_override: str | None = None,
    ) -> Path:
        market_sha256 = "a" * 64
        results_sha256 = "b" * 64
        paper_book_sha256 = "c" * 64
        decision_ledger_sha256 = "d" * 64
        experiment_key = hashlib.sha256(
            f"{market_sha256}|{results_sha256}|baseline-v1".encode("utf-8")
        ).hexdigest()
        summary_path = root / f"run-{run_id}.json"
        summary = {
            "schema_version": 2,
            "run_id": run_id,
            "experiment_key": experiment_key,
            "market_sha256": market_sha256,
            "sealed_results_sha256": results_sha256,
            "strategy_id": "baseline-v1",
            "real_money_execution": False,
            "paper_book_sha256": paper_book_sha256,
            "decision_ledger_sha256": decision_ledger_sha256,
            "transaction_schema_version": 1,
            "transaction_run_id": run_id,
            "outcome_lineage_trust": outcome_lineage_payload(binding),
        }
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        manifest_root = root / ".run-transactions" / run_id
        manifest_root.mkdir(parents=True)
        expected_summary_hash = hashlib.sha256(summary_path.read_bytes()).hexdigest()
        manifest = {
            "schema_version": 1,
            "phase": "completed",
            "run_id": run_id,
            "experiment_key": experiment_key,
            "market_sha256": market_sha256,
            "sealed_results_sha256": results_sha256,
            "strategy_id": "baseline-v1",
            "real_money_execution": False,
            "targets": {
                "paper_book": "paper_book.json",
                "decision_ledger": "decisions.jsonl",
                "summary": summary_path.name,
            },
            "new": {
                "paper_book_sha256": paper_book_sha256,
                "decision_ledger_sha256": decision_ledger_sha256,
                "summary_sha256": (
                    expected_hash_override
                    if expected_hash_override is not None
                    else expected_summary_hash
                ),
            },
        }
        (manifest_root / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return summary_path

    @staticmethod
    def _accept(registry: RunRegistry, binding: OutcomeLineageBinding) -> str:
        key = registry.begin(
            "a" * 64,
            "b" * 64,
            "baseline-v1",
            "accepted-run",
            outcome_lineage=binding,
        )
        registry.complete(key)
        return key

    def test_schema_two_workspace_cannot_be_rewritten_as_schema_one_after_durable_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
            accepted = self._binding("accepted-root")
            self._accept(registry, accepted)
            self._write_bound_summary(root, accepted)

            registry.path.write_text(
                json.dumps({"schema_version": 1, "runs": {}}, sort_keys=True),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "lineage-trust schema was downgraded"):
                RunRegistry(registry.path)

    def test_schema_downgrade_cannot_hide_behind_retained_legacy_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
            accepted = self._binding("accepted-root")
            self._accept(registry, accepted)
            self._write_bound_summary(root, accepted)

            legacy_key = registry.experiment_identity("c" * 64, "d" * 64, "legacy-v1")
            legacy_entry = {
                "base_identity": legacy_key,
                "run_id": "retained-legacy-run",
                "market_sha256": "c" * 64,
                "results_sha256": "d" * 64,
                "strategy_id": "legacy-v1",
                "status": "in_progress",
            }
            registry.path.write_text(
                json.dumps(
                    {"schema_version": 1, "runs": {legacy_key: legacy_entry}},
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "lineage-trust schema was downgraded"):
                RunRegistry(registry.path)

    def test_marker_removal_cannot_hide_behind_retained_legacy_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
            accepted = self._binding("accepted-root")
            self._accept(registry, accepted)
            summary_path = self._write_bound_summary(root, accepted)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            summary.pop("outcome_lineage_trust")
            summary_path.write_text(json.dumps(summary, sort_keys=True), encoding="utf-8")

            legacy_key = registry.experiment_identity("c" * 64, "d" * 64, "legacy-v1")
            registry.path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "runs": {
                            legacy_key: {
                                "base_identity": legacy_key,
                                "run_id": "retained-legacy-run",
                                "market_sha256": "c" * 64,
                                "results_sha256": "d" * 64,
                                "strategy_id": "legacy-v1",
                                "status": "in_progress",
                            }
                        },
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "run summary SHA-256 mismatch"):
                RunRegistry(registry.path)

    def test_schema_downgrade_cannot_reuse_lineage_run_id_as_completed_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
            accepted = self._binding("accepted-root")
            self._accept(registry, accepted)
            self._write_bound_summary(root, accepted)

            state = json.loads(registry.path.read_text(encoding="utf-8"))
            state["schema_version"] = 1
            state.pop("outcome_lineage_trust")
            for entry in state["runs"].values():
                entry.pop("outcome_lineage", None)
            registry.path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "lineage-trust schema was downgraded"):
                RunRegistry(registry.path)

    def test_schema_downgrade_cannot_reuse_lineage_run_id_as_in_progress_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
            accepted = self._binding("accepted-root")
            self._accept(registry, accepted)
            self._write_bound_summary(root, accepted)

            state = json.loads(registry.path.read_text(encoding="utf-8"))
            state["schema_version"] = 1
            state.pop("outcome_lineage_trust")
            for entry in state["runs"].values():
                entry.pop("outcome_lineage", None)
                entry["status"] = "in_progress"
                for field in (
                    "result_path",
                    "paper_book_sha256",
                    "decision_ledger_sha256",
                    "abort_reason",
                    "reconciled_from_summary",
                ):
                    entry.pop(field, None)
            registry.path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "lineage-trust schema was downgraded"):
                RunRegistry(registry.path)

    def test_marker_removal_cannot_reuse_lineage_run_id_as_completed_legacy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
            accepted = self._binding("accepted-root")
            self._accept(registry, accepted)
            summary_path = self._write_bound_summary(root, accepted)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            summary.pop("outcome_lineage_trust")
            summary_path.write_text(json.dumps(summary, sort_keys=True), encoding="utf-8")

            state = json.loads(registry.path.read_text(encoding="utf-8"))
            state["schema_version"] = 1
            state.pop("outcome_lineage_trust")
            for entry in state["runs"].values():
                entry.pop("outcome_lineage", None)
            registry.path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "run summary SHA-256 mismatch"):
                RunRegistry(registry.path)

    def test_schema_two_registry_must_cover_hash_bound_durable_summary_trust(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
            accepted = self._binding("accepted-root")
            self._accept(registry, accepted)
            self._write_bound_summary(root, accepted)

            state = json.loads(registry.path.read_text(encoding="utf-8"))
            conflicting = self._binding(
                "different-root",
                root_revision_id="results-r1-restarted",
            )
            state["outcome_lineage_trust"] = [outcome_lineage_payload(conflicting)]
            state["runs"] = {}
            registry.path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "conflicts with lineage trust"):
                RunRegistry(registry.path)

    def test_lineage_summary_trust_must_match_transaction_summary_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
            accepted = self._binding("accepted-root")
            self._accept(registry, accepted)
            self._write_bound_summary(
                root,
                accepted,
                expected_hash_override="f" * 64,
            )

            with self.assertRaisesRegex(ValueError, "run summary SHA-256 mismatch"):
                RunRegistry(registry.path)

    def test_marker_removal_cannot_bypass_manifest_hash_before_schema_downgrade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
            accepted = self._binding("accepted-root")
            self._accept(registry, accepted)
            summary_path = self._write_bound_summary(root, accepted)

            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            summary.pop("outcome_lineage_trust")
            summary_path.write_text(
                json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            registry.path.write_text(
                json.dumps({"schema_version": 1, "runs": {}}, sort_keys=True),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "run summary SHA-256 mismatch"):
                RunRegistry(registry.path)

    def test_transaction_bound_summary_bytes_are_read_once_for_hash_and_parse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
            accepted = self._binding("accepted-root")
            self._accept(registry, accepted)
            summary_path = self._write_bound_summary(root, accepted)
            original_stable_read = run_registry_module._read_stable_regular_file_bytes
            summary_reads = 0

            def tracked_stable_read(path: Path, *, label: str) -> bytes:
                nonlocal summary_reads
                if path == summary_path:
                    summary_reads += 1
                    if summary_reads > 1:
                        raise AssertionError("transaction-bound run summary path was reopened")
                return original_stable_read(path, label=label)

            with patch.object(
                run_registry_module,
                "_read_stable_regular_file_bytes",
                new=tracked_stable_read,
            ):
                RunRegistry(registry.path)

            self.assertEqual(summary_reads, 1)

    def test_terminal_manifest_invalid_utf8_summary_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
            accepted = self._binding("accepted-root")
            self._accept(registry, accepted)
            summary_path = self._write_bound_summary(root, accepted)
            summary_path.write_bytes(b"\xff\xfe\x00")

            with self.assertRaisesRegex(ValueError, "run summary is invalid"):
                RunRegistry(registry.path)

    def test_summary_leaf_replacement_during_stable_read_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
            accepted = self._binding("accepted-root")
            self._accept(registry, accepted)
            summary_path = self._write_bound_summary(root, accepted)
            replacement = summary_path.with_name("replacement-summary.json")
            replacement.write_bytes(summary_path.read_bytes())

            original_stable_read = run_registry_module._read_stable_regular_file_bytes
            original_os_read = os.read
            reading_summary = False
            replaced = False

            def tracked_stable_read(path: Path, *, label: str) -> bytes:
                nonlocal reading_summary
                if path != summary_path:
                    return original_stable_read(path, label=label)
                reading_summary = True
                try:
                    return original_stable_read(path, label=label)
                finally:
                    reading_summary = False

            def swapping_read(descriptor: int, size: int) -> bytes:
                nonlocal replaced
                chunk = original_os_read(descriptor, size)
                if reading_summary and chunk and not replaced:
                    replaced = True
                    try:
                        replacement.replace(summary_path)
                    except OSError as exc:
                        raise unittest.SkipTest(
                            f"open-file replacement unavailable: {exc}"
                        ) from exc
                return chunk

            with (
                patch.object(
                    run_registry_module,
                    "_read_stable_regular_file_bytes",
                    new=tracked_stable_read,
                ),
                patch.object(os, "read", new=swapping_read),
                self.assertRaisesRegex(ValueError, "run summary changed while validating"),
            ):
                RunRegistry(registry.path)
            self.assertTrue(replaced)

    @unittest.skipIf(os.name == "nt", "POSIX dir_fd replacement regression")
    def test_transaction_parent_replacement_during_manifest_read_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
            accepted = self._binding("accepted-root")
            self._accept(registry, accepted)
            self._write_bound_summary(root, accepted)

            transaction_root = root / ".run-transactions"
            held_root = root / ".run-transactions-held"
            replacement_root = root / ".run-transactions-replacement"
            replacement_manifest = replacement_root / "accepted-run" / "manifest.json"
            replacement_manifest.parent.mkdir(parents=True)
            replacement_manifest.write_bytes(
                (transaction_root / "accepted-run" / "manifest.json").read_bytes()
            )

            original_nested = run_registry_module._read_posix_nested_regular_file_bytes
            original_os_read = os.read
            reading_manifest = False
            replaced = False

            def tracked_nested(
                workspace: Path,
                components: tuple[str, ...],
                *,
                label: str,
            ) -> bytes:
                nonlocal reading_manifest
                reading_manifest = True
                try:
                    return original_nested(workspace, components, label=label)
                finally:
                    reading_manifest = False

            def swapping_read(descriptor: int, size: int) -> bytes:
                nonlocal replaced
                chunk = original_os_read(descriptor, size)
                if reading_manifest and chunk and not replaced:
                    replaced = True
                    transaction_root.rename(held_root)
                    replacement_root.rename(transaction_root)
                return chunk

            with (
                patch.object(
                    run_registry_module,
                    "_read_posix_nested_regular_file_bytes",
                    new=tracked_nested,
                ),
                patch.object(os, "read", new=swapping_read),
                self.assertRaisesRegex(
                    ValueError,
                    "transaction manifest path is not a regular file|canonical namespace",
                ),
            ):
                RunRegistry(registry.path)
            self.assertTrue(replaced)

    def test_transaction_parent_alias_is_rejected_before_manifest_trust(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
            accepted = self._binding("accepted-root")
            self._accept(registry, accepted)
            self._write_bound_summary(root, accepted)

            transaction_root = root / ".run-transactions"
            real_transaction_root = root / ".run-transactions-real"
            transaction_root.rename(real_transaction_root)
            try:
                os.symlink(
                    real_transaction_root,
                    transaction_root,
                    target_is_directory=True,
                )
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"directory symlink unavailable: {exc}")

            with self.assertRaisesRegex(
                ValueError,
                "parent namespace|unsafe|transaction manifest path is not a regular file",
            ):
                RunRegistry(registry.path)

    def test_manifest_leaf_alias_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
            accepted = self._binding("accepted-root")
            self._accept(registry, accepted)
            self._write_bound_summary(root, accepted)

            manifest = root / ".run-transactions" / "accepted-run" / "manifest.json"
            real_manifest = manifest.with_name("manifest.real.json")
            manifest.rename(real_manifest)
            try:
                os.symlink(real_manifest, manifest)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"file symlink unavailable: {exc}")

            with self.assertRaisesRegex(
                ValueError,
                "unsafe|non-aliased|transaction manifest path is not a regular file",
            ):
                RunRegistry(registry.path)

    def test_genuine_never_upgraded_schema_one_workspace_remains_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
            key = registry.begin(
                "a" * 64,
                "b" * 64,
                "baseline-v1",
                "legacy-run",
            )
            registry.complete(key)
            (root / "run-legacy-run.json").write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "run_id": "legacy-run",
                        "real_money_execution": False,
                    },
                    sort_keys=True,
                ) + "\n",
                encoding="utf-8",
            )

            reopened = RunRegistry(registry.path)
            state = json.loads(reopened.path.read_text(encoding="utf-8"))
            self.assertEqual(state["schema_version"], 1)
            self.assertNotIn("outcome_lineage_trust", state)


if __name__ == "__main__":
    unittest.main()

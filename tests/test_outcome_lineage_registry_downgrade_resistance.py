from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
            registry = RunRegistry(root / "run_registry.json")
            accepted = self._binding("accepted-root")
            self._accept(registry, accepted)
            self._write_bound_summary(root, accepted)

            registry.path.write_text(
                json.dumps({"schema_version": 1, "runs": {}}, sort_keys=True),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "lineage-trust schema was downgraded"):
                RunRegistry(registry.path)

    def test_schema_two_registry_must_cover_hash_bound_durable_summary_trust(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry(root / "run_registry.json")
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
            registry = RunRegistry(root / "run_registry.json")
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
            registry = RunRegistry(root / "run_registry.json")
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
            registry = RunRegistry(root / "run_registry.json")
            accepted = self._binding("accepted-root")
            self._accept(registry, accepted)
            summary_path = self._write_bound_summary(root, accepted)
            original_read_bytes = Path.read_bytes
            summary_reads = 0

            def tracked_read_bytes(path: Path) -> bytes:
                nonlocal summary_reads
                if path == summary_path:
                    summary_reads += 1
                    if summary_reads > 1:
                        raise AssertionError("transaction-bound run summary path was reopened")
                return original_read_bytes(path)

            with patch.object(Path, "read_bytes", new=tracked_read_bytes):
                RunRegistry(registry.path)

            self.assertEqual(summary_reads, 1)

    def test_genuine_never_upgraded_schema_one_workspace_remains_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry(root / "run_registry.json")
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

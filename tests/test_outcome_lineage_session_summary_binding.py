from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from autosport.dataset import ReplayDataset
from autosport.integrity import sha256_file
from autosport.outcome_trust import outcome_lineage_binding_from_dataset, outcome_lineage_payload
from autosport.run_registry import RunRegistry
from autosport.session import AutosportSession


class OutcomeLineageSessionSummaryBindingTests(unittest.TestCase):
    @staticmethod
    def _sha(label: str) -> str:
        return hashlib.sha256(label.encode("utf-8")).hexdigest()

    def _dataset(self, root: Path) -> ReplayDataset:
        market_path = root / "market.jsonl"
        market_path.write_bytes(b"")
        record_sha = self._sha("accepted-root-record")
        results_path = root / "results.json"
        results_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "quote_outcomes": {},
                    "outcome_reveal_after": "2026-01-01T15:00:00+00:00",
                    "outcome_provenance": {
                        "schema_version": 2,
                        "source_identity": "official-results:test-fixture",
                        "source_record_id": "table-tennis-results:2026-01-01",
                        "source_record_file": "not-redistributed.json",
                        "source_record_sha256": record_sha,
                        "source_record_revision_id": "results-r1",
                        "source_record_revision": 1,
                        "source_record_revision_kind": "initial",
                        "source_record_lineage_root_sha256": record_sha,
                        "source_record_lineage_root_revision_id": "results-r1",
                        "source_record_lineage_depth": 1,
                        "source_record_lineage": [
                            {
                                "revision_id": "results-r1",
                                "revision": 1,
                                "revision_kind": "initial",
                                "recorded_at": "2026-01-01T11:00:00+00:00",
                                "record_sha256": record_sha,
                                "predecessor_record_sha256": None,
                                "supersedes_revision_id": None,
                                "correction_reason": None,
                            }
                        ],
                        "source_record_lineage_verified": True,
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return ReplayDataset(
            root=root,
            name="lineage-summary-binding",
            sport="table_tennis",
            market_path=market_path,
            results_path=results_path,
            market_sha256=hashlib.sha256(market_path.read_bytes()).hexdigest(),
            results_sha256=hashlib.sha256(results_path.read_bytes()).hexdigest(),
            schema_version=2,
            governance=None,
            import_identity=None,
        )

    def test_completed_session_summary_carries_transaction_hash_bound_lineage_trust(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dataset = self._dataset(root)
            binding = outcome_lineage_binding_from_dataset(dataset)
            self.assertIsNotNone(binding)
            assert binding is not None

            workspace = root / "workspace"
            session = AutosportSession(workspace)
            try:
                result = session.run_dataset(dataset)
            finally:
                session.close()

            summary_path = Path(result.result_path)
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
            self.assertEqual(
                summary["outcome_lineage_trust"],
                outcome_lineage_payload(binding),
            )

            manifest_path = (
                workspace
                / ".run-transactions"
                / result.replay.run_id
                / "manifest.json"
            )
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["new"]["summary_sha256"],
                sha256_file(summary_path),
            )
            self.assertEqual(manifest["phase"], "completed")

            # Reproduce the registry-only schema downgrade after a real completed
            # session rather than a hand-built summary/manifest fixture.
            (workspace / "run_registry.json").write_text(
                json.dumps({"schema_version": 1, "runs": {}}, sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "lineage-trust schema was downgraded"):
                RunRegistry(workspace / "run_registry.json")


if __name__ == "__main__":
    unittest.main()

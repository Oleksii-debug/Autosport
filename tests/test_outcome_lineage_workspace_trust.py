from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.dataset import ReplayDataset, load_dataset
from autosport.outcome_trust import (
    OutcomeLineageTrustError,
    bind_dataset_outcome_lineage,
)
from autosport.session import AutosportSession
from autosport.workspace_lock import WorkspaceEconomicLock


class OutcomeLineageWorkspaceTrustTests(unittest.TestCase):
    source_identity = "official-results:test-fixture"
    record_id = "table-tennis-results:2026-01-01"

    @staticmethod
    def _sha(label: str) -> str:
        return hashlib.sha256(label.encode("utf-8")).hexdigest()

    def _dataset(
        self,
        root: Path,
        *,
        revisions: tuple[tuple[str, str], ...],
        source_identity: str | None = None,
        record_id: str | None = None,
        name: str = "dataset",
    ) -> ReplayDataset:
        source_identity = source_identity or self.source_identity
        record_id = record_id or self.record_id
        lineage = []
        for revision, (revision_id, record_sha) in enumerate(revisions, start=1):
            predecessor = revisions[revision - 2] if revision > 1 else None
            lineage.append(
                {
                    "revision_id": revision_id,
                    "revision": revision,
                    "revision_kind": "initial" if revision == 1 else "correction",
                    "recorded_at": f"2026-01-01T{10 + revision:02d}:00:00+00:00",
                    "record_sha256": record_sha,
                    "predecessor_record_sha256": predecessor[1] if predecessor else None,
                    "supersedes_revision_id": predecessor[0] if predecessor else None,
                    "correction_reason": None if revision == 1 else "official correction",
                }
            )
        first = lineage[0]
        last = lineage[-1]
        results = root / f"{name}-results.json"
        results.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "quote_outcomes": {"tt-a|winner|alice": "void"},
                    "outcome_reveal_after": "2026-01-01T15:00:00+00:00",
                    "outcome_provenance": {
                        "schema_version": 2,
                        "source_identity": source_identity,
                        "source_record_id": record_id,
                        "source_record_file": "not-redistributed.json",
                        "source_record_sha256": last["record_sha256"],
                        "source_record_revision_id": last["revision_id"],
                        "source_record_revision": last["revision"],
                        "source_record_revision_kind": last["revision_kind"],
                        "source_record_lineage_root_sha256": first["record_sha256"],
                        "source_record_lineage_root_revision_id": first["revision_id"],
                        "source_record_lineage_depth": len(lineage),
                        "source_record_lineage": lineage,
                        "source_record_lineage_verified": True,
                    },
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        results_sha = hashlib.sha256(results.read_bytes()).hexdigest()
        return ReplayDataset(
            root=root,
            name=name,
            sport="table_tennis",
            market_path=root / "unused-market.jsonl",
            results_path=results,
            market_sha256="0" * 64,
            results_sha256=results_sha,
            schema_version=2,
            governance=None,
            import_identity=None,
        )

    @staticmethod
    def _bind(workspace: Path, dataset: ReplayDataset):
        with WorkspaceEconomicLock(workspace):
            return bind_dataset_outcome_lineage(workspace, dataset)

    def test_identical_reimport_is_idempotent_and_later_correction_extends_trust(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            r1 = ("results-r1", self._sha("r1"))
            r2 = ("results-r2", self._sha("r2"))
            r3 = ("results-r3", self._sha("r3"))
            first = self._dataset(root, revisions=(r1, r2), name="first")

            self._bind(workspace, first)
            trust_path = workspace / "outcome_lineage_trust.json"
            initial_bytes = trust_path.read_bytes()
            self._bind(workspace, first)
            self.assertEqual(trust_path.read_bytes(), initial_bytes)

            extended = self._dataset(root, revisions=(r1, r2, r3), name="extended")
            binding = self._bind(workspace, extended)
            self.assertIsNotNone(binding)
            assert binding is not None
            self.assertEqual(binding.head.revision, 3)
            registry = json.loads(trust_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [item["revision_id"] for item in registry["records"][0]["revisions"]],
                ["results-r1", "results-r2", "results-r3"],
            )

            # Replaying a previously trusted historical prefix cannot downgrade the
            # durable latest chain, but remains a valid idempotent import.
            self._bind(workspace, first)
            registry_after_prefix = json.loads(trust_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [item["revision_id"] for item in registry_after_prefix["records"][0]["revisions"]],
                ["results-r1", "results-r2", "results-r3"],
            )

    def test_restart_from_different_revision_one_root_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            accepted = self._dataset(
                root,
                revisions=(("results-r1", self._sha("accepted-root")),),
                name="accepted",
            )
            restarted = self._dataset(
                root,
                revisions=(("results-r1-restarted", self._sha("malicious-root")),),
                name="restarted",
            )

            self._bind(workspace, accepted)
            with self.assertRaisesRegex(OutcomeLineageTrustError, "different root"):
                self._bind(workspace, restarted)

            registry = json.loads(
                (workspace / "outcome_lineage_trust.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                registry["records"][0]["root_record_sha256"],
                self._sha("accepted-root"),
            )

    def test_same_root_divergent_correction_fork_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            root_revision = ("results-r1", self._sha("root"))
            accepted = self._dataset(
                root,
                revisions=(root_revision, ("results-r2", self._sha("accepted-r2"))),
                name="accepted",
            )
            fork = self._dataset(
                root,
                revisions=(root_revision, ("results-r2-fork", self._sha("fork-r2"))),
                name="fork",
            )

            self._bind(workspace, accepted)
            with self.assertRaisesRegex(OutcomeLineageTrustError, "diverged at revision 2"):
                self._bind(workspace, fork)

    def test_malformed_or_tampered_durable_registry_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "outcome_lineage_trust.json").write_text(
                '{"schema_version":1,"schema_version":1,"records":[]}',
                encoding="utf-8",
            )
            dataset = self._dataset(
                root,
                revisions=(("results-r1", self._sha("r1")),),
                name="candidate",
            )

            with self.assertRaisesRegex(OutcomeLineageTrustError, "duplicate JSON object key"):
                self._bind(workspace, dataset)

    def test_results_hash_drift_is_rejected_before_registry_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = root / "workspace"
            dataset = self._dataset(
                root,
                revisions=(("results-r1", self._sha("r1")),),
                name="candidate",
            )
            dataset.results_path.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(OutcomeLineageTrustError, "hash changed"):
                self._bind(workspace, dataset)
            self.assertFalse((workspace / "outcome_lineage_trust.json").exists())

    def test_session_enforces_trust_gate_before_any_economic_mutation(self) -> None:
        dataset = load_dataset(Path("examples/tt_demo"))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session = AutosportSession(workspace, "10000")
            with patch(
                "autosport.session.bind_dataset_outcome_lineage",
                side_effect=OutcomeLineageTrustError("synthetic lineage conflict"),
            ):
                with self.assertRaisesRegex(OutcomeLineageTrustError, "synthetic lineage conflict"):
                    session.run_dataset(dataset)
            session.close()

            self.assertFalse((workspace / "paper_book.json").exists())
            registry = json.loads(
                (workspace / "run_registry.json").read_text(encoding="utf-8")
            )
            self.assertEqual(registry["runs"], {})


if __name__ == "__main__":
    unittest.main()

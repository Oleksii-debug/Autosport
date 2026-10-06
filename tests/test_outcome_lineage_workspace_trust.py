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
    outcome_lineage_binding_from_dataset,
)
from autosport.run_registry import RunRegistry
from autosport.session import AutosportSession


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
            market_sha256=self._sha(f"market-{name}"),
            results_sha256=results_sha,
            schema_version=2,
            governance=None,
            import_identity=None,
        )

    @staticmethod
    def _binding(dataset: ReplayDataset):
        binding = outcome_lineage_binding_from_dataset(dataset)
        assert binding is not None
        return binding

    @staticmethod
    def _accept(
        registry: RunRegistry,
        dataset: ReplayDataset,
        *,
        run_id: str,
        allow_repeat: bool = False,
    ) -> str:
        key = registry.begin(
            dataset.market_sha256,
            dataset.results_sha256,
            "baseline-v1",
            run_id,
            allow_repeat=allow_repeat,
            outcome_lineage=OutcomeLineageWorkspaceTrustTests._binding(dataset),
        )
        registry.complete(key)
        return key

    def test_identical_reimport_and_extension_persist_in_existing_run_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
            r1 = ("results-r1", self._sha("r1"))
            r2 = ("results-r2", self._sha("r2"))
            r3 = ("results-r3", self._sha("r3"))
            first = self._dataset(root, revisions=(r1, r2), name="first")

            self._accept(registry, first, run_id="run-one")
            self._accept(
                registry,
                first,
                run_id="run-two",
                allow_repeat=True,
            )
            extended = self._dataset(root, revisions=(r1, r2, r3), name="extended")
            self._accept(registry, extended, run_id="run-three")

            # A previously trusted prefix remains acceptable after a later extension.
            registry.assert_outcome_lineage_compatible(self._binding(first))
            with self.assertRaisesRegex(
                OutcomeLineageTrustError,
                "older than the trusted current head",
            ):
                registry.begin(
                    first.market_sha256,
                    first.results_sha256,
                    "baseline-v1",
                    "stale-prefix-new-run",
                    allow_repeat=True,
                    outcome_lineage=self._binding(first),
                )
            state = json.loads((root / "run_registry.json").read_text(encoding="utf-8"))
            histories = [
                item["outcome_lineage"]["revisions"]
                for item in state["runs"].values()
                if "outcome_lineage" in item
            ]
            self.assertEqual([len(history) for history in histories], [2, 2, 3])
            self.assertFalse((root / "outcome_lineage_trust.json").exists())

    def test_restart_from_different_revision_one_root_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
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
            self._accept(registry, accepted, run_id="accepted-run")

            with self.assertRaisesRegex(OutcomeLineageTrustError, "different root"):
                registry.assert_outcome_lineage_compatible(self._binding(restarted))
            with self.assertRaisesRegex(OutcomeLineageTrustError, "different root"):
                registry.begin(
                    restarted.market_sha256,
                    restarted.results_sha256,
                    "baseline-v1",
                    "restarted-run",
                    outcome_lineage=self._binding(restarted),
                )
            self.assertEqual(len(json.loads(registry.path.read_text(encoding="utf-8"))["runs"]), 1)

    def test_same_root_divergent_correction_fork_is_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
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
            self._accept(registry, accepted, run_id="accepted-run")

            with self.assertRaisesRegex(OutcomeLineageTrustError, "diverged at revision 2"):
                registry.assert_outcome_lineage_compatible(self._binding(fork))

    def test_registry_reopen_rejects_tampered_conflicting_lineage_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
            r1 = ("results-r1", self._sha("root"))
            first = self._dataset(
                root,
                revisions=(r1, ("results-r2", self._sha("r2"))),
                name="first",
            )
            second = self._dataset(
                root,
                revisions=(r1, ("results-r2", self._sha("r2")), ("results-r3", self._sha("r3"))),
                name="second",
            )
            self._accept(registry, first, run_id="run-one")
            self._accept(registry, second, run_id="run-two")

            state = json.loads(registry.path.read_text(encoding="utf-8"))
            second_item = list(state["runs"].values())[1]
            second_item["outcome_lineage"]["revisions"][1]["record_sha256"] = self._sha("forked-r2")
            registry.path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "conflicting outcome lineage evidence"):
                RunRegistry(registry.path)

    def test_results_hash_drift_is_rejected_before_registry_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
            dataset = self._dataset(
                root,
                revisions=(("results-r1", self._sha("r1")),),
                name="candidate",
            )
            before = registry.path.read_bytes()
            dataset.results_path.write_text("{}", encoding="utf-8")

            with self.assertRaisesRegex(OutcomeLineageTrustError, "hash changed"):
                outcome_lineage_binding_from_dataset(dataset)
            self.assertEqual(registry.path.read_bytes(), before)

    def test_session_enforces_conflict_check_before_any_economic_mutation(self) -> None:
        dataset = load_dataset(Path("examples/tt_demo"))
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            session = AutosportSession(workspace, "10000")
            with patch(
                "autosport.session.outcome_lineage_binding_from_dataset",
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

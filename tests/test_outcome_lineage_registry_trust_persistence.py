from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from autosport.outcome_trust import (
    OutcomeLineageBinding,
    OutcomeLineageTrustError,
    TrustedOutcomeRevision,
)
from autosport.run_registry import RunRegistry


class OutcomeLineageRegistryTrustPersistenceTests(unittest.TestCase):
    source_identity = "official-results:test-fixture"
    record_id = "table-tennis-results:2026-01-01"

    @staticmethod
    def _sha(label: str) -> str:
        return hashlib.sha256(label.encode("utf-8")).hexdigest()

    def _binding(
        self,
        *labels: str,
        root_revision_id: str = "results-r1",
    ) -> OutcomeLineageBinding:
        revisions = tuple(
            TrustedOutcomeRevision(
                revision=index,
                revision_id=(root_revision_id if index == 1 else f"results-r{index}"),
                record_sha256=self._sha(label),
            )
            for index, label in enumerate(labels, start=1)
        )
        return OutcomeLineageBinding(
            source_identity=self.source_identity,
            record_id=self.record_id,
            root_revision_id=revisions[0].revision_id,
            root_record_sha256=revisions[0].record_sha256,
            revisions=revisions,
        )

    @staticmethod
    def _accept(
        registry: RunRegistry,
        binding: OutcomeLineageBinding,
        *,
        run_id: str = "accepted-run",
    ) -> str:
        key = registry.begin(
            "a" * 64,
            "b" * 64,
            "baseline-v1",
            run_id,
            outcome_lineage=binding,
        )
        registry.complete(key)
        return key

    def test_registry_level_trust_survives_completed_run_entry_deletion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry(root / "run_registry.json")
            accepted = self._binding("accepted-root")
            restarted = self._binding(
                "different-root",
                root_revision_id="results-r1-restarted",
            )
            accepted_key = self._accept(registry, accepted)

            # Preserve durable completed-run history while simulating deletion of
            # only the registry run entry that originally carried lineage trust.
            (root / "run-accepted-run.json").write_text("{}\n", encoding="utf-8")
            state = json.loads(registry.path.read_text(encoding="utf-8"))
            self.assertEqual(state["schema_version"], 2)
            self.assertEqual(len(state["outcome_lineage_trust"]), 1)
            del state["runs"][accepted_key]
            registry.path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

            reopened = RunRegistry(registry.path)
            before = registry.path.read_bytes()
            with self.assertRaisesRegex(OutcomeLineageTrustError, "different root"):
                reopened.assert_outcome_lineage_compatible(restarted)
            with self.assertRaisesRegex(OutcomeLineageTrustError, "different root"):
                reopened.begin(
                    "c" * 64,
                    "d" * 64,
                    "baseline-v1",
                    "restarted-run",
                    outcome_lineage=restarted,
                )
            self.assertEqual(registry.path.read_bytes(), before)

    def test_lineage_registry_cannot_drop_registry_level_trust_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry(root / "run_registry.json")
            self._accept(registry, self._binding("accepted-root"))

            state = json.loads(registry.path.read_text(encoding="utf-8"))
            self.assertEqual(state["schema_version"], 2)
            state.pop("outcome_lineage_trust")
            registry.path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "invalid run registry"):
                RunRegistry(registry.path)

    def test_run_lineage_cannot_exceed_registry_level_trust_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry(root / "run_registry.json")
            accepted = self._binding("accepted-root", "accepted-correction")
            self._accept(registry, accepted)

            state = json.loads(registry.path.read_text(encoding="utf-8"))
            trust = state["outcome_lineage_trust"][0]
            self.assertEqual(len(trust["revisions"]), 2)
            trust["revisions"].pop()
            registry.path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                "exceeds registry-level trust history",
            ):
                RunRegistry(registry.path)

    def test_registry_level_trust_advances_to_longest_compatible_extension(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry(root / "run_registry.json")
            initial = self._binding("accepted-root")
            extended = self._binding("accepted-root", "accepted-correction")
            self._accept(registry, initial, run_id="run-one")
            registry.begin(
                "c" * 64,
                "d" * 64,
                "baseline-v1",
                "run-two",
                outcome_lineage=extended,
            )

            state = json.loads(registry.path.read_text(encoding="utf-8"))
            self.assertEqual(state["schema_version"], 2)
            self.assertEqual(len(state["outcome_lineage_trust"]), 1)
            self.assertEqual(
                len(state["outcome_lineage_trust"][0]["revisions"]),
                2,
            )


if __name__ == "__main__":
    unittest.main()

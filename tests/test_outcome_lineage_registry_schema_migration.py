from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from autosport.outcome_trust import OutcomeLineageBinding, TrustedOutcomeRevision
from autosport.run_registry import RunRegistry


class OutcomeLineageRegistrySchemaMigrationTests(unittest.TestCase):
    @staticmethod
    def _binding() -> OutcomeLineageBinding:
        digest = hashlib.sha256(b"accepted-root").hexdigest()
        revision = TrustedOutcomeRevision(
            revision=1,
            revision_id="results-r1",
            record_sha256=digest,
        )
        return OutcomeLineageBinding(
            source_identity="official-results:test-fixture",
            record_id="table-tennis-results:2026-01-01",
            root_revision_id=revision.revision_id,
            root_record_sha256=revision.record_sha256,
            revisions=(revision,),
        )

    def test_first_lineage_acceptance_migrates_existing_schema_one_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            registry = RunRegistry.initialize_pristine(root / "run_registry.json")
            legacy_key = registry.begin(
                "a" * 64,
                "b" * 64,
                "baseline-v1",
                "legacy-run",
            )
            registry.complete(legacy_key)

            legacy_state = json.loads(registry.path.read_text(encoding="utf-8"))
            self.assertEqual(legacy_state["schema_version"], 1)
            self.assertNotIn("outcome_lineage_trust", legacy_state)

            binding = self._binding()
            lineage_key = registry.begin(
                "c" * 64,
                "d" * 64,
                "baseline-v1",
                "lineage-run",
                outcome_lineage=binding,
            )

            migrated = json.loads(registry.path.read_text(encoding="utf-8"))
            self.assertEqual(migrated["schema_version"], 2)
            self.assertIn(legacy_key, migrated["runs"])
            self.assertIn(lineage_key, migrated["runs"])
            self.assertNotIn("outcome_lineage", migrated["runs"][legacy_key])
            self.assertEqual(len(migrated["outcome_lineage_trust"]), 1)
            self.assertEqual(
                migrated["outcome_lineage_trust"][0]["root_record_sha256"],
                binding.root_record_sha256,
            )

            reopened = RunRegistry(registry.path)
            reopened.assert_outcome_lineage_compatible(binding)


if __name__ == "__main__":
    unittest.main()

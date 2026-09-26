from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.outcome_trust import (
    OutcomeLineageBinding,
    OutcomeLineageTrustError,
    TrustedOutcomeRevision,
)
from autosport.run_registry import RunRegistry


class OutcomeRevisionProductAvailabilityTests(unittest.TestCase):
    source_identity = "official-results:test-fixture"
    record_id = "table-tennis-results:2026-01-01"
    t0 = "2026-01-01T09:59:59Z"
    t1 = "2026-01-01T10:00:00Z"
    t_mid = "2026-01-01T10:30:00Z"
    t2 = "2026-01-01T11:00:00Z"
    t3 = "2026-01-01T12:00:00Z"

    @staticmethod
    def _sha(label: str) -> str:
        return hashlib.sha256(label.encode("utf-8")).hexdigest()

    def _binding(
        self,
        *labels: str,
        forged_available_at: str | None = None,
    ) -> OutcomeLineageBinding:
        revisions = tuple(
            TrustedOutcomeRevision(
                revision=index,
                revision_id=f"results-r{index}",
                record_sha256=self._sha(label),
                first_available_at=forged_available_at,
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

    def _accept(
        self,
        registry: RunRegistry,
        binding: OutcomeLineageBinding,
        *,
        run_id: str,
        market: str,
        results: str,
        accepted_at: str,
    ) -> None:
        with patch("autosport.run_registry._utc_now", return_value=accepted_at):
            key = registry.begin(
                self._sha(market),
                self._sha(results),
                "baseline-v1",
                run_id,
                outcome_lineage=binding,
            )
        registry.complete(key)

    def _resolve(
        self,
        registry: RunRegistry,
        cutoff: str,
    ) -> TrustedOutcomeRevision | None:
        return registry.outcome_revision_as_of(
            source_identity=self.source_identity,
            record_id=self.record_id,
            cutoff=cutoff,
        )

    def test_extension_resolves_only_revisions_product_available_by_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = RunRegistry.initialize_pristine(Path(tmp) / "run_registry.json")
            first = self._binding("r1")
            extended = self._binding("r1", "r2")

            self._accept(
                registry,
                first,
                run_id="run-r1",
                market="market-r1",
                results="results-r1",
                accepted_at=self.t1,
            )
            self.assertIsNone(self._resolve(registry, self.t0))
            self.assertEqual(self._resolve(registry, self.t1).revision, 1)

            self._accept(
                registry,
                extended,
                run_id="run-r2",
                market="market-r2",
                results="results-r2",
                accepted_at=self.t2,
            )
            self.assertEqual(self._resolve(registry, self.t_mid).revision, 1)
            self.assertEqual(self._resolve(registry, self.t2).revision, 2)

            reopened = RunRegistry(registry.path)
            self.assertEqual(self._resolve(reopened, self.t_mid).revision, 1)
            self.assertEqual(self._resolve(reopened, self.t3).revision, 2)

    def test_first_import_of_full_chain_does_not_backdate_predecessor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = RunRegistry.initialize_pristine(Path(tmp) / "run_registry.json")
            full_chain = self._binding("r1", "r2")

            self._accept(
                registry,
                full_chain,
                run_id="late-import",
                market="market-late",
                results="results-late",
                accepted_at=self.t2,
            )

            self.assertIsNone(self._resolve(registry, self.t_mid))
            resolved = self._resolve(registry, self.t2)
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.revision, 2)

            state = json.loads(registry.path.read_text(encoding="utf-8"))
            revisions = state["outcome_lineage_trust"][0]["revisions"]
            self.assertEqual(
                {item["first_available_at"] for item in revisions},
                {"2026-01-01T11:00:00.000000Z"},
            )

    def test_exact_reimport_preserves_earliest_product_availability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = RunRegistry.initialize_pristine(Path(tmp) / "run_registry.json")
            binding = self._binding("r1")

            self._accept(
                registry,
                binding,
                run_id="first-import",
                market="market-first",
                results="results-first",
                accepted_at=self.t1,
            )
            self._accept(
                registry,
                binding,
                run_id="exact-reimport",
                market="market-second",
                results="results-second",
                accepted_at=self.t0,
            )

            resolved = self._resolve(registry, self.t1)
            self.assertIsNotNone(resolved)
            self.assertEqual(
                resolved.first_available_at,
                "2026-01-01T10:00:00.000000Z",
            )

    def test_caller_cannot_backdate_product_availability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = RunRegistry.initialize_pristine(Path(tmp) / "run_registry.json")
            forged = self._binding(
                "r1",
                forged_available_at="2000-01-01T00:00:00Z",
            )
            self._accept(
                registry,
                forged,
                run_id="forged-time",
                market="market-forged",
                results="results-forged",
                accepted_at=self.t2,
            )

            self.assertIsNone(self._resolve(registry, self.t1))
            self.assertEqual(
                self._resolve(registry, self.t2).first_available_at,
                "2026-01-01T11:00:00.000000Z",
            )

    def test_as_of_cutoff_rejects_string_subclass_before_virtual_replace(self) -> None:
        class ForgedCutoff(str):
            replace_calls = 0

            def replace(self, old, new, count=-1):
                type(self).replace_calls += 1
                del old, new, count
                return "2026-01-01T12:00:00+00:00"

        with tempfile.TemporaryDirectory() as tmp:
            registry = RunRegistry.initialize_pristine(Path(tmp) / "run_registry.json")
            self._accept(
                registry,
                self._binding("r1"),
                run_id="exact-cutoff",
                market="market-exact-cutoff",
                results="results-exact-cutoff",
                accepted_at=self.t1,
            )

            forged = ForgedCutoff(self.t0)
            with self.assertRaisesRegex(ValueError, "cutoff.*exact"):
                self._resolve(registry, forged)
            self.assertEqual(ForgedCutoff.replace_calls, 0)

    def test_as_of_identity_rejects_string_subclass_before_hash_lookup(self) -> None:
        class ForgedIdentity(str):
            hash_calls = 0

            def __hash__(self):
                type(self).hash_calls += 1
                return str.__hash__(self)

        with tempfile.TemporaryDirectory() as tmp:
            registry = RunRegistry.initialize_pristine(Path(tmp) / "run_registry.json")
            self._accept(
                registry,
                self._binding("r1"),
                run_id="exact-identity",
                market="market-exact-identity",
                results="results-exact-identity",
                accepted_at=self.t1,
            )

            with self.assertRaisesRegex(ValueError, "source_identity.*exact"):
                registry.outcome_revision_as_of(
                    source_identity=ForgedIdentity(self.source_identity),
                    record_id=self.record_id,
                    cutoff=self.t1,
                )
            self.assertEqual(ForgedIdentity.hash_calls, 0)

    def test_equivalent_timezone_cutoffs_match_and_naive_cutoff_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = RunRegistry.initialize_pristine(Path(tmp) / "run_registry.json")
            self._accept(
                registry,
                self._binding("r1"),
                run_id="timezone",
                market="market-timezone",
                results="results-timezone",
                accepted_at=self.t1,
            )

            utc = self._resolve(registry, "2026-01-01T10:00:00Z")
            offset = self._resolve(registry, "2026-01-01T12:00:00+02:00")
            self.assertEqual(utc, offset)
            with self.assertRaisesRegex(
                OutcomeLineageTrustError,
                "explicit timezone",
            ):
                self._resolve(registry, "2026-01-01T10:00:00")

    def test_registry_trust_cannot_strip_availability_preserved_by_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = RunRegistry.initialize_pristine(Path(tmp) / "run_registry.json")
            self._accept(
                registry,
                self._binding("r1"),
                run_id="durable-availability",
                market="market-durable",
                results="results-durable",
                accepted_at=self.t1,
            )

            state = json.loads(registry.path.read_text(encoding="utf-8"))
            trust_revision = state["outcome_lineage_trust"][0]["revisions"][0]
            self.assertEqual(
                trust_revision.pop("first_available_at"),
                "2026-01-01T10:00:00.000000Z",
            )
            registry.path.write_text(
                json.dumps(state, sort_keys=True),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "conflicting outcome lineage evidence",
            ):
                RunRegistry(registry.path)

    def test_legacy_unstamped_trust_migrates_without_backdating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = RunRegistry.initialize_pristine(Path(tmp) / "run_registry.json")
            self._accept(
                registry,
                self._binding("r1"),
                run_id="legacy-origin",
                market="market-origin",
                results="results-origin",
                accepted_at=self.t1,
            )

            state = json.loads(registry.path.read_text(encoding="utf-8"))
            for revision in state["outcome_lineage_trust"][0]["revisions"]:
                revision.pop("first_available_at", None)
            for item in state["runs"].values():
                lineage = item.get("outcome_lineage")
                if lineage is not None:
                    for revision in lineage["revisions"]:
                        revision.pop("first_available_at", None)
            registry.path.write_text(
                json.dumps(state, sort_keys=True),
                encoding="utf-8",
            )

            reopened = RunRegistry(registry.path)
            self.assertIsNone(self._resolve(reopened, self.t1))
            self.assertIsNone(self._resolve(reopened, self.t_mid))

            self._accept(
                reopened,
                self._binding("r1", "r2"),
                run_id="post-migration-extension",
                market="market-extension",
                results="results-extension",
                accepted_at=self.t2,
            )

            self.assertIsNone(self._resolve(reopened, self.t_mid))
            resolved = self._resolve(reopened, self.t2)
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.revision, 2)
            migrated = json.loads(reopened.path.read_text(encoding="utf-8"))
            self.assertEqual(
                {
                    revision["first_available_at"]
                    for revision in migrated["outcome_lineage_trust"][0]["revisions"]
                },
                {"2026-01-01T11:00:00.000000Z"},
            )

    def test_clock_rollback_cannot_backdate_new_correction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            registry = RunRegistry.initialize_pristine(Path(tmp) / "run_registry.json")
            self._accept(
                registry,
                self._binding("r1"),
                run_id="accepted",
                market="market-accepted",
                results="results-accepted",
                accepted_at=self.t2,
            )
            before = registry.path.read_bytes()

            with patch("autosport.run_registry._utc_now", return_value=self.t1):
                with self.assertRaisesRegex(
                    OutcomeLineageTrustError,
                    "predates already trusted",
                ):
                    registry.begin(
                        self._sha("market-extension"),
                        self._sha("results-extension"),
                        "baseline-v1",
                        "clock-rollback",
                        outcome_lineage=self._binding("r1", "r2"),
                    )

            self.assertEqual(registry.path.read_bytes(), before)
            self.assertEqual(self._resolve(registry, self.t2).revision, 1)


if __name__ == "__main__":
    unittest.main()

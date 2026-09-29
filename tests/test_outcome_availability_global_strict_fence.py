from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import autosport._outcome_availability_clock_dispatch_guard as availability_guard
import autosport.outcome_trust as outcome_trust
from autosport.outcome_trust import (
    OutcomeLineageBinding,
    OutcomeLineageTrustError,
    TrustedOutcomeRevision,
)
from autosport.run_registry import RunRegistry


class OutcomeAvailabilityGlobalStrictFenceTests(unittest.TestCase):
    @staticmethod
    def _sha(label: str) -> str:
        return hashlib.sha256(label.encode("utf-8")).hexdigest()

    def _binding(
        self,
        *,
        source_identity: str,
        record_id: str,
        label: str,
    ) -> OutcomeLineageBinding:
        revision = TrustedOutcomeRevision(
            revision=1,
            revision_id=f"{record_id}-r1",
            record_sha256=self._sha(label),
        )
        return OutcomeLineageBinding(
            source_identity=source_identity,
            record_id=record_id,
            root_revision_id=revision.revision_id,
            root_record_sha256=revision.record_sha256,
            revisions=(revision,),
        )

    def test_equal_representable_tick_is_not_strictly_after_established_history(self) -> None:
        fence = "2026-01-01T10:00:00Z"

        with self.assertRaisesRegex(
            OutcomeLineageTrustError,
            "strictly later than registry-wide established",
        ):
            availability_guard._strictly_after_registry_fence(
                "2026-01-01T10:00:00+00:00",
                fence,
                canonical_timestamp=outcome_trust._canonical_timestamp,
                parse_timestamp=outcome_trust._parse_timestamp,
                error_type=OutcomeLineageTrustError,
            )

        self.assertEqual(
            availability_guard._strictly_after_registry_fence(
                "2026-01-01T10:00:00.000001Z",
                fence,
                canonical_timestamp=outcome_trust._canonical_timestamp,
                parse_timestamp=outcome_trust._parse_timestamp,
                error_type=OutcomeLineageTrustError,
            ),
            "2026-01-01T10:00:00.000001Z",
        )

    def test_cross_lineage_clock_rollback_leaves_new_identity_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run_registry.json"
            registry = RunRegistry.initialize_pristine(path)
            first = self._binding(
                source_identity="official-results:first",
                record_id="event:first",
                label="first",
            )
            first_key = registry.begin(
                self._sha("market-first"),
                self._sha("results-first"),
                "baseline-v1",
                "run-first",
                outcome_lineage=first,
            )
            registry.complete(first_key)

            # Model a legitimate previously-established product timestamp followed
            # by a wall-clock rollback. RunRegistry intentionally does not claim a
            # rollback-proof wall clock, so the causal fence must come from durable
            # already-established product history rather than the current clock.
            future = "2999-01-01T00:00:00.000000Z"
            state = json.loads(path.read_text(encoding="utf-8"))
            for binding in state["outcome_lineage_trust"]:
                for revision in binding["revisions"]:
                    revision["first_available_at"] = future
            for entry in state["runs"].values():
                lineage = entry.get("outcome_lineage")
                if lineage is not None:
                    for revision in lineage["revisions"]:
                        revision["first_available_at"] = future
            path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

            reopened = RunRegistry(path)
            second = self._binding(
                source_identity="official-results:second",
                record_id="event:second",
                label="second",
            )
            with self.assertRaisesRegex(
                OutcomeLineageTrustError,
                "strictly later than registry-wide established",
            ):
                reopened.begin(
                    self._sha("market-second"),
                    self._sha("results-second"),
                    "baseline-v1",
                    "run-second",
                    outcome_lineage=second,
                )

            durable = json.loads(path.read_text(encoding="utf-8"))
            by_identity = {
                (binding["source_identity"], binding["record_id"]): binding
                for binding in durable["outcome_lineage_trust"]
            }
            staged = by_identity[("official-results:second", "event:second")]
            self.assertNotIn("first_available_at", staged["revisions"][0])
            self.assertNotIn(
                "run-second",
                {entry["run_id"] for entry in durable["runs"].values()},
            )


if __name__ == "__main__":
    unittest.main()

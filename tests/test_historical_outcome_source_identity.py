from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from autosport.historical_corpus import _outcome_provenance


class HistoricalOutcomeSourceIdentityTests(unittest.TestCase):
    def _results(
        self,
        root: Path,
        *,
        record_source: object = "official-results:test-fixture",
        claimed_source: str = "official-results:test-fixture",
        include_source: bool = True,
    ) -> dict[str, object]:
        outcomes = {"tt-a|winner|alice": "win"}
        source_record: dict[str, object] = {"quote_outcomes": outcomes}
        if include_source:
            source_record["source"] = record_source
        source_path = root / "official-outcome-source-record.json"
        source_path.write_text(json.dumps(source_record, sort_keys=True), encoding="utf-8")
        source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
        return {
            "quote_outcomes": outcomes,
            "outcome_provenance": {
                "schema_version": 1,
                "kind": "historical_outcome_provenance",
                "source_identity": claimed_source,
                "source_record_file": source_path.name,
                "source_record_sha256": source_sha256,
                "terms_reference": "https://example.test/results-terms",
                "retention_basis": "test-only",
                "authority_reference": "test-authority",
                "available_at": "2026-01-01T11:00:00+00:00",
                "acquired_at": "2026-01-02T00:02:00+00:00",
                "verified_at": "2026-01-02T00:03:00+00:00",
                "licensing_or_retention_verified": True,
                "redistribution_policy": "internal_only",
                "redistribution_verified": False,
            },
        }

    def _validate(self, root: Path, results: dict[str, object]) -> dict[str, object]:
        return _outcome_provenance(
            results,
            source_root=root,
            reveal_dt=datetime.fromisoformat("2026-01-01T11:00:00+00:00"),
            imported_dt=datetime.fromisoformat("2026-01-02T00:05:00+00:00"),
        )

    def test_matching_source_identity_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provenance = self._validate(root, self._results(root))
            self.assertEqual(provenance["source_identity"], "official-results:test-fixture")

    def test_rehashed_source_record_cannot_be_relabelled_as_another_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results = self._results(
                root,
                record_source="official-results:actual",
                claimed_source="official-results:claimed",
            )
            with self.assertRaisesRegex(
                ValueError,
                "source record.source must match sealed results outcome_provenance.source_identity",
            ):
                self._validate(root, results)

    def test_source_record_requires_explicit_source_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            results = self._results(root, include_source=False)
            with self.assertRaisesRegex(
                ValueError,
                "sealed outcome source record.source must be a non-empty string",
            ):
                self._validate(root, results)


if __name__ == "__main__":
    unittest.main()

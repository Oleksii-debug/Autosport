from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from autosport.historical_corpus import _outcome_provenance


class HistoricalOutcomeLineageDowngradeTests(unittest.TestCase):
    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _results(
        self,
        root: Path,
        *,
        forged_lineage: dict[str, object],
    ) -> dict[str, object]:
        quote_key = "tt-a|winner|alice"
        source_record = root / "official-outcomes-v1.json"
        source_record.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source": "official-results:test-fixture",
                    "quote_outcomes": {quote_key: "win"},
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        provenance: dict[str, object] = {
            "schema_version": 1,
            "kind": "historical_outcome_provenance",
            "source_identity": "official-results:test-fixture",
            "source_record_file": source_record.name,
            "source_record_sha256": self._sha256(source_record),
            "terms_reference": "https://example.test/results-terms",
            "retention_basis": "verified internal research retention for test outcome record",
            "authority_reference": "test-outcome-authority-record",
            "available_at": "2026-01-01T12:00:00+00:00",
            "acquired_at": "2026-01-02T00:02:00+00:00",
            "verified_at": "2026-01-02T00:03:00+00:00",
            "licensing_or_retention_verified": True,
            "redistribution_policy": "internal_only",
            "redistribution_verified": False,
        }
        provenance.update(forged_lineage)
        return {
            "schema_version": 1,
            "quote_outcomes": {quote_key: "win"},
            "outcome_provenance": provenance,
        }

    def test_schema_v1_cannot_claim_verifier_derived_lineage_evidence(self) -> None:
        complete_forgery = {
            "source_record_id": "forged-record",
            "source_record_revision_id": "forged-r9",
            "source_record_revision": 9,
            "source_record_revision_kind": "correction",
            "source_record_recorded_at": "2026-01-01T11:55:00+00:00",
            "source_record_predecessor_sha256": "1" * 64,
            "source_record_supersedes_revision_id": "forged-r8",
            "source_record_lineage_root_sha256": "2" * 64,
            "source_record_lineage_root_revision_id": "forged-r1",
            "source_record_lineage_depth": 9,
            "source_record_lineage": [],
            "source_record_lineage_verified": True,
        }
        cases = (
            {"source_record_lineage_verified": True},
            complete_forgery,
        )

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for forged_lineage in cases:
                with self.subTest(fields=sorted(forged_lineage)):
                    results = self._results(root, forged_lineage=forged_lineage)
                    with self.assertRaisesRegex(
                        ValueError,
                        "must not provide verifier-derived lineage fields",
                    ):
                        _outcome_provenance(
                            results,
                            source_root=root,
                            reveal_dt=datetime.fromisoformat("2026-01-01T12:00:00+00:00"),
                            imported_dt=datetime.fromisoformat("2026-01-02T00:05:00+00:00"),
                        )


if __name__ == "__main__":
    unittest.main()

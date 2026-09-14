from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from autosport.historical_corpus import _outcome_provenance


class HistoricalOutcomeLineageV2IntegrationTests(unittest.TestCase):
    source = "official-results:test-fixture"
    quote_key = "tt-a|winner|alice"

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    @staticmethod
    def _write(path: Path, payload: dict[str, object]) -> Path:
        path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        return path

    def _provenance(
        self,
        *,
        schema_version: int,
        source_record: Path,
        available_at: str = "2026-01-01T12:00:00+00:00",
    ) -> dict[str, object]:
        return {
            "schema_version": schema_version,
            "kind": "historical_outcome_provenance",
            "source_identity": self.source,
            "source_record_file": source_record.name,
            "source_record_sha256": self._sha256(source_record),
            "terms_reference": "https://results.example/terms",
            "retention_basis": "test-fixture",
            "authority_reference": "test-authority",
            "available_at": available_at,
            "acquired_at": "2026-01-01T12:05:00+00:00",
            "verified_at": "2026-01-01T12:10:00+00:00",
            "licensing_or_retention_verified": True,
            "redistribution_policy": "internal_only",
            "redistribution_verified": False,
        }

    @staticmethod
    def _dt(value: str) -> datetime:
        return datetime.fromisoformat(value)

    def test_schema_v1_remains_legacy_compatible_without_lineage_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_record = self._write(
                root / "legacy-outcomes.json",
                {
                    "source": self.source,
                    "quote_outcomes": {self.quote_key: "win"},
                },
            )
            provenance = self._provenance(
                schema_version=1,
                source_record=source_record,
            )
            results = {
                "quote_outcomes": {self.quote_key: "win"},
                "outcome_provenance": provenance,
            }

            normalized = _outcome_provenance(
                results,
                source_root=root,
                reveal_dt=self._dt("2026-01-01T12:15:00+00:00"),
                imported_dt=self._dt("2026-01-01T12:20:00+00:00"),
            )

            self.assertEqual(normalized["schema_version"], 1)
            self.assertNotIn("source_record_lineage_verified", normalized)
            self.assertNotIn("source_record_revision_id", normalized)

    def test_schema_v2_root_is_lineage_verified_and_head_is_read_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_record = self._write(
                root / "outcomes-r1.json",
                {
                    "schema_version": 2,
                    "source": self.source,
                    "record_id": "table-tennis-results:2026-01-01",
                    "revision_id": "results-r1",
                    "revision": 1,
                    "revision_kind": "initial",
                    "recorded_at": "2026-01-01T11:55:00+00:00",
                    "quote_outcomes": {self.quote_key: "win"},
                },
            )
            provenance = self._provenance(
                schema_version=2,
                source_record=source_record,
            )
            results = {
                "quote_outcomes": {self.quote_key: "win"},
                "outcome_provenance": provenance,
            }

            original_read_bytes = Path.read_bytes
            reads: dict[Path, int] = {}

            def counted_read_bytes(path: Path) -> bytes:
                reads[path] = reads.get(path, 0) + 1
                return original_read_bytes(path)

            with patch.object(Path, "read_bytes", counted_read_bytes):
                normalized = _outcome_provenance(
                    results,
                    source_root=root,
                    reveal_dt=self._dt("2026-01-01T12:15:00+00:00"),
                    imported_dt=self._dt("2026-01-01T12:20:00+00:00"),
                )

            self.assertEqual(reads.get(source_record), 1)
            self.assertIs(normalized["source_record_lineage_verified"], True)
            self.assertEqual(normalized["source_record_id"], "table-tennis-results:2026-01-01")
            self.assertEqual(normalized["source_record_revision_id"], "results-r1")
            self.assertEqual(normalized["source_record_revision"], 1)
            self.assertEqual(normalized["source_record_lineage_depth"], 1)
            self.assertEqual(
                normalized["source_record_lineage_root_sha256"],
                self._sha256(source_record),
            )

    def test_schema_v2_correction_binds_predecessor_and_supersedes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = self._write(
                root / "outcomes-r1.json",
                {
                    "schema_version": 2,
                    "source": self.source,
                    "record_id": "table-tennis-results:2026-01-01",
                    "revision_id": "results-r1",
                    "revision": 1,
                    "revision_kind": "initial",
                    "recorded_at": "2026-01-01T11:30:00+00:00",
                    "quote_outcomes": {self.quote_key: "win"},
                },
            )
            second = self._write(
                root / "outcomes-r2.json",
                {
                    "schema_version": 2,
                    "source": self.source,
                    "record_id": "table-tennis-results:2026-01-01",
                    "revision_id": "results-r2",
                    "revision": 2,
                    "revision_kind": "correction",
                    "recorded_at": "2026-01-01T11:55:00+00:00",
                    "quote_outcomes": {self.quote_key: "void"},
                    "predecessor_record_file": first.name,
                    "predecessor_record_sha256": self._sha256(first),
                    "supersedes_revision_id": "results-r1",
                    "correction_reason": "official result correction",
                },
            )
            provenance = self._provenance(
                schema_version=2,
                source_record=second,
            )
            results = {
                "quote_outcomes": {self.quote_key: "void"},
                "outcome_provenance": provenance,
            }

            normalized = _outcome_provenance(
                results,
                source_root=root,
                reveal_dt=self._dt("2026-01-01T12:15:00+00:00"),
                imported_dt=self._dt("2026-01-01T12:20:00+00:00"),
            )

            self.assertEqual(normalized["source_record_revision_id"], "results-r2")
            self.assertEqual(normalized["source_record_revision"], 2)
            self.assertEqual(normalized["source_record_revision_kind"], "correction")
            self.assertEqual(normalized["source_record_supersedes_revision_id"], "results-r1")
            self.assertEqual(normalized["source_record_predecessor_sha256"], self._sha256(first))
            self.assertEqual(normalized["source_record_lineage_root_revision_id"], "results-r1")
            self.assertEqual(normalized["source_record_lineage_depth"], 2)

    def test_correction_cannot_be_available_before_it_was_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_record = self._write(
                root / "outcomes-r1.json",
                {
                    "schema_version": 2,
                    "source": self.source,
                    "record_id": "table-tennis-results:2026-01-01",
                    "revision_id": "results-r1",
                    "revision": 1,
                    "revision_kind": "initial",
                    "recorded_at": "2026-01-01T12:01:00+00:00",
                    "quote_outcomes": {self.quote_key: "win"},
                },
            )
            provenance = self._provenance(
                schema_version=2,
                source_record=source_record,
                available_at="2026-01-01T12:00:00+00:00",
            )
            results = {
                "quote_outcomes": {self.quote_key: "win"},
                "outcome_provenance": provenance,
            }

            with self.assertRaisesRegex(ValueError, "must not precede source record recorded_at"):
                _outcome_provenance(
                    results,
                    source_root=root,
                    reveal_dt=self._dt("2026-01-01T12:15:00+00:00"),
                    imported_dt=self._dt("2026-01-01T12:20:00+00:00"),
                )

    def test_bool_or_string_provenance_schema_versions_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_record = self._write(
                root / "legacy-outcomes.json",
                {"source": self.source, "quote_outcomes": {self.quote_key: "win"}},
            )
            for invalid in (True, "1", 3):
                provenance = self._provenance(
                    schema_version=1,
                    source_record=source_record,
                )
                provenance["schema_version"] = invalid
                results = {
                    "quote_outcomes": {self.quote_key: "win"},
                    "outcome_provenance": provenance,
                }
                with self.subTest(schema_version=invalid):
                    with self.assertRaisesRegex(ValueError, "schema_version"):
                        _outcome_provenance(
                            results,
                            source_root=root,
                            reveal_dt=self._dt("2026-01-01T12:15:00+00:00"),
                            imported_dt=self._dt("2026-01-01T12:20:00+00:00"),
                        )


if __name__ == "__main__":
    unittest.main()

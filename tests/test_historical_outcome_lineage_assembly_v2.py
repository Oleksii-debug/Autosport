from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from autosport.dataset import load_dataset
from autosport.historical_corpus import assemble_historical_corpus
from autosport.parlayapi_provider import ParlayApiTableTennisProvider


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_snapshot(root: Path) -> tuple[Path, Path]:
    market = root / "snapshot-a.jsonl"
    evidence = root / "snapshot-a.evidence.json"
    event = {
        "event_id": "tt-a",
        "market_id": "winner",
        "selection_id": "alice",
        "decimal_odds": "1.80",
        "observed_ts": "2026-01-01T10:00:01+00:00",
        "source_id": ParlayApiTableTennisProvider.source_id,
        "sequence": 1,
        "market_type": "winner",
        "source_ts": "2026-01-01T10:00:00+00:00",
        "ingest_ts": "2026-01-02T00:00:00+00:00",
        "metadata": {
            "bookmaker_key": "book-a",
            "source_time_semantics": "provider_quote_last_update",
        },
    }
    market.write_text(json.dumps(event, sort_keys=True) + "\n", encoding="utf-8")
    evidence.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "parlayapi_point_in_time_historical_snapshot",
                "provider": "parlayapi",
                "sport_key": "table_tennis",
                "requested_at": "2026-01-01T10:00:30+00:00",
                "snapshot_at": "2026-01-01T10:00:20+00:00",
                "captured_at": "2026-01-02T00:00:00+00:00",
                "response_sha256": "1" * 64,
                "market_sha256": _sha256(market),
                "quote_count": 1,
                "has_data": True,
                "market_types": ["winner"],
                "bookmaker_keys": ["book-a"],
                "snapshot_timestamp_fallback_count": 0,
                "point_in_time_snapshot_contains_odds": True,
                "point_in_time_odds_market_coverage_verified": False,
                "historical_window_market_coverage_verified": False,
                "sealed_outcomes_present": False,
                "replay_corpus_ready": False,
                "terms_reference": "https://parlay-api.com/terms",
                "licensing_or_retention_verified": False,
                "redistribution_verified": False,
                "real_money_execution": False,
                "human_tested": False,
                "nvda_verified": False,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return market, evidence


def _write_governance(root: Path) -> Path:
    claims = {
        "source_identity": "parlayapi:account-entitlement-2026-01",
        "source_ids": [ParlayApiTableTennisProvider.source_id],
        "terms_reference": "https://parlay-api.com/terms",
        "retention_basis": "verified fixture extension through 2026-12-31",
        "retention_expires_at": "2026-12-31T00:00:00+00:00",
        "authorization_valid_through": "2026-12-31T00:00:00+00:00",
        "retention_extension_authority_reference": "test-extension-authority-record",
        "redistribution_policy": "internal_only",
        "licensing_or_retention_verified": True,
        "redistribution_verified": False,
        "authority_reference": "non-secret-entitlement-record-2026-01",
        "verified_at": "2026-01-02T00:01:00+00:00",
    }
    authority = root / "governance-authority.json"
    authority.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "historical_corpus_governance_authority_record",
                **claims,
                "evidence_reference": "test-only corpus governance authority",
                "verification_method": "deterministic test fixture",
                "recorded_by": "test-suite",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    proof = root / "governance-proof.json"
    proof.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "historical_corpus_governance_proof",
                **claims,
                "authority_record_file": authority.name,
                "authority_record_sha256": _sha256(authority),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return proof


def _write_corrected_results(root: Path) -> tuple[Path, Path, Path]:
    quote_key = "tt-a|winner|alice"
    first = root / "official-outcomes-r1.json"
    first.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "source": "official-results:test-fixture",
                "record_id": "table-tennis-results:2026-01-01",
                "revision_id": "results-r1",
                "revision": 1,
                "revision_kind": "initial",
                "recorded_at": "2026-01-01T11:30:00+00:00",
                "quote_outcomes": {quote_key: "win"},
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    second = root / "official-outcomes-r2.json"
    second.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "source": "official-results:test-fixture",
                "record_id": "table-tennis-results:2026-01-01",
                "revision_id": "results-r2",
                "revision": 2,
                "revision_kind": "correction",
                "recorded_at": "2026-01-01T11:55:00+00:00",
                "quote_outcomes": {quote_key: "void"},
                "predecessor_record_file": first.name,
                "predecessor_record_sha256": _sha256(first),
                "supersedes_revision_id": "results-r1",
                "correction_reason": "official result correction",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    results = root / "sealed-results.json"
    results.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "quote_outcomes": {quote_key: "void"},
                "outcome_provenance": {
                    "schema_version": 2,
                    "kind": "historical_outcome_provenance",
                    "source_identity": "official-results:test-fixture",
                    "source_record_file": second.name,
                    "source_record_sha256": _sha256(second),
                    "terms_reference": "https://example.test/results-terms",
                    "retention_basis": "verified internal research retention for test outcome record",
                    "authority_reference": "test-outcome-authority-record",
                    "available_at": "2026-01-01T12:00:00+00:00",
                    "acquired_at": "2026-01-02T00:02:00+00:00",
                    "verified_at": "2026-01-02T00:03:00+00:00",
                    "licensing_or_retention_verified": True,
                    "redistribution_policy": "internal_only",
                    "redistribution_verified": False,
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return results, first, second


class HistoricalOutcomeLineageV2AssemblyTests(unittest.TestCase):
    def test_correction_lineage_survives_full_assembly_manifest_and_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _write_snapshot(root)
            governance = _write_governance(root)
            results, first, second = _write_corrected_results(root)
            output = root / "corpus"

            build = assemble_historical_corpus(
                [snapshot],
                results_path=results,
                governance_proof_path=governance,
                output_dir=output,
                name="verified corrected TT outcomes",
                outcome_reveal_after="2026-01-01T12:00:00+00:00",
                imported_at="2026-01-02T00:05:00+00:00",
            )
            loaded = load_dataset(output)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            sealed = json.loads((output / "results.json").read_text(encoding="utf-8"))
            evidence = manifest["governance"]["outcome_evidence"]
            provenance = sealed["outcome_provenance"]

            self.assertEqual(loaded.import_identity, build.import_identity)
            self.assertEqual(evidence["source_record_id"], "table-tennis-results:2026-01-01")
            self.assertEqual(evidence["source_record_revision_id"], "results-r2")
            self.assertEqual(evidence["source_record_revision"], 2)
            self.assertEqual(evidence["source_record_revision_kind"], "correction")
            self.assertEqual(evidence["source_record_recorded_at"], "2026-01-01T11:55:00+00:00")
            self.assertEqual(evidence["source_record_predecessor_sha256"], _sha256(first))
            self.assertEqual(evidence["source_record_supersedes_revision_id"], "results-r1")
            self.assertEqual(evidence["source_record_lineage_root_sha256"], _sha256(first))
            self.assertEqual(evidence["source_record_lineage_root_revision_id"], "results-r1")
            self.assertEqual(evidence["source_record_lineage_depth"], 2)
            self.assertEqual(
                [revision["revision_id"] for revision in evidence["source_record_lineage"]],
                ["results-r1", "results-r2"],
            )
            self.assertIs(evidence["source_record_lineage_verified"], True)
            self.assertFalse(evidence["source_record_redistributed"])
            self.assertFalse((output / first.name).exists())
            self.assertFalse((output / second.name).exists())
            self.assertEqual(provenance["source_record_revision_id"], "results-r2")
            self.assertEqual(provenance["source_record_lineage_root_sha256"], _sha256(first))
            self.assertEqual(provenance["source_record_lineage"], evidence["source_record_lineage"])
            self.assertIs(provenance["source_record_lineage_verified"], True)

    def test_three_revision_chain_preserves_every_verified_link_after_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _write_snapshot(root)
            governance = _write_governance(root)
            results, first, second = _write_corrected_results(root)
            quote_key = "tt-a|winner|alice"
            third = root / "official-outcomes-r3.json"
            third.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "source": "official-results:test-fixture",
                        "record_id": "table-tennis-results:2026-01-01",
                        "revision_id": "results-r3",
                        "revision": 3,
                        "revision_kind": "correction",
                        "recorded_at": "2026-01-01T11:58:00+00:00",
                        "quote_outcomes": {quote_key: "loss"},
                        "predecessor_record_file": second.name,
                        "predecessor_record_sha256": _sha256(second),
                        "supersedes_revision_id": "results-r2",
                        "correction_reason": "second official result correction",
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            sealed = json.loads(results.read_text(encoding="utf-8"))
            sealed["quote_outcomes"] = {quote_key: "loss"}
            sealed["outcome_provenance"]["source_record_file"] = third.name
            sealed["outcome_provenance"]["source_record_sha256"] = _sha256(third)
            results.write_text(json.dumps(sealed, sort_keys=True), encoding="utf-8")
            output = root / "corpus-deep"

            assemble_historical_corpus(
                [snapshot],
                results_path=results,
                governance_proof_path=governance,
                output_dir=output,
                name="verified deep corrected TT outcomes",
                outcome_reveal_after="2026-01-01T12:00:00+00:00",
                imported_at="2026-01-02T00:05:00+00:00",
            )

            loaded = load_dataset(output)
            self.assertIsNotNone(loaded.import_identity)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            persisted = json.loads((output / "results.json").read_text(encoding="utf-8"))
            evidence_lineage = manifest["governance"]["outcome_evidence"]["source_record_lineage"]
            provenance_lineage = persisted["outcome_provenance"]["source_record_lineage"]

            self.assertEqual(provenance_lineage, evidence_lineage)
            self.assertEqual(
                evidence_lineage,
                [
                    {
                        "revision_id": "results-r1",
                        "revision": 1,
                        "revision_kind": "initial",
                        "recorded_at": "2026-01-01T11:30:00+00:00",
                        "record_sha256": _sha256(first),
                        "predecessor_record_sha256": None,
                        "supersedes_revision_id": None,
                        "correction_reason": None,
                    },
                    {
                        "revision_id": "results-r2",
                        "revision": 2,
                        "revision_kind": "correction",
                        "recorded_at": "2026-01-01T11:55:00+00:00",
                        "record_sha256": _sha256(second),
                        "predecessor_record_sha256": _sha256(first),
                        "supersedes_revision_id": "results-r1",
                        "correction_reason": "official result correction",
                    },
                    {
                        "revision_id": "results-r3",
                        "revision": 3,
                        "revision_kind": "correction",
                        "recorded_at": "2026-01-01T11:58:00+00:00",
                        "record_sha256": _sha256(third),
                        "predecessor_record_sha256": _sha256(second),
                        "supersedes_revision_id": "results-r2",
                        "correction_reason": "second official result correction",
                    },
                ],
            )
            self.assertEqual(
                manifest["governance"]["outcome_evidence"]["source_record_lineage_depth"],
                len(evidence_lineage),
            )
            for source_record in (first, second, third):
                self.assertFalse((output / source_record.name).exists())

    def test_sealed_results_schema_version_is_exact_non_boolean_integer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _write_snapshot(root)
            governance = _write_governance(root)
            results, _, _ = _write_corrected_results(root)
            canonical = json.loads(results.read_text(encoding="utf-8"))

            for index, invalid in enumerate((True, "1", 1.0), start=1):
                malformed = dict(canonical)
                malformed["schema_version"] = invalid
                results.write_text(json.dumps(malformed, sort_keys=True), encoding="utf-8")
                output = root / f"corpus-invalid-schema-{index}"
                with self.subTest(schema_version=invalid):
                    with self.assertRaisesRegex(ValueError, "exact integer 1"):
                        assemble_historical_corpus(
                            [snapshot],
                            results_path=results,
                            governance_proof_path=governance,
                            output_dir=output,
                            name="invalid schema fixture",
                            outcome_reveal_after="2026-01-01T12:00:00+00:00",
                            imported_at="2026-01-02T00:05:00+00:00",
                        )
                    self.assertFalse(output.exists())

    def test_identical_revision_reassembly_has_identical_import_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _write_snapshot(root)
            governance = _write_governance(root)
            results, _, _ = _write_corrected_results(root)

            builds = []
            for suffix in ("one", "two"):
                builds.append(
                    assemble_historical_corpus(
                        [snapshot],
                        results_path=results,
                        governance_proof_path=governance,
                        output_dir=root / f"corpus-{suffix}",
                        name="verified corrected TT outcomes",
                        outcome_reveal_after="2026-01-01T12:00:00+00:00",
                        imported_at="2026-01-02T00:05:00+00:00",
                    )
                )

            self.assertEqual(builds[0].import_identity, builds[1].import_identity)
            self.assertEqual(builds[0].market_sha256, builds[1].market_sha256)
            self.assertEqual(builds[0].results_sha256, builds[1].results_sha256)


if __name__ == "__main__":
    unittest.main()

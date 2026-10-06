from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from autosport.dataset import load_dataset
from autosport.historical_governance import verify_governance_authority_binding


def _write_bound_pair(
    root: Path,
    *,
    proof_source_ids: list[object],
    authority_source_ids: list[object],
) -> Path:
    authority = {
        "schema_version": 1,
        "kind": "historical_corpus_governance_authority_record",
        "source_identity": "licensed-provider-account:test",
        "source_ids": authority_source_ids,
        "terms_reference": "provider-terms:2026-09-01",
        "retention_basis": "test-only retained internal research authority",
        "retention_expires_at": "2026-12-12T06:00:00Z",
        "authorization_valid_through": "2026-12-12T06:00:00Z",
        "retention_extension_authority_reference": None,
        "authority_reference": "entitlement-record:test",
        "verified_at": "2026-09-13T06:00:00Z",
        "redistribution_policy": "internal_only",
        "redistribution_verified": False,
        "licensing_or_retention_verified": True,
        "evidence_reference": "test authority",
        "verification_method": "deterministic test fixture",
        "recorded_by": "test-suite",
    }
    authority_path = root / "authority-record.json"
    authority_path.write_text(json.dumps(authority, sort_keys=True), encoding="utf-8")

    proof = {
        "schema_version": 1,
        "kind": "historical_corpus_governance_proof",
        "source_identity": authority["source_identity"],
        "source_ids": proof_source_ids,
        "terms_reference": authority["terms_reference"],
        "retention_basis": authority["retention_basis"],
        "retention_expires_at": authority["retention_expires_at"],
        "authorization_valid_through": authority["authorization_valid_through"],
        "retention_extension_authority_reference": authority[
            "retention_extension_authority_reference"
        ],
        "authority_reference": authority["authority_reference"],
        "verified_at": authority["verified_at"],
        "redistribution_policy": authority["redistribution_policy"],
        "redistribution_verified": authority["redistribution_verified"],
        "licensing_or_retention_verified": True,
        "authority_record_file": authority_path.name,
        "authority_record_sha256": hashlib.sha256(authority_path.read_bytes()).hexdigest(),
    }
    proof_path = root / "governance-proof.json"
    proof_path.write_text(json.dumps(proof, sort_keys=True), encoding="utf-8")
    return proof_path


class HistoricalSourceIdTypeTests(unittest.TestCase):
    def test_bound_authority_rejects_non_string_source_id_instead_of_coercing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proof_path = _write_bound_pair(
                Path(tmp),
                proof_source_ids=[7],
                authority_source_ids=["7"],
            )
            with self.assertRaisesRegex(ValueError, "source_ids.*canonical strings"):
                verify_governance_authority_binding(proof_path)

    def test_bound_authority_rejects_padded_source_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proof_path = _write_bound_pair(
                Path(tmp),
                proof_source_ids=[" source-a "],
                authority_source_ids=["source-a"],
            )
            with self.assertRaisesRegex(ValueError, "source_ids.*canonical strings"):
                verify_governance_authority_binding(proof_path)

    def test_historical_manifest_rejects_non_string_source_id_before_payload_access(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expires = "2026-12-01T00:00:00+00:00"
            manifest = {
                "schema_version": 2,
                "dataset_kind": "historical",
                "name": "malformed source id fixture",
                "sport": "table_tennis",
                "market_file": "missing-market.jsonl",
                "results_file": "missing-results.json",
                "market_sha256": "0" * 64,
                "results_sha256": "0" * 64,
                "governance": {
                    "source_identity": "parlayapi:test-source-type",
                    "terms_reference": "https://parlay-api.com/terms",
                    "retention_basis": "test fixture",
                    "retention_expires_at": expires,
                    "authorization_valid_through": expires,
                    "redistribution_policy": "internal_only",
                    "acquired_at": "2026-09-01T00:00:00+00:00",
                    "imported_at": "2026-09-01T00:01:00+00:00",
                    "coverage": {
                        "start_ts": "2026-09-01T00:00:00+00:00",
                        "end_ts": "2026-09-01T00:00:01+00:00",
                        "source_ids": [7],
                        "market_types": ["winner"],
                    },
                    "causality": {
                        "strategy_time_field": "observed_ts",
                        "outcome_reveal_after": "2026-09-01T01:00:00+00:00",
                    },
                    "acquisition_evidence": {
                        "snapshots": [{"captured_at": "2026-09-01T00:00:00+00:00"}],
                        "retention_expires_at": expires,
                        "authorization_valid_through": expires,
                        "retention_extension_authority_reference": None,
                    },
                },
            }
            (root / "manifest.json").write_text(
                json.dumps(manifest, sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(
                ValueError,
                "governance.coverage.source_ids.*canonical strings",
            ):
                load_dataset(root)


if __name__ == "__main__":
    unittest.main()

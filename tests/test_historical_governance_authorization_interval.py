from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from autosport.historical_governance import verify_governance_authority_binding


class HistoricalGovernanceAuthorizationIntervalTests(unittest.TestCase):
    def _write_bound_pair(
        self,
        root: Path,
        *,
        authorization_valid_through: str,
        retention_expires_at: str = "2026-12-12T06:00:00Z",
    ) -> Path:
        bound = {
            "source_identity": "licensed-provider-account:test",
            "source_ids": ["parlayapi:table_tennis"],
            "terms_reference": "provider-terms:2026-09-01",
            "retention_basis": "paid archive access retained for internal research",
            "retention_expires_at": retention_expires_at,
            "authorization_valid_through": authorization_valid_through,
            "retention_extension_authority_reference": "provider-consent:test-456",
            "authority_reference": "entitlement-record:test-123",
            "verified_at": "2026-09-13T06:00:00Z",
            "redistribution_policy": "internal_only",
            "redistribution_verified": False,
            "licensing_or_retention_verified": True,
        }
        authority = {
            "schema_version": 1,
            "kind": "historical_corpus_governance_authority_record",
            **bound,
            "evidence_reference": "account entitlement receipt retained outside the corpus",
            "verification_method": "human-reviewed provider entitlement record",
            "recorded_by": "release-owner",
        }
        authority_path = root / "authority-record.json"
        authority_path.write_text(
            json.dumps(authority, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        proof = {
            "schema_version": 1,
            "kind": "historical_corpus_governance_proof",
            **bound,
            "authority_record_file": authority_path.name,
            "authority_record_sha256": hashlib.sha256(authority_path.read_bytes()).hexdigest(),
        }
        proof_path = root / "governance-proof.json"
        proof_path.write_text(
            json.dumps(proof, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        return proof_path

    def test_authorization_must_cover_claimed_retention_interval(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proof_path = self._write_bound_pair(
                Path(tmp),
                authorization_valid_through="2026-10-01T00:00:00Z",
            )
            with self.assertRaisesRegex(
                ValueError,
                "authorization_valid_through.*retention_expires_at",
            ):
                verify_governance_authority_binding(proof_path)

    def test_authorization_covering_retention_interval_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proof_path = self._write_bound_pair(
                Path(tmp),
                authorization_valid_through="2026-12-12T06:00:00Z",
            )
            binding = verify_governance_authority_binding(proof_path)
            self.assertEqual(binding.governance_proof, str(proof_path))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from autosport.historical_governance import verify_governance_authority_binding


class GovernanceAuthorityPresenceTests(unittest.TestCase):
    def _pair(self, root: Path) -> tuple[Path, Path]:
        authority = {
            "schema_version": 1,
            "kind": "historical_corpus_governance_authority_record",
            "source_identity": "licensed-provider-account:test",
            "source_ids": ["parlayapi:table_tennis"],
            "terms_reference": "provider-terms:2026-09-01",
            "retention_basis": "internal research retention",
            "retention_expires_at": "2026-12-12T06:00:00Z",
            "retention_extension_authority_reference": "provider-consent:test-456",
            "authority_reference": "entitlement-record:test-123",
            "verified_at": "2026-09-13T06:00:00Z",
            "redistribution_policy": "internal_only",
            "redistribution_verified": False,
            "licensing_or_retention_verified": True,
            "evidence_reference": "external entitlement evidence",
            "verification_method": "human-reviewed entitlement record",
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
            **{
                field: authority[field]
                for field in (
                    "source_identity",
                    "source_ids",
                    "terms_reference",
                    "retention_basis",
                    "retention_expires_at",
                    "retention_extension_authority_reference",
                    "authority_reference",
                    "verified_at",
                    "redistribution_policy",
                    "redistribution_verified",
                    "licensing_or_retention_verified",
                )
            },
            "authority_record_file": authority_path.name,
            "authority_record_sha256": hashlib.sha256(authority_path.read_bytes()).hexdigest(),
        }
        proof_path = root / "governance-proof.json"
        proof_path.write_text(
            json.dumps(proof, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        return proof_path, authority_path

    def test_absent_and_explicit_null_bound_claims_are_not_equivalent(self) -> None:
        for proof_has_null in (False, True):
            with self.subTest(proof_has_null=proof_has_null), tempfile.TemporaryDirectory() as tmp:
                proof_path, authority_path = self._pair(Path(tmp))
                proof = json.loads(proof_path.read_text(encoding="utf-8"))
                authority = json.loads(authority_path.read_text(encoding="utf-8"))

                if proof_has_null:
                    proof["retention_expires_at"] = None
                    authority.pop("retention_expires_at")
                else:
                    proof.pop("retention_expires_at")
                    authority["retention_expires_at"] = None

                authority_path.write_text(
                    json.dumps(authority, ensure_ascii=False, sort_keys=True),
                    encoding="utf-8",
                )
                proof["authority_record_sha256"] = hashlib.sha256(
                    authority_path.read_bytes()
                ).hexdigest()
                proof_path.write_text(
                    json.dumps(proof, ensure_ascii=False, sort_keys=True),
                    encoding="utf-8",
                )

                with self.assertRaisesRegex(ValueError, "retention_expires_at presence"):
                    verify_governance_authority_binding(proof_path)


if __name__ == "__main__":
    unittest.main()

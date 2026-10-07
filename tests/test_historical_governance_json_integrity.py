from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from autosport.historical_governance import verify_governance_authority_binding


class HistoricalGovernanceJsonIntegrityTests(unittest.TestCase):
    def _write_valid_pair(self, root: Path) -> tuple[Path, Path]:
        authority = {
            "schema_version": 1,
            "kind": "historical_corpus_governance_authority_record",
            "source_identity": "licensed-provider-account:test",
            "source_ids": ["licensed:test"],
            "terms_reference": "provider-terms:test",
            "licensing_or_retention_verified": True,
            "evidence_reference": "entitlement:test",
            "verification_method": "human-reviewed test fixture",
            "recorded_by": "test",
        }
        authority_path = root / "authority.json"
        authority_path.write_text(
            json.dumps(authority, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        proof = {
            "schema_version": 1,
            "kind": "historical_corpus_governance_proof",
            "source_identity": authority["source_identity"],
            "source_ids": authority["source_ids"],
            "terms_reference": authority["terms_reference"],
            "licensing_or_retention_verified": True,
            "authority_record_file": authority_path.name,
            "authority_record_sha256": hashlib.sha256(authority_path.read_bytes()).hexdigest(),
        }
        proof_path = root / "proof.json"
        proof_path.write_text(
            json.dumps(proof, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        return proof_path, authority_path

    def test_valid_minimal_binding_still_passes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proof_path, _authority_path = self._write_valid_pair(Path(tmp))
            binding = verify_governance_authority_binding(proof_path)
            self.assertEqual(binding.recorded_by, "test")

    def test_duplicate_proof_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proof_path, _authority_path = self._write_valid_pair(Path(tmp))
            text = proof_path.read_text(encoding="utf-8")
            ambiguous = text.replace(
                '"licensing_or_retention_verified": true',
                '"licensing_or_retention_verified": false, '
                '"licensing_or_retention_verified": true',
            )
            self.assertNotEqual(text, ambiguous)
            proof_path.write_text(ambiguous, encoding="utf-8")

            with self.assertRaisesRegex(
                ValueError,
                "duplicate JSON object key: licensing_or_retention_verified",
            ):
                verify_governance_authority_binding(proof_path)

    def test_hash_bound_duplicate_authority_key_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proof_path, authority_path = self._write_valid_pair(Path(tmp))
            authority = authority_path.read_text(encoding="utf-8")
            ambiguous = authority.replace(
                '"licensing_or_retention_verified": true',
                '"licensing_or_retention_verified": false, '
                '"licensing_or_retention_verified": true',
            )
            self.assertNotEqual(authority, ambiguous)
            authority_path.write_text(ambiguous, encoding="utf-8")

            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            proof["authority_record_sha256"] = hashlib.sha256(
                authority_path.read_bytes()
            ).hexdigest()
            proof_path.write_text(
                json.dumps(proof, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "duplicate JSON object key: licensing_or_retention_verified",
            ):
                verify_governance_authority_binding(proof_path)

    def test_nonfinite_json_constant_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proof_path, _authority_path = self._write_valid_pair(Path(tmp))
            text = proof_path.read_text(encoding="utf-8")
            ambiguous = text[:-1] + ', "diagnostic": NaN}'
            proof_path.write_text(ambiguous, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "non-finite JSON number: NaN"):
                verify_governance_authority_binding(proof_path)

    def test_distinct_overflowing_bound_numbers_do_not_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proof_path, authority_path = self._write_valid_pair(Path(tmp))

            authority = authority_path.read_text(encoding="utf-8")
            authority_path.write_text(
                authority[:-1] + ', "retention_basis": 9e999}',
                encoding="utf-8",
            )

            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            proof["authority_record_sha256"] = hashlib.sha256(
                authority_path.read_bytes()
            ).hexdigest()
            proof_path.write_text(
                json.dumps(proof, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            proof_text = proof_path.read_text(encoding="utf-8")
            proof_path.write_text(
                proof_text[:-1] + ', "retention_basis": 1e400}',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "governance proof.retention_basis does not match authority evidence artifact",
            ):
                verify_governance_authority_binding(proof_path)

    def test_bound_boolean_does_not_alias_numeric_authority_claim(self) -> None:
        for proof_value, authority_number in ((True, "1.0"), (False, "0.0")):
            with self.subTest(proof_value=proof_value, authority_number=authority_number):
                with tempfile.TemporaryDirectory() as tmp:
                    proof_path, authority_path = self._write_valid_pair(Path(tmp))

                    authority = authority_path.read_text(encoding="utf-8")
                    authority_path.write_text(
                        authority[:-1]
                        + f', "redistribution_verified": {authority_number}}}',
                        encoding="utf-8",
                    )

                    proof = json.loads(proof_path.read_text(encoding="utf-8"))
                    proof["redistribution_verified"] = proof_value
                    proof["authority_record_sha256"] = hashlib.sha256(
                        authority_path.read_bytes()
                    ).hexdigest()
                    proof_path.write_text(
                        json.dumps(proof, ensure_ascii=False, sort_keys=True),
                        encoding="utf-8",
                    )

                    with self.assertRaisesRegex(
                        ValueError,
                        "governance proof.redistribution_verified does not match authority evidence artifact",
                    ):
                        verify_governance_authority_binding(proof_path)

    def test_schema_version_requires_exact_json_integer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for raw_version in ("true", '"1"', "1.0"):
                with self.subTest(raw_version=raw_version):
                    proof_path, _authority_path = self._write_valid_pair(root)
                    text = proof_path.read_text(encoding="utf-8")
                    replaced = text.replace('"schema_version": 1', f'"schema_version": {raw_version}')
                    self.assertNotEqual(text, replaced)
                    proof_path.write_text(replaced, encoding="utf-8")
                    with self.assertRaisesRegex(
                        ValueError,
                        "governance proof schema_version must be 1",
                    ):
                        verify_governance_authority_binding(proof_path)

    def test_authority_schema_version_requires_exact_json_integer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proof_path, authority_path = self._write_valid_pair(Path(tmp))
            authority = authority_path.read_text(encoding="utf-8")
            replaced = authority.replace('"schema_version": 1', '"schema_version": true')
            self.assertNotEqual(authority, replaced)
            authority_path.write_text(replaced, encoding="utf-8")

            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            proof["authority_record_sha256"] = hashlib.sha256(
                authority_path.read_bytes()
            ).hexdigest()
            proof_path.write_text(
                json.dumps(proof, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "governance authority record schema_version must be 1",
            ):
                verify_governance_authority_binding(proof_path)


if __name__ == "__main__":
    unittest.main()

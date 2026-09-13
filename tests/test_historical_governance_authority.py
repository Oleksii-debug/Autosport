from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.historical_governance import (
    bundle_corpus_main,
    corpus_main,
    verify_governance_authority_binding,
)


class HistoricalGovernanceAuthorityTests(unittest.TestCase):
    def _write_bound_pair(self, root: Path) -> tuple[Path, Path]:
        authority = {
            "schema_version": 1,
            "kind": "historical_corpus_governance_authority_record",
            "source_identity": "licensed-provider-account:test",
            "source_ids": ["parlayapi:table_tennis"],
            "terms_reference": "provider-terms:2026-09-01",
            "retention_basis": "paid archive access retained for internal research",
            "retention_expires_at": "2026-12-12T06:00:00Z",
            "retention_extension_authority_reference": "provider-consent:test-456",
            "authority_reference": "entitlement-record:test-123",
            "verified_at": "2026-09-13T06:00:00Z",
            "redistribution_policy": "internal_only",
            "redistribution_verified": False,
            "licensing_or_retention_verified": True,
            "evidence_reference": "account entitlement receipt retained outside the corpus",
            "verification_method": "human-reviewed provider entitlement record",
            "recorded_by": "release-owner",
        }
        authority_path = root / "authority-record.json"
        authority_path.write_text(
            json.dumps(authority, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        authority_sha = hashlib.sha256(authority_path.read_bytes()).hexdigest()
        proof = {
            "schema_version": 1,
            "kind": "historical_corpus_governance_proof",
            "source_identity": authority["source_identity"],
            "source_ids": authority["source_ids"],
            "terms_reference": authority["terms_reference"],
            "retention_basis": authority["retention_basis"],
            "retention_expires_at": authority["retention_expires_at"],
            "retention_extension_authority_reference": authority[
                "retention_extension_authority_reference"
            ],
            "authority_reference": authority["authority_reference"],
            "verified_at": authority["verified_at"],
            "redistribution_policy": authority["redistribution_policy"],
            "redistribution_verified": authority["redistribution_verified"],
            "licensing_or_retention_verified": True,
            "authority_record_file": authority_path.name,
            "authority_record_sha256": authority_sha,
        }
        proof_path = root / "governance-proof.json"
        proof_path.write_text(
            json.dumps(proof, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        return proof_path, authority_path

    @staticmethod
    def _governance_argument(args: list[str]) -> Path:
        for index, value in enumerate(args):
            if value == "--governance-proof":
                return Path(args[index + 1])
            if value.startswith("--governance-proof="):
                return Path(value.split("=", 1)[1])
        raise AssertionError("delegated args did not contain --governance-proof")

    def test_valid_binding_hashes_and_matches_claims(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proof_path, authority_path = self._write_bound_pair(Path(tmp))
            binding = verify_governance_authority_binding(proof_path)
            self.assertEqual(
                binding.governance_proof_sha256,
                hashlib.sha256(proof_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                binding.authority_record_sha256,
                hashlib.sha256(authority_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(binding.recorded_by, "release-owner")

    def test_binding_reads_each_source_artifact_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proof_path, authority_path = self._write_bound_pair(Path(tmp))
            original_read_bytes = Path.read_bytes
            reads: list[Path] = []

            def tracked_read_bytes(path: Path) -> bytes:
                reads.append(path)
                return original_read_bytes(path)

            with patch.object(Path, "read_bytes", tracked_read_bytes):
                verify_governance_authority_binding(proof_path)

            self.assertEqual(reads.count(proof_path), 1)
            self.assertEqual(reads.count(authority_path), 1)

    def test_bare_self_declared_proof_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proof_path, _ = self._write_bound_pair(Path(tmp))
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            proof.pop("authority_record_file")
            proof.pop("authority_record_sha256")
            proof_path.write_text(json.dumps(proof), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "authority_record_file"):
                verify_governance_authority_binding(proof_path)

    def test_authority_record_hash_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proof_path, authority_path = self._write_bound_pair(Path(tmp))
            authority = json.loads(authority_path.read_text(encoding="utf-8"))
            authority["retention_basis"] = "tampered after proof was recorded"
            authority_path.write_text(json.dumps(authority), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "does not match authority evidence artifact"):
                verify_governance_authority_binding(proof_path)

    def test_hash_bound_authority_claim_mismatch_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proof_path, authority_path = self._write_bound_pair(Path(tmp))
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            authority = json.loads(authority_path.read_text(encoding="utf-8"))
            authority["redistribution_policy"] = "prohibited"
            authority_path.write_text(
                json.dumps(authority, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            proof["authority_record_sha256"] = hashlib.sha256(authority_path.read_bytes()).hexdigest()
            proof_path.write_text(json.dumps(proof), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "redistribution_policy"):
                verify_governance_authority_binding(proof_path)

    def test_retention_expiry_must_match_hash_bound_authority_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proof_path, authority_path = self._write_bound_pair(Path(tmp))
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            authority = json.loads(authority_path.read_text(encoding="utf-8"))
            authority["retention_expires_at"] = "2027-01-01T00:00:00Z"
            authority_path.write_text(
                json.dumps(authority, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            proof["authority_record_sha256"] = hashlib.sha256(authority_path.read_bytes()).hexdigest()
            proof_path.write_text(json.dumps(proof), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "retention_expires_at"):
                verify_governance_authority_binding(proof_path)

    def test_retention_extension_reference_must_match_hash_bound_authority_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proof_path, authority_path = self._write_bound_pair(Path(tmp))
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            authority = json.loads(authority_path.read_text(encoding="utf-8"))
            authority["retention_extension_authority_reference"] = "provider-consent:changed"
            authority_path.write_text(
                json.dumps(authority, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )
            proof["authority_record_sha256"] = hashlib.sha256(authority_path.read_bytes()).hexdigest()
            proof_path.write_text(json.dumps(proof), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "retention_extension_authority_reference"):
                verify_governance_authority_binding(proof_path)

    def test_authority_record_must_be_direct_sibling(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proof_path, authority_path = self._write_bound_pair(root)
            nested = root / "nested"
            nested.mkdir()
            moved = nested / authority_path.name
            moved.write_bytes(authority_path.read_bytes())
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            proof["authority_record_file"] = f"nested/{authority_path.name}"
            proof["authority_record_sha256"] = hashlib.sha256(moved.read_bytes()).hexdigest()
            proof_path.write_text(json.dumps(proof), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "direct sibling"):
                verify_governance_authority_binding(proof_path)

    def test_canonical_corpus_entrypoint_refuses_unbound_governance_before_delegation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proof_path, _ = self._write_bound_pair(Path(tmp))
            proof = json.loads(proof_path.read_text(encoding="utf-8"))
            proof.pop("authority_record_file")
            proof.pop("authority_record_sha256")
            proof_path.write_text(json.dumps(proof), encoding="utf-8")
            with patch("autosport.historical_corpus.main") as delegated:
                rc = corpus_main(["--governance-proof", str(proof_path)])
            self.assertEqual(rc, 3)
            delegated.assert_not_called()

    def test_both_wrappers_delegate_only_frozen_verified_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proof_path, authority_path = self._write_bound_pair(Path(tmp))
            expected_proof = proof_path.read_bytes()
            expected_authority = authority_path.read_bytes()
            args = ["--governance-proof", str(proof_path), "--sentinel"]

            def assert_frozen(delegated_args: list[str], result: int) -> int:
                frozen_proof = self._governance_argument(delegated_args)
                self.assertNotEqual(frozen_proof, proof_path)
                self.assertEqual(frozen_proof.read_bytes(), expected_proof)
                self.assertEqual(
                    (frozen_proof.parent / authority_path.name).read_bytes(),
                    expected_authority,
                )
                self.assertIn("--sentinel", delegated_args)
                return result

            with patch(
                "autosport.historical_corpus.main",
                side_effect=lambda delegated_args: assert_frozen(delegated_args, 17),
            ) as delegated:
                self.assertEqual(corpus_main(args), 17)
                delegated.assert_called_once()
            with patch(
                "autosport.historical_bundle_corpus.main",
                side_effect=lambda delegated_args: assert_frozen(delegated_args, 23),
            ) as delegated:
                self.assertEqual(bundle_corpus_main(args), 23)
                delegated.assert_called_once()

    def test_source_replacement_after_verification_cannot_change_delegated_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proof_path, authority_path = self._write_bound_pair(Path(tmp))
            expected_proof = proof_path.read_bytes()
            expected_authority = authority_path.read_bytes()
            expected_proof_sha = hashlib.sha256(expected_proof).hexdigest()
            args = ["--governance-proof", str(proof_path)]

            def replace_originals_then_assert_frozen(delegated_args: list[str]) -> int:
                proof_path.write_text('{"tampered": true}', encoding="utf-8")
                authority_path.write_text('{"tampered": true}', encoding="utf-8")
                frozen_proof = self._governance_argument(delegated_args)
                frozen_authority = frozen_proof.parent / authority_path.name
                self.assertEqual(frozen_proof.read_bytes(), expected_proof)
                self.assertEqual(frozen_authority.read_bytes(), expected_authority)
                binding = verify_governance_authority_binding(frozen_proof)
                self.assertEqual(binding.governance_proof_sha256, expected_proof_sha)
                return 31

            with patch(
                "autosport.historical_corpus.main",
                side_effect=replace_originals_then_assert_frozen,
            ) as delegated:
                self.assertEqual(corpus_main(args), 31)
                delegated.assert_called_once()

    def test_help_bypasses_governance_gate_without_running_a_build(self) -> None:
        for entrypoint, target in (
            (corpus_main, "autosport.historical_corpus.main"),
            (bundle_corpus_main, "autosport.historical_bundle_corpus.main"),
        ):
            with self.subTest(target=target):
                with patch(target, return_value=0) as delegated:
                    self.assertEqual(entrypoint(["--help"]), 0)
                    delegated.assert_called_once_with(["--help"])

    def test_repeated_governance_proof_is_rejected_before_delegation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            proof_path, _ = self._write_bound_pair(Path(tmp))
            args = [
                "--governance-proof",
                str(proof_path),
                f"--governance-proof={proof_path}",
            ]
            for entrypoint, target in (
                (corpus_main, "autosport.historical_corpus.main"),
                (bundle_corpus_main, "autosport.historical_bundle_corpus.main"),
            ):
                with self.subTest(target=target):
                    with patch(target) as delegated:
                        self.assertEqual(entrypoint(args), 3)
                        delegated.assert_not_called()


if __name__ == "__main__":
    unittest.main()

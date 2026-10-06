from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autosport.historical_corpus as corpus


def _write_bound_proof(root: Path, *, verified_at: str) -> Path:
    authority = {
        "schema_version": 1,
        "kind": "historical_corpus_governance_authority_record",
        "source_identity": "test-provider:chronology-fixture",
        "source_ids": ["test-provider:table_tennis"],
        "terms_reference": "https://example.test/terms",
        "retention_basis": "verified test-only internal research retention",
        "retention_expires_at": "2026-12-31T00:00:00+00:00",
        "authorization_valid_through": "2026-12-31T00:00:00+00:00",
        "authority_reference": "test-authority:chronology-fixture",
        "verified_at": verified_at,
        "redistribution_policy": "internal_only",
        "redistribution_verified": False,
        "licensing_or_retention_verified": True,
        "evidence_reference": "test-only bound authority evidence",
        "verification_method": "deterministic test fixture",
        "recorded_by": "test-suite",
    }
    authority_path = root / "authority-record.json"
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
        "retention_basis": authority["retention_basis"],
        "retention_expires_at": authority["retention_expires_at"],
        "authorization_valid_through": authority["authorization_valid_through"],
        "authority_reference": authority["authority_reference"],
        "verified_at": authority["verified_at"],
        "redistribution_policy": authority["redistribution_policy"],
        "redistribution_verified": authority["redistribution_verified"],
        "licensing_or_retention_verified": True,
        "authority_record_file": authority_path.name,
        "authority_record_sha256": hashlib.sha256(authority_path.read_bytes()).hexdigest(),
    }
    proof_path = root / "governance.json"
    proof_path.write_text(
        json.dumps(proof, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    return proof_path


class GovernanceProofImportChronologyTests(unittest.TestCase):
    def test_future_governance_verification_fails_before_snapshot_or_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "corpus"
            proof_path = _write_bound_proof(
                root,
                verified_at="2026-01-02T12:00:00+00:00",
            )
            with patch.object(
                corpus,
                "_snapshot",
                side_effect=AssertionError("snapshot must not be read after impossible governance chronology"),
            ) as snapshot:
                with self.assertRaisesRegex(
                    ValueError,
                    "imported_at must not precede governance proof verification",
                ):
                    corpus.assemble_historical_corpus(
                        [(root / "market.jsonl", root / "evidence.json")],
                        results_path=root / "results.json",
                        governance_proof_path=proof_path,
                        output_dir=output,
                        name="blocked future governance proof",
                        outcome_reveal_after="2026-01-02T09:00:00+00:00",
                        imported_at="2026-01-02T10:00:00+00:00",
                    )

            snapshot.assert_not_called()
            self.assertFalse(output.exists())

    def test_equal_or_earlier_governance_verification_passes_chronology_gate(self) -> None:
        for verified_at in (
            "2026-01-02T10:00:00+00:00",
            "2026-01-02T09:59:59+00:00",
        ):
            with self.subTest(verified_at=verified_at), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                output = root / "corpus"
                proof_path = _write_bound_proof(root, verified_at=verified_at)
                with patch.object(
                    corpus,
                    "_snapshot",
                    side_effect=RuntimeError("downstream-sentinel"),
                ) as snapshot:
                    with self.assertRaisesRegex(RuntimeError, "downstream-sentinel"):
                        corpus.assemble_historical_corpus(
                            [(root / "market.jsonl", root / "evidence.json")],
                            results_path=root / "results.json",
                            governance_proof_path=proof_path,
                            output_dir=output,
                            name="allowed governance proof boundary",
                            outcome_reveal_after="2026-01-02T09:00:00+00:00",
                            imported_at="2026-01-02T10:00:00+00:00",
                        )

                snapshot.assert_called_once()
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()

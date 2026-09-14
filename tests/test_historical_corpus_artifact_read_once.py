from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autosport.historical_corpus as corpus
from autosport.outcome_revision import canonical_outcome_revision_id
from autosport.parlayapi_provider import ParlayApiTableTennisProvider


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_snapshot(root: Path) -> tuple[Path, Path]:
    market = root / "snapshot.jsonl"
    evidence = root / "snapshot.evidence.json"
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
                "market_sha256": _digest(market.read_bytes()),
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
    authority = root / "governance-authority.json"
    bound = {
        "source_identity": "parlayapi:account-entitlement-test",
        "source_ids": [ParlayApiTableTennisProvider.source_id],
        "terms_reference": "https://parlay-api.com/terms",
        "retention_basis": "verified fixture extension through 2026-12-31",
        "retention_expires_at": "2026-12-31T00:00:00+00:00",
        "authorization_valid_through": "2026-12-31T00:00:00+00:00",
        "retention_extension_authority_reference": "fixture-extension-authority",
        "redistribution_policy": "internal_only",
        "licensing_or_retention_verified": True,
        "redistribution_verified": False,
        "authority_reference": "fixture-authority",
        "verified_at": "2026-01-02T00:01:00+00:00",
    }
    authority_payload = {
        "schema_version": 1,
        "kind": "historical_corpus_governance_authority_record",
        **bound,
        "evidence_reference": "test-only read-once governance authority",
        "verification_method": "deterministic test fixture",
        "recorded_by": "test-suite",
    }
    authority.write_text(
        json.dumps(authority_payload, sort_keys=True),
        encoding="utf-8",
    )
    path = root / "governance-proof.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "historical_corpus_governance_proof",
                **bound,
                "authority_record_file": authority.name,
                "authority_record_sha256": _digest(authority.read_bytes()),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def _write_results(root: Path) -> tuple[Path, Path]:
    source = root / "official-outcome-source-record.json"
    outcomes = {"tt-a|winner|alice": "win"}
    effective_at = "2026-01-01T10:59:00Z"
    revision_id = canonical_outcome_revision_id(
        source_identity="official-results:test-fixture",
        kind="initial",
        effective_at=effective_at,
        quote_outcomes=outcomes,
    )
    source.write_text(
        json.dumps(
            {
                "source": "official-results:test-fixture",
                "quote_outcomes": outcomes,
                "fixture_only": True,
                "revision": {
                    "schema_version": 1,
                    "kind": "initial",
                    "effective_at": effective_at,
                    "revision_id": revision_id,
                    "supersedes_revision_id": None,
                    "predecessor_record_file": None,
                    "predecessor_record_sha256": None,
                },
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
                "quote_outcomes": outcomes,
                "outcome_provenance": {
                    "schema_version": 1,
                    "kind": "historical_outcome_provenance",
                    "source_identity": "official-results:test-fixture",
                    "source_record_file": source.name,
                    "source_record_sha256": _digest(source.read_bytes()),
                    "outcome_revision_id": revision_id,
                    "outcome_lineage_root_revision_id": revision_id,
                    "terms_reference": "https://example.test/results-terms",
                    "retention_basis": "verified internal research retention fixture",
                    "authority_reference": "fixture-outcome-authority",
                    "available_at": "2026-01-01T11:00:00+00:00",
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
    return results, source


def _assemble(
    root: Path,
    *,
    snapshot: tuple[Path, Path],
    results: Path,
    proof: Path,
) -> Path:
    output = root / "corpus"
    corpus.assemble_historical_corpus(
        [snapshot],
        results_path=results,
        governance_proof_path=proof,
        output_dir=output,
        name="read-once integrity fixture",
        outcome_reveal_after="2026-01-01T11:00:00+00:00",
        imported_at="2026-01-02T00:05:00+00:00",
    )
    return output


class HistoricalCorpusArtifactReadOnceTests(unittest.TestCase):
    def test_governance_digest_stays_bound_to_bytes_that_were_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _write_snapshot(root)
            results, _ = _write_results(root)
            proof = _write_governance(root)
            original = proof.read_bytes()
            replacement = json.loads(original.decode("utf-8"))
            replacement["retention_basis"] = "replacement after verification"

            real_governance = corpus._governance_proof

            def verify_then_replace(path: Path, *, payload: bytes | None = None) -> dict:
                parsed = real_governance(path, payload=payload)
                proof.write_text(json.dumps(replacement, sort_keys=True), encoding="utf-8")
                return parsed

            with patch.object(corpus, "_governance_proof", side_effect=verify_then_replace):
                output = _assemble(
                    root,
                    snapshot=snapshot,
                    results=results,
                    proof=proof,
                )

            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            recorded = manifest["governance"]["acquisition_evidence"]["governance_proof_sha256"]
            self.assertEqual(recorded, _digest(original))
            self.assertNotEqual(recorded, _digest(proof.read_bytes()))

    def test_snapshot_evidence_digest_stays_bound_to_bytes_that_were_parsed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _write_snapshot(root)
            market, evidence_path = snapshot
            original = evidence_path.read_bytes()
            replacement = json.loads(original.decode("utf-8"))
            replacement["response_sha256"] = "2" * 64
            results, _ = _write_results(root)
            proof = _write_governance(root)

            real_snapshot = corpus._snapshot

            def verify_then_replace(
                market_path: Path,
                evidence: Path,
                *,
                expected_terms_reference: str,
            ):
                rows, parsed = real_snapshot(
                    market_path,
                    evidence,
                    expected_terms_reference=expected_terms_reference,
                )
                evidence_path.write_text(
                    json.dumps(replacement, sort_keys=True),
                    encoding="utf-8",
                )
                return rows, parsed

            with patch.object(corpus, "_snapshot", side_effect=verify_then_replace):
                output = _assemble(
                    root,
                    snapshot=(market, evidence_path),
                    results=results,
                    proof=proof,
                )

            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            recorded = manifest["governance"]["acquisition_evidence"]["snapshots"][0][
                "evidence_sha256"
            ]
            self.assertEqual(recorded, _digest(original))
            self.assertNotEqual(recorded, _digest(evidence_path.read_bytes()))

    def test_snapshot_market_is_parsed_from_same_bytes_that_were_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            market, evidence = _write_snapshot(root)
            original = market.read_bytes()
            replacement = json.loads(original.decode("utf-8"))
            replacement["selection_id"] = "mallory"
            replacement_bytes = (
                json.dumps(replacement, sort_keys=True) + "\n"
            ).encode("utf-8")
            mutated = False
            real_read = corpus._read_bytes

            def read_then_replace(path: Path, *, context: str) -> bytes:
                nonlocal mutated
                payload = real_read(path, context=context)
                if path == market and not mutated:
                    market.write_bytes(replacement_bytes)
                    mutated = True
                return payload

            with patch.object(corpus, "_read_bytes", side_effect=read_then_replace):
                rows, _ = corpus._snapshot(
                    market,
                    evidence,
                    expected_terms_reference="https://parlay-api.com/terms",
                )

            self.assertTrue(mutated)
            self.assertEqual(rows[0][0].selection_id, "alice")
            self.assertEqual(market.read_bytes(), replacement_bytes)

    def test_outcome_source_is_parsed_from_same_bytes_that_were_hashed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _write_snapshot(root)
            results, source = _write_results(root)
            proof = _write_governance(root)
            original = source.read_bytes()
            replacement = json.loads(original.decode("utf-8"))
            replacement["quote_outcomes"]["tt-a|winner|alice"] = "loss"
            replacement_bytes = json.dumps(replacement, sort_keys=True).encode("utf-8")
            mutated = False
            real_read = corpus._read_bytes

            def read_then_replace(path: Path, *, context: str) -> bytes:
                nonlocal mutated
                payload = real_read(path, context=context)
                if path == source and not mutated:
                    source.write_bytes(replacement_bytes)
                    mutated = True
                return payload

            with patch.object(corpus, "_read_bytes", side_effect=read_then_replace):
                output = _assemble(
                    root,
                    snapshot=snapshot,
                    results=results,
                    proof=proof,
                )

            self.assertTrue(mutated)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            recorded = manifest["governance"]["outcome_evidence"]["source_record_sha256"]
            self.assertEqual(recorded, _digest(original))
            self.assertNotEqual(recorded, _digest(source.read_bytes()))


if __name__ == "__main__":
    unittest.main()

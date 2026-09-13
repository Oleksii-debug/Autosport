import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autosport.historical_corpus as corpus
from autosport.parlayapi_provider import ParlayApiTableTennisProvider


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _write_fixture(root: Path) -> tuple[Path, Path, Path, Path, Path]:
    market = root / "snapshot.jsonl"
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

    evidence = root / "snapshot.evidence.json"
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
                "market_sha256": _sha256_bytes(market.read_bytes()),
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

    source_record = root / "official-outcome-source-record.json"
    quote_outcomes = {"tt-a|winner|alice": "win"}
    source_record.write_text(
        json.dumps(
            {
                "source": "official-results:test-fixture",
                "events": ["tt-a"],
                "quote_outcomes": quote_outcomes,
                "fixture_only": True,
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
                "quote_outcomes": quote_outcomes,
                "outcome_provenance": {
                    "schema_version": 1,
                    "kind": "historical_outcome_provenance",
                    "source_identity": "official-results:test-fixture",
                    "source_record_file": source_record.name,
                    "source_record_sha256": _sha256_bytes(source_record.read_bytes()),
                    "terms_reference": "https://example.test/results-terms",
                    "retention_basis": "verified internal research retention for test outcome record",
                    "authority_reference": "test-outcome-authority-record",
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

    proof = root / "governance-proof.json"
    proof.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "kind": "historical_corpus_governance_proof",
                "source_identity": "parlayapi:account-entitlement-2026-01",
                "source_ids": [ParlayApiTableTennisProvider.source_id],
                "terms_reference": "https://parlay-api.com/terms",
                "retention_basis": "verified internal research retention authority through 2026-04-01",
                "redistribution_policy": "internal_only",
                "licensing_or_retention_verified": True,
                "redistribution_verified": False,
                "authority_reference": "non-secret-entitlement-record-2026-01",
                "verified_at": "2026-01-02T00:01:00+00:00",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return market, evidence, results, source_record, proof


class HistoricalCorpusReadOnceIntegrityTests(unittest.TestCase):
    def test_external_artifacts_are_bound_to_the_exact_bytes_parsed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            market, evidence, results, source_record, proof = _write_fixture(root)
            output = root / "corpus"

            original_market = market.read_bytes()
            original_evidence = evidence.read_bytes()
            original_source_record = source_record.read_bytes()
            original_proof = proof.read_bytes()
            original_read_bytes = corpus._read_bytes
            swapped: set[Path] = set()

            replacements = {
                market: (json.dumps({"event_id": "tampered"}) + "\n").encode("utf-8"),
                evidence: json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "tampered-after-snapshot",
                    },
                    sort_keys=True,
                ).encode("utf-8"),
                source_record: json.dumps(
                    {
                        "source": "tampered",
                        "events": ["tt-a"],
                        "quote_outcomes": {"tt-a|winner|alice": "loss"},
                    },
                    sort_keys=True,
                ).encode("utf-8"),
                proof: json.dumps(
                    {
                        "schema_version": 1,
                        "kind": "historical_corpus_governance_proof",
                        "source_identity": "tampered-after-snapshot",
                    },
                    sort_keys=True,
                ).encode("utf-8"),
            }

            def read_once_then_replace(path: Path, *, context: str) -> bytes:
                payload = original_read_bytes(path, context=context)
                canonical = Path(path)
                if canonical in replacements and canonical not in swapped:
                    canonical.write_bytes(replacements[canonical])
                    swapped.add(canonical)
                return payload

            with patch.object(corpus, "_read_bytes", side_effect=read_once_then_replace):
                build = corpus.assemble_historical_corpus(
                    [(market, evidence)],
                    results_path=results,
                    governance_proof_path=proof,
                    output_dir=output,
                    name="read-once integrity regression",
                    outcome_reveal_after="2026-01-01T11:00:00+00:00",
                    imported_at="2026-01-02T00:05:00+00:00",
                )

            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            acquisition = manifest["governance"]["acquisition_evidence"]
            outcome_evidence = manifest["governance"]["outcome_evidence"]
            emitted_market = (output / "market.jsonl").read_bytes()

            self.assertEqual(swapped, {market, evidence, source_record, proof})
            self.assertEqual(
                acquisition["governance_proof_sha256"],
                _sha256_bytes(original_proof),
            )
            self.assertEqual(
                acquisition["snapshots"][0]["evidence_sha256"],
                _sha256_bytes(original_evidence),
            )
            self.assertEqual(
                acquisition["snapshots"][0]["market_sha256"],
                _sha256_bytes(original_market),
            )
            self.assertEqual(
                outcome_evidence["source_record_sha256"],
                _sha256_bytes(original_source_record),
            )
            self.assertIn(b'"event_id":"tt-a"', emitted_market)
            self.assertNotIn(b"tampered", emitted_market)
            self.assertEqual(build.import_identity, manifest["import_identity"])
            self.assertFalse(acquisition["historical_window_market_coverage_verified"])


if __name__ == "__main__":
    unittest.main()

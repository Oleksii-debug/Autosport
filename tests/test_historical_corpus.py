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


def _write_snapshot(
    root: Path,
    *,
    suffix: str = "a",
    observed: str = "2026-01-01T10:00:01+00:00",
) -> tuple[Path, Path]:
    market = root / f"snapshot-{suffix}.jsonl"
    evidence = root / f"snapshot-{suffix}.evidence.json"
    event = {
        "event_id": f"tt-{suffix}",
        "market_id": "winner",
        "selection_id": "alice",
        "decimal_odds": "1.80",
        "observed_ts": observed,
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
    payload = {
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
    }
    evidence.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return market, evidence


def _write_results(
    root: Path,
    *event_ids: str,
    provenance_overrides: dict | None = None,
) -> Path:
    path = root / "sealed-results.json"
    source_record = root / "official-outcome-source-record.json"
    quote_outcomes = {
        f"{event_id}|winner|alice": "win" for event_id in event_ids
    }
    source_record.write_text(
        json.dumps(
            {
                "source": "official-results:test-fixture",
                "events": list(event_ids),
                "quote_outcomes": quote_outcomes,
                "fixture_only": True,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    provenance = {
        "schema_version": 1,
        "kind": "historical_outcome_provenance",
        "source_identity": "official-results:test-fixture",
        "source_record_file": source_record.name,
        "source_record_sha256": _sha256(source_record),
        "terms_reference": "https://example.test/results-terms",
        "retention_basis": "verified internal research retention for test outcome record",
        "authority_reference": "test-outcome-authority-record",
        "available_at": "2026-01-01T11:00:00+00:00",
        "acquired_at": "2026-01-02T00:02:00+00:00",
        "verified_at": "2026-01-02T00:03:00+00:00",
        "licensing_or_retention_verified": True,
        "redistribution_policy": "internal_only",
        "redistribution_verified": False,
    }
    if provenance_overrides:
        provenance.update(provenance_overrides)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "quote_outcomes": quote_outcomes,
                "outcome_provenance": provenance,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path


def _write_governance(root: Path, **overrides) -> Path:
    path = root / "governance-proof.json"
    payload = {
        "schema_version": 1,
        "kind": "historical_corpus_governance_proof",
        "source_identity": "parlayapi:account-entitlement-2026-01",
        "source_ids": [ParlayApiTableTennisProvider.source_id],
        "terms_reference": "https://parlay-api.com/terms",
        "retention_basis": "verified fixture extension through 2026-12-31",
        "retention_expires_at": "2026-12-31T00:00:00+00:00",
        "retention_extension_authority_reference": "test-extension-authority-record",
        "redistribution_policy": "internal_only",
        "licensing_or_retention_verified": True,
        "redistribution_verified": False,
        "authority_reference": "non-secret-entitlement-record-2026-01",
        "verified_at": "2026-01-02T00:01:00+00:00",
    }
    payload.update(overrides)
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return path


class HistoricalCorpusAssemblerTests(unittest.TestCase):
    def test_assembles_schema_v2_without_promoting_window_coverage(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _write_snapshot(root, suffix="a")
            second = _write_snapshot(root, suffix="b", observed="2026-01-01T10:00:02+00:00")
            results = _write_results(root, "tt-a", "tt-b")
            proof = _write_governance(root)
            output = root / "corpus"

            build = assemble_historical_corpus(
                [first, second],
                results_path=results,
                governance_proof_path=proof,
                output_dir=output,
                name="verified selected TT snapshots",
                outcome_reveal_after="2026-01-01T11:00:00+00:00",
                imported_at="2026-01-02T00:05:00+00:00",
            )

            dataset = load_dataset(output)
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            sealed = json.loads((output / "results.json").read_text(encoding="utf-8"))
            acquisition = manifest["governance"]["acquisition_evidence"]
            outcome_evidence = manifest["governance"]["outcome_evidence"]
            self.assertEqual(dataset.schema_version, 2)
            self.assertEqual(dataset.import_identity, build.import_identity)
            self.assertEqual(build.snapshot_count, 2)
            self.assertEqual(build.event_count, 2)
            self.assertEqual(
                manifest["governance"]["retention_expires_at"],
                "2026-12-31T00:00:00+00:00",
            )
            self.assertEqual(
                manifest["governance"]["retention_extension_authority_reference"],
                "test-extension-authority-record",
            )
            self.assertEqual(acquisition["scope"], "selected_point_in_time_snapshots_only")
            self.assertTrue(acquisition["point_in_time_snapshot_contains_odds"])
            self.assertFalse(acquisition["historical_window_market_coverage_verified"])
            self.assertTrue(acquisition["licensing_or_retention_verified"])
            self.assertEqual(acquisition["rights_source_ids"], [ParlayApiTableTennisProvider.source_id])
            self.assertEqual(acquisition["governance_proof_sha256"], _sha256(proof))
            self.assertEqual(outcome_evidence["source_record_file"], "official-outcome-source-record.json")
            self.assertEqual(
                outcome_evidence["source_record_sha256"],
                _sha256(root / "official-outcome-source-record.json"),
            )
            self.assertTrue(outcome_evidence["source_record_checksum_verified"])
            self.assertTrue(outcome_evidence["quote_outcomes_bound_to_source_record"])
            self.assertFalse(outcome_evidence["source_record_redistributed"])
            self.assertEqual(outcome_evidence["available_at"], "2026-01-01T11:00:00+00:00")
            self.assertTrue(outcome_evidence["licensing_or_retention_verified"])
            self.assertEqual(
                sealed["outcome_provenance"]["source_identity"],
                "official-results:test-fixture",
            )
            self.assertEqual(
                sealed["outcome_provenance"]["quote_outcomes_sha256"],
                outcome_evidence["quote_outcomes_sha256"],
            )
            self.assertEqual(manifest["import_identity"], dataset.import_identity)

    def test_missing_retention_expiry_fails_closed_before_snapshot_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _write_snapshot(root)
            proof = _write_governance(root)
            payload = json.loads(proof.read_text(encoding="utf-8"))
            del payload["retention_expires_at"]
            proof.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            output = root / "corpus"
            with self.assertRaisesRegex(ValueError, "retention_expires_at"):
                assemble_historical_corpus(
                    [snapshot],
                    results_path=_write_results(root, "tt-a"),
                    governance_proof_path=proof,
                    output_dir=output,
                    name="blocked",
                    outcome_reveal_after="2026-01-01T11:00:00+00:00",
                    imported_at="2026-01-02T00:05:00+00:00",
                )
            self.assertFalse(output.exists())

    def test_retention_beyond_standard_ceiling_requires_extension_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _write_snapshot(root)
            output = root / "corpus"
            proof = _write_governance(
                root,
                retention_extension_authority_reference=None,
            )
            with self.assertRaisesRegex(ValueError, "beyond 90 days"):
                assemble_historical_corpus(
                    [snapshot],
                    results_path=_write_results(root, "tt-a"),
                    governance_proof_path=proof,
                    output_dir=output,
                    name="blocked",
                    outcome_reveal_after="2026-01-01T11:00:00+00:00",
                    imported_at="2026-01-02T00:05:00+00:00",
                )
            self.assertFalse(output.exists())

    def test_import_after_retention_expiry_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _write_snapshot(root)
            output = root / "corpus"
            proof = _write_governance(
                root,
                retention_expires_at="2026-01-02T00:04:00+00:00",
                retention_extension_authority_reference=None,
            )
            with self.assertRaisesRegex(ValueError, "imported_at exceeds"):
                assemble_historical_corpus(
                    [snapshot],
                    results_path=_write_results(root, "tt-a"),
                    governance_proof_path=proof,
                    output_dir=output,
                    name="blocked",
                    outcome_reveal_after="2026-01-01T11:00:00+00:00",
                    imported_at="2026-01-02T00:05:00+00:00",
                )
            self.assertFalse(output.exists())

    def test_unverified_retention_rights_fail_closed_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _write_snapshot(root)
            results = _write_results(root, "tt-a")
            proof = _write_governance(root, licensing_or_retention_verified=False)
            output = root / "corpus"
            with self.assertRaisesRegex(ValueError, "licensing_or_retention_verified=true"):
                assemble_historical_corpus(
                    [snapshot],
                    results_path=results,
                    governance_proof_path=proof,
                    output_dir=output,
                    name="blocked",
                    outcome_reveal_after="2026-01-01T11:00:00+00:00",
                    imported_at="2026-01-02T00:05:00+00:00",
                )
            self.assertFalse(output.exists())

    def test_governance_rights_must_bind_actual_snapshot_source_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _write_snapshot(root)
            output = root / "corpus"
            with self.assertRaisesRegex(ValueError, "source_ids do not match"):
                assemble_historical_corpus(
                    [snapshot],
                    results_path=_write_results(root, "tt-a"),
                    governance_proof_path=_write_governance(root, source_ids=["different-provider"]),
                    output_dir=output,
                    name="blocked",
                    outcome_reveal_after="2026-01-01T11:00:00+00:00",
                    imported_at="2026-01-02T00:05:00+00:00",
                )
            self.assertFalse(output.exists())

    def test_old_overbroad_snapshot_coverage_claim_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            market, evidence_path = _write_snapshot(root)
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["point_in_time_odds_market_coverage_verified"] = True
            evidence_path.write_text(json.dumps(evidence, sort_keys=True), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "point_in_time_odds_market_coverage_verified=false"):
                assemble_historical_corpus(
                    [(market, evidence_path)],
                    results_path=_write_results(root, "tt-a"),
                    governance_proof_path=_write_governance(root),
                    output_dir=root / "corpus",
                    name="blocked",
                    outcome_reveal_after="2026-01-01T11:00:00+00:00",
                    imported_at="2026-01-02T00:05:00+00:00",
                )

    def test_snapshot_hash_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            market, evidence = _write_snapshot(root)
            market.write_text(market.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "market_sha256"):
                assemble_historical_corpus(
                    [(market, evidence)],
                    results_path=_write_results(root, "tt-a"),
                    governance_proof_path=_write_governance(root),
                    output_dir=root / "corpus",
                    name="blocked",
                    outcome_reveal_after="2026-01-01T11:00:00+00:00",
                    imported_at="2026-01-02T00:05:00+00:00",
                )

    def test_permitted_redistribution_requires_affirmative_verification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _write_snapshot(root)
            proof = _write_governance(
                root,
                redistribution_policy="permitted",
                redistribution_verified=False,
            )
            with self.assertRaisesRegex(ValueError, "redistribution_verified=true"):
                assemble_historical_corpus(
                    [snapshot],
                    results_path=_write_results(root, "tt-a"),
                    governance_proof_path=proof,
                    output_dir=root / "corpus",
                    name="blocked",
                    outcome_reveal_after="2026-01-01T11:00:00+00:00",
                    imported_at="2026-01-02T00:05:00+00:00",
                )

    def test_missing_sealed_outcomes_fail_closed_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _write_snapshot(root)
            output = root / "corpus"
            with self.assertRaisesRegex(ValueError, "missing quote outcomes"):
                assemble_historical_corpus(
                    [snapshot],
                    results_path=_write_results(root),
                    governance_proof_path=_write_governance(root),
                    output_dir=output,
                    name="blocked",
                    outcome_reveal_after="2026-01-01T11:00:00+00:00",
                    imported_at="2026-01-02T00:05:00+00:00",
                )
            self.assertFalse(output.exists())

    def test_partial_sealed_outcomes_fail_closed_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _write_snapshot(root, suffix="a")
            second = _write_snapshot(root, suffix="b", observed="2026-01-01T10:00:02+00:00")
            output = root / "corpus"
            with self.assertRaisesRegex(ValueError, "missing quote outcomes"):
                assemble_historical_corpus(
                    [first, second],
                    results_path=_write_results(root, "tt-a"),
                    governance_proof_path=_write_governance(root),
                    output_dir=output,
                    name="blocked",
                    outcome_reveal_after="2026-01-01T11:00:00+00:00",
                    imported_at="2026-01-02T00:05:00+00:00",
                )
            self.assertFalse(output.exists())

    def test_unknown_sealed_outcome_fails_validation_and_leaves_no_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _write_snapshot(root)
            output = root / "corpus"
            with self.assertRaisesRegex(ValueError, "absent from historical market corpus"):
                assemble_historical_corpus(
                    [snapshot],
                    results_path=_write_results(root, "tt-a", "tt-unknown"),
                    governance_proof_path=_write_governance(root),
                    output_dir=output,
                    name="blocked",
                    outcome_reveal_after="2026-01-01T11:00:00+00:00",
                    imported_at="2026-01-02T00:05:00+00:00",
                )
            self.assertFalse(output.exists())

    def test_invalid_sealed_outcome_value_fails_closed_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _write_snapshot(root)
            results = _write_results(root, "tt-a")
            payload = json.loads(results.read_text(encoding="utf-8"))
            payload["quote_outcomes"]["tt-a|winner|alice"] = "push"
            results.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            output = root / "corpus"
            with self.assertRaisesRegex(ValueError, "allowed values are win, loss, void"):
                assemble_historical_corpus(
                    [snapshot],
                    results_path=results,
                    governance_proof_path=_write_governance(root),
                    output_dir=output,
                    name="blocked",
                    outcome_reveal_after="2026-01-01T11:00:00+00:00",
                    imported_at="2026-01-02T00:05:00+00:00",
                )
            self.assertFalse(output.exists())

    def test_duplicate_source_identity_across_snapshots_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = _write_snapshot(root, suffix="same")
            second_market = root / "copy.jsonl"
            second_evidence = root / "copy.evidence.json"
            second_market.write_bytes(first[0].read_bytes())
            evidence = json.loads(first[1].read_text(encoding="utf-8"))
            evidence["market_sha256"] = _sha256(second_market)
            second_evidence.write_text(json.dumps(evidence, sort_keys=True), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate canonical source event identity"):
                assemble_historical_corpus(
                    [first, (second_market, second_evidence)],
                    results_path=_write_results(root, "tt-same"),
                    governance_proof_path=_write_governance(root),
                    output_dir=root / "corpus",
                    name="blocked",
                    outcome_reveal_after="2026-01-01T11:00:00+00:00",
                    imported_at="2026-01-02T00:05:00+00:00",
                )

    def test_missing_outcome_provenance_fails_closed_before_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _write_snapshot(root)
            results = _write_results(root, "tt-a")
            payload = json.loads(results.read_text(encoding="utf-8"))
            del payload["outcome_provenance"]
            results.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            output = root / "corpus"
            with self.assertRaisesRegex(ValueError, "outcome_provenance must be an object"):
                assemble_historical_corpus(
                    [snapshot],
                    results_path=results,
                    governance_proof_path=_write_governance(root),
                    output_dir=output,
                    name="blocked",
                    outcome_reveal_after="2026-01-01T11:00:00+00:00",
                    imported_at="2026-01-02T00:05:00+00:00",
                )
            self.assertFalse(output.exists())

    def test_missing_outcome_source_artifact_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _write_snapshot(root)
            results = _write_results(root, "tt-a")
            (root / "official-outcome-source-record.json").unlink()
            output = root / "corpus"
            with self.assertRaisesRegex(ValueError, "source record is not readable"):
                assemble_historical_corpus(
                    [snapshot],
                    results_path=results,
                    governance_proof_path=_write_governance(root),
                    output_dir=output,
                    name="blocked",
                    outcome_reveal_after="2026-01-01T11:00:00+00:00",
                    imported_at="2026-01-02T00:05:00+00:00",
                )
            self.assertFalse(output.exists())

    def test_outcome_availability_after_reveal_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _write_snapshot(root)
            results = _write_results(
                root,
                "tt-a",
                provenance_overrides={"available_at": "2026-01-01T12:00:00+00:00"},
            )
            output = root / "corpus"
            with self.assertRaisesRegex(ValueError, "must not precede sealed outcome source availability"):
                assemble_historical_corpus(
                    [snapshot],
                    results_path=results,
                    governance_proof_path=_write_governance(root),
                    output_dir=output,
                    name="blocked",
                    outcome_reveal_after="2026-01-01T11:00:00+00:00",
                    imported_at="2026-01-02T00:05:00+00:00",
                )
            self.assertFalse(output.exists())

    def test_import_before_outcome_acquisition_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _write_snapshot(root)
            results = _write_results(
                root,
                "tt-a",
                provenance_overrides={
                    "acquired_at": "2026-01-02T00:06:00+00:00",
                    "verified_at": "2026-01-02T00:06:00+00:00",
                },
            )
            output = root / "corpus"
            with self.assertRaisesRegex(ValueError, "imported_at must not precede sealed outcome acquisition"):
                assemble_historical_corpus(
                    [snapshot],
                    results_path=results,
                    governance_proof_path=_write_governance(root),
                    output_dir=output,
                    name="blocked",
                    outcome_reveal_after="2026-01-01T11:00:00+00:00",
                    imported_at="2026-01-02T00:05:00+00:00",
                )
            self.assertFalse(output.exists())

    def test_invalid_outcome_source_checksum_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _write_snapshot(root)
            results = _write_results(
                root,
                "tt-a",
                provenance_overrides={"source_record_sha256": "not-a-sha"},
            )
            output = root / "corpus"
            with self.assertRaisesRegex(ValueError, "64-character SHA-256"):
                assemble_historical_corpus(
                    [snapshot],
                    results_path=results,
                    governance_proof_path=_write_governance(root),
                    output_dir=output,
                    name="blocked",
                    outcome_reveal_after="2026-01-01T11:00:00+00:00",
                    imported_at="2026-01-02T00:05:00+00:00",
                )
            self.assertFalse(output.exists())

    def test_mismatched_outcome_source_checksum_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _write_snapshot(root)
            results = _write_results(
                root,
                "tt-a",
                provenance_overrides={"source_record_sha256": "f" * 64},
            )
            output = root / "corpus"
            with self.assertRaisesRegex(ValueError, "does not match source record artifact"):
                assemble_historical_corpus(
                    [snapshot],
                    results_path=results,
                    governance_proof_path=_write_governance(root),
                    output_dir=output,
                    name="blocked",
                    outcome_reveal_after="2026-01-01T11:00:00+00:00",
                    imported_at="2026-01-02T00:05:00+00:00",
                )
            self.assertFalse(output.exists())

    def test_changed_sealed_outcome_label_with_valid_source_hash_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _write_snapshot(root)
            results = _write_results(root, "tt-a")
            payload = json.loads(results.read_text(encoding="utf-8"))
            payload["quote_outcomes"]["tt-a|winner|alice"] = "loss"
            results.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
            output = root / "corpus"
            with self.assertRaisesRegex(ValueError, "do not match hashed source record outcomes"):
                assemble_historical_corpus(
                    [snapshot],
                    results_path=results,
                    governance_proof_path=_write_governance(root),
                    output_dir=output,
                    name="blocked",
                    outcome_reveal_after="2026-01-01T11:00:00+00:00",
                    imported_at="2026-01-02T00:05:00+00:00",
                )
            self.assertFalse(output.exists())

    def test_effective_redistribution_policy_is_most_restrictive_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot = _write_snapshot(root)
            output = root / "corpus"
            build = assemble_historical_corpus(
                [snapshot],
                results_path=_write_results(root, "tt-a"),
                governance_proof_path=_write_governance(
                    root,
                    redistribution_policy="permitted",
                    redistribution_verified=True,
                ),
                output_dir=output,
                name="conservative-rights",
                outcome_reveal_after="2026-01-01T11:00:00+00:00",
                imported_at="2026-01-02T00:05:00+00:00",
            )
            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(build.redistribution_policy, "internal_only")
            self.assertEqual(manifest["governance"]["redistribution_policy"], "internal_only")


if __name__ == "__main__":
    unittest.main()
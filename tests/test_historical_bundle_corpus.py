from __future__ import annotations

import hashlib
import importlib.metadata
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.historical_bundle_corpus import (
    assemble_historical_corpus_from_bundle,
    verify_acquisition_bundle,
)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )


def _reseal_bundle(root: Path, bundle: dict[str, object]) -> str:
    request_scope = bundle["request_scope"]
    snapshots = bundle["snapshots"]
    match_results = bundle["match_results"]
    assert isinstance(request_scope, dict)
    assert isinstance(snapshots, list)
    assert isinstance(match_results, dict)
    request_identity = _canonical_hash(request_scope)
    bundle["request_identity"] = request_identity
    identity_payload = {
        "schema_version": 1,
        "kind": "parlayapi_historical_acquisition_bundle",
        "request_identity": request_identity,
        "snapshots": snapshots,
        "match_results": match_results,
    }
    bundle["evidence_identity"] = _canonical_hash(identity_payload)
    bundle_path = root / "bundle.json"
    _write_json(bundle_path, bundle)
    return _sha(bundle_path)


class HistoricalBundleCorpusTests(unittest.TestCase):
    def _bundle(self, root: Path) -> tuple[Path, str, dict[str, object]]:
        bundle_root = root / "acquisition"
        snapshots = bundle_root / "snapshots"
        snapshots.mkdir(parents=True)

        market = snapshots / "0001-market.jsonl"
        market.write_text('{"opaque_test_event":true}\n', encoding="utf-8")
        market_sha = _sha(market)
        provider_response_sha = _canonical_hash({"snapshot": "provider-response"})

        snapshot_evidence = {
            "schema_version": 1,
            "kind": "parlayapi_point_in_time_historical_snapshot",
            "provider": "parlayapi",
            "sport_key": "table_tennis",
            "requested_at": "2026-09-01T12:00:00Z",
            "snapshot_at": "2026-09-01T11:59:00Z",
            "captured_at": "2026-09-13T03:00:00Z",
            "response_sha256": provider_response_sha,
            "market_sha256": market_sha,
            "quote_count": 1,
            "has_data": True,
            "point_in_time_snapshot_contains_odds": True,
            "point_in_time_odds_market_coverage_verified": False,
            "historical_window_market_coverage_verified": False,
            "sealed_outcomes_present": False,
            "replay_corpus_ready": False,
            "licensing_or_retention_verified": False,
            "redistribution_verified": False,
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
        snapshot_evidence_path = snapshots / "0001-evidence.json"
        _write_json(snapshot_evidence_path, snapshot_evidence)

        result_payload = {"matches": [{"id": "m1"}]}
        canonical_response_sha = _canonical_hash(result_payload)
        result_capture = {
            "schema_version": 1,
            "kind": "parlayapi_historical_match_result_capture",
            "provider": "parlayapi",
            "sport_key": "table_tennis",
            "request": {"date": "2026-09-01", "priced_only": False},
            "captured_at": "2026-09-13T03:00:01Z",
            "canonical_response_sha256": canonical_response_sha,
            "payload": result_payload,
        }
        result_capture_path = bundle_root / "match-results.json"
        _write_json(result_capture_path, result_capture)
        capture_sha = _sha(result_capture_path)

        result_evidence = {
            "schema_version": 1,
            "kind": "parlayapi_historical_match_result_evidence",
            "provider": "parlayapi",
            "sport_key": "table_tennis",
            "requested_date": "2026-09-01",
            "priced_only": False,
            "captured_at": "2026-09-13T03:00:01Z",
            "canonical_response_sha256": canonical_response_sha,
            "capture_sha256": capture_sha,
            "historical_window_hours": 720,
            "historical_window_from": "2026-08-01",
            "provider_result_schema_parsed": False,
            "sealed_quote_outcomes_derived": False,
            "point_in_time_odds_market_coverage_verified": False,
            "historical_window_market_coverage_verified": False,
            "replay_corpus_ready": False,
            "licensing_or_retention_verified": False,
            "redistribution_verified": False,
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
        result_evidence_path = bundle_root / "match-results.evidence.json"
        _write_json(result_evidence_path, result_evidence)

        snapshot_entry = {
            "requested_at": "2026-09-01T12:00:00Z",
            "snapshot_at": "2026-09-01T11:59:00Z",
            "captured_at": "2026-09-13T03:00:00Z",
            "market_file": "snapshots/0001-market.jsonl",
            "evidence_file": "snapshots/0001-evidence.json",
            "market_sha256": market_sha,
            "evidence_sha256": _sha(snapshot_evidence_path),
            "provider_response_sha256": provider_response_sha,
            "quote_count": 1,
            "point_in_time_snapshot_contains_odds": True,
        }
        coverage_request = {"date_from": "2026-09-01", "date_to": "2026-09-01"}
        coverage_evidence = {
            "date_from": "2026-09-01",
            "date_to": "2026-09-01",
            "observed_at": "2026-09-13T03:00:00Z",
            "historical_window_hours": 720,
            "historical_window_from": "2026-08-01T00:00:00Z",
            "response_sha256": _canonical_hash({"coverage": "provider-response"}),
            "api_version": "test",
            "source_count": 1,
            "total_rows": 3,
            "total_priced_rows": 2,
            "sources": [
                {
                    "source": "test-source",
                    "rows": 3,
                    "first_date": "2026-09-01",
                    "last_date": "2026-09-01",
                    "priced_rows": 2,
                }
            ],
            "historical_window_market_coverage_verified": False,
            "licensing_or_retention_verified": False,
            "redistribution_verified": False,
        }
        result_entry = {
            "requested_date": "2026-09-01",
            "priced_only": False,
            "captured_at": "2026-09-13T03:00:01Z",
            "capture_file": "match-results.json",
            "evidence_file": "match-results.evidence.json",
            "capture_sha256": capture_sha,
            "evidence_sha256": _sha(result_evidence_path),
            "canonical_response_sha256": canonical_response_sha,
            "historical_window_hours": 720,
            "historical_window_from": "2026-08-01",
            "coverage_preflight": coverage_evidence,
        }
        request_scope = {
            "provider": "parlayapi",
            "sport_key": "table_tennis",
            "regions": ["us"],
            "markets": ["h2h", "spreads", "totals"],
            "requested_snapshot_timestamps": ["2026-09-01T12:00:00Z"],
            "coverage_preflight": coverage_request,
            "match_results": {"date": "2026-09-01", "priced_only": False},
        }
        request_identity = _canonical_hash(request_scope)
        identity_payload = {
            "schema_version": 1,
            "kind": "parlayapi_historical_acquisition_bundle",
            "request_identity": request_identity,
            "snapshots": [snapshot_entry],
            "match_results": result_entry,
        }
        bundle: dict[str, object] = {
            **identity_payload,
            "evidence_identity": _canonical_hash(identity_payload),
            "request_scope": request_scope,
            "acquisition_scope": "selected_point_in_time_snapshots_plus_match_result_archive",
            "snapshot_count": 1,
            "snapshots_with_odds": 1,
            "all_requested_snapshots_returned_odds": True,
            "provider_result_schema_parsed": False,
            "sealed_quote_outcomes_derived": False,
            "point_in_time_odds_market_coverage_verified": False,
            "historical_window_market_coverage_verified": False,
            "licensing_or_retention_verified": False,
            "redistribution_verified": False,
            "replay_corpus_ready": False,
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
        bundle_path = bundle_root / "bundle.json"
        _write_json(bundle_path, bundle)
        return bundle_root, _sha(bundle_path), bundle

    def test_verifies_complete_bundle_and_forwards_exact_snapshot_pairs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, bundle_sha, _ = self._bundle(Path(temp))
            verified = verify_acquisition_bundle(root, expected_bundle_sha256=bundle_sha)
            self.assertEqual(verified.bundle_sha256, bundle_sha)
            self.assertEqual(len(verified.snapshot_pairs), 1)
            self.assertTrue(verified.result_capture_path.endswith("match-results.json"))

            sentinel = object()
            with patch(
                "autosport.historical_bundle_corpus.assemble_historical_corpus",
                return_value=sentinel,
            ) as assembler:
                result = assemble_historical_corpus_from_bundle(
                    root,
                    expected_bundle_sha256=bundle_sha,
                    results_path="sealed-results.json",
                    governance_proof_path="governance.json",
                    output_dir="corpus",
                    name="real-table-tennis",
                    outcome_reveal_after="2026-09-02T00:00:00Z",
                    imported_at="2026-09-13T04:00:00Z",
                )
            self.assertIs(result, sentinel)
            self.assertEqual(assembler.call_args.args[0], verified.snapshot_pairs)
            self.assertEqual(assembler.call_args.kwargs["results_path"], "sealed-results.json")
            self.assertEqual(assembler.call_args.kwargs["governance_proof_path"], "governance.json")

    def test_rejects_resealed_bundle_without_request_coverage_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _, bundle = self._bundle(Path(temp))
            request_scope = bundle["request_scope"]
            assert isinstance(request_scope, dict)
            request_scope.pop("coverage_preflight")
            resealed_sha = _reseal_bundle(root, bundle)
            with self.assertRaisesRegex(ValueError, "request_scope.coverage_preflight must be an object"):
                verify_acquisition_bundle(root, expected_bundle_sha256=resealed_sha)

    def test_rejects_resealed_bundle_without_coverage_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _, bundle = self._bundle(Path(temp))
            match_results = bundle["match_results"]
            assert isinstance(match_results, dict)
            match_results.pop("coverage_preflight")
            resealed_sha = _reseal_bundle(root, bundle)
            with self.assertRaisesRegex(ValueError, "match_results.coverage_preflight must be an object"):
                verify_acquisition_bundle(root, expected_bundle_sha256=resealed_sha)

    def test_rejects_resealed_coverage_row_totals_that_do_not_match_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _, bundle = self._bundle(Path(temp))
            match_results = bundle["match_results"]
            assert isinstance(match_results, dict)
            coverage = match_results["coverage_preflight"]
            assert isinstance(coverage, dict)
            coverage["total_rows"] = 4
            resealed_sha = _reseal_bundle(root, bundle)
            with self.assertRaisesRegex(ValueError, "total_rows does not match sources"):
                verify_acquisition_bundle(root, expected_bundle_sha256=resealed_sha)

    def test_rejects_tampered_snapshot_even_when_bundle_file_hash_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, bundle_sha, _ = self._bundle(Path(temp))
            (root / "snapshots" / "0001-market.jsonl").write_text(
                '{"opaque_test_event":"tampered"}\n',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "market_sha256 does not match"):
                verify_acquisition_bundle(root, expected_bundle_sha256=bundle_sha)

    def test_rejects_bundle_overclaim_before_corpus_assembly(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _, bundle = self._bundle(Path(temp))
            bundle["licensing_or_retention_verified"] = True
            bundle_path = root / "bundle.json"
            _write_json(bundle_path, bundle)
            with self.assertRaisesRegex(ValueError, "licensing_or_retention_verified must remain false"):
                verify_acquisition_bundle(root, expected_bundle_sha256=_sha(bundle_path))

    def test_rejects_partial_snapshot_bundle_instead_of_silently_dropping_no_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _, bundle = self._bundle(Path(temp))
            snapshots = bundle["snapshots"]
            assert isinstance(snapshots, list) and isinstance(snapshots[0], dict)
            snapshots[0]["point_in_time_snapshot_contains_odds"] = False
            bundle["snapshots_with_odds"] = 0
            bundle["all_requested_snapshots_returned_odds"] = False
            bundle_path = root / "bundle.json"
            _write_json(bundle_path, bundle)
            with self.assertRaisesRegex(ValueError, "every requested snapshot returned odds"):
                verify_acquisition_bundle(root, expected_bundle_sha256=_sha(bundle_path))

    def test_rejects_wrong_external_bundle_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root, _, _ = self._bundle(Path(temp))
            with self.assertRaisesRegex(ValueError, "does not match expected_bundle_sha256"):
                verify_acquisition_bundle(root, expected_bundle_sha256="0" * 64)

    def test_installed_bundle_corpus_entrypoint_resolves(self) -> None:
        distribution = importlib.metadata.distribution("autosport-lab")
        scripts = {
            entry.name: entry
            for entry in distribution.entry_points
            if entry.group == "console_scripts"
        }
        entry = scripts["autosport-build-historical-corpus-from-bundle"]
        self.assertEqual(
            entry.value,
            "autosport.historical_bundle_corpus:main",
        )
        loaded = entry.load()
        self.assertEqual(loaded.__module__, "autosport.historical_bundle_corpus")
        self.assertEqual(loaded.__name__, "main")


if __name__ == "__main__":
    unittest.main()

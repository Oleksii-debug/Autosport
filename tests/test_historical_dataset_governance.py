import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from autosport.dataset import load_dataset
from autosport.outcome_provenance import canonical_outcomes_sha256


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _base_event() -> dict:
    return {
        "event_id": "tt-001",
        "market_id": "winner",
        "selection_id": "alice",
        "decimal_odds": "1.80",
        "observed_ts": "2026-01-01T10:00:01+00:00",
        "source_id": "licensed-feed",
        "sequence": 1,
        "market_type": "winner",
        "source_ts": "2026-01-01T10:00:00+00:00",
        "ingest_ts": "2026-01-01T10:00:01+00:00",
        "metadata": {},
    }


def _base_governance() -> dict:
    return {
        "source_identity": "vendor-feed:contract-abc:export-2026-01-01",
        "terms_reference": "contract-abc",
        "retention_basis": "licensed internal historical research through 2027-01-01",
        "redistribution_policy": "internal_only",
        "acquired_at": "2026-01-02T00:00:00+00:00",
        "imported_at": "2026-01-02T00:05:00+00:00",
        "coverage": {
            "start_ts": "2026-01-01T10:00:00+00:00",
            "end_ts": "2026-01-01T10:01:00+00:00",
            "source_ids": ["licensed-feed"],
            "market_types": ["winner"],
        },
        "causality": {
            "strategy_time_field": "observed_ts",
            "outcome_reveal_after": "2026-01-01T10:02:00+00:00",
        },
    }


def _outcome_provenance(outcomes: dict[str, object]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "historical_outcome_provenance",
        "source_identity": "official-results-feed:table-tennis:2026-01-01",
        "source_reference": "official-results-export-2026-01-01",
        "authority_reference": "result-authority-record-2026-01",
        "terms_reference": "result-feed-contract-2026",
        "retention_basis": "licensed internal historical research through 2027-01-01",
        "redistribution_policy": "internal_only",
        "licensing_or_retention_verified": True,
        "redistribution_verified": False,
        "authoritative_outcomes_verified": True,
        "acquired_at": "2026-01-01T11:00:00+00:00",
        "verified_at": "2026-01-01T11:01:00+00:00",
        "source_payload_sha256": "a" * 64,
        "quote_outcomes_sha256": canonical_outcomes_sha256(outcomes),
        "real_money_execution": False,
    }


def _write_dataset(
    root: Path,
    *,
    event: dict | None = None,
    governance: dict | None = None,
    import_identity: str | None = None,
) -> Path:
    market_path = root / "market.jsonl"
    results_path = root / "results.json"
    event = _base_event() if event is None else event
    governance = _base_governance() if governance is None else governance
    market_path.write_text(json.dumps(event, sort_keys=True) + "\n", encoding="utf-8")
    outcomes = {"tt-001|winner|alice": "win"}
    results_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "outcome_reveal_after": governance["causality"]["outcome_reveal_after"],
                "quote_outcomes": outcomes,
                "outcome_provenance": _outcome_provenance(outcomes),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 2,
        "dataset_kind": "historical",
        "name": "licensed table-tennis sample",
        "sport": "table_tennis",
        "market_file": market_path.name,
        "results_file": results_path.name,
        "market_sha256": _sha256(market_path),
        "results_sha256": _sha256(results_path),
        "governance": governance,
    }
    if import_identity is not None:
        manifest["import_identity"] = import_identity
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return root


class HistoricalDatasetGovernanceTests(unittest.TestCase):
    def test_valid_historical_manifest_has_stable_content_addressed_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_dataset(Path(tmp))
            first = load_dataset(root)
            second = load_dataset(root)
            self.assertEqual(first.schema_version, 2)
            self.assertIsNotNone(first.governance)
            self.assertEqual(first.governance.source_identity, "vendor-feed:contract-abc:export-2026-01-01")
            self.assertEqual(first.governance.redistribution_policy, "internal_only")
            self.assertEqual(first.import_identity, second.import_identity)
            self.assertEqual(len(first.import_identity), 64)

    def test_missing_retention_basis_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            governance = _base_governance()
            governance.pop("retention_basis")
            root = _write_dataset(Path(tmp), governance=governance)
            with self.assertRaisesRegex(ValueError, "retention_basis"):
                load_dataset(root)

    def test_source_time_after_observation_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = _base_event()
            event["source_ts"] = "2026-01-01T10:00:02+00:00"
            root = _write_dataset(Path(tmp), event=event)
            with self.assertRaisesRegex(ValueError, "source_ts is after observed_ts"):
                load_dataset(root)

    def test_historical_event_requires_explicit_ingest_timestamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = _base_event()
            event.pop("ingest_ts")
            root = _write_dataset(Path(tmp), event=event)
            with self.assertRaisesRegex(ValueError, "requires explicit ingest_ts"):
                load_dataset(root)

    def test_outcome_reveal_before_market_coverage_end_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            governance = _base_governance()
            governance["causality"]["outcome_reveal_after"] = "2026-01-01T10:00:30+00:00"
            root = _write_dataset(Path(tmp), governance=governance)
            with self.assertRaisesRegex(ValueError, "outcome_reveal_after"):
                load_dataset(root)

    def test_forged_import_identity_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_dataset(Path(tmp), import_identity="0" * 64)
            with self.assertRaisesRegex(ValueError, "historical import identity mismatch"):
                load_dataset(root)

    def test_future_outcome_metadata_in_market_stream_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = _base_event()
            event["metadata"] = {"final_result": "alice"}
            root = _write_dataset(Path(tmp), event=event)
            with self.assertRaisesRegex(ValueError, "future/outcome metadata"):
                load_dataset(root)

    def test_nested_future_outcome_metadata_in_market_stream_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = _base_event()
            event["metadata"] = {
                "research_signal": {
                    "model": "candidate-v1",
                    "sealed_context": {"final_result": "alice"},
                }
            }
            root = _write_dataset(Path(tmp), event=event)
            with self.assertRaisesRegex(ValueError, "future/outcome metadata"):
                load_dataset(root)

    def test_future_outcome_metadata_nested_in_list_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = _base_event()
            event["metadata"] = {
                "research_signal": [
                    {"feature": "recent-form"},
                    {"settlement_result": "win"},
                ]
            }
            root = _write_dataset(Path(tmp), event=event)
            with self.assertRaisesRegex(ValueError, "future/outcome metadata"):
                load_dataset(root)

    def test_winner_alias_nested_in_metadata_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            event = _base_event()
            event["metadata"] = {"research_signal": {"features": {"winner": "alice"}}}
            root = _write_dataset(Path(tmp), event=event)
            with self.assertRaisesRegex(ValueError, "future/outcome metadata"):
                load_dataset(root)

    def test_manifest_path_escape_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_dataset(Path(tmp))
            manifest_path = root / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["market_file"] = "../outside.jsonl"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "escapes the dataset root"):
                load_dataset(root)


if __name__ == "__main__":
    unittest.main()

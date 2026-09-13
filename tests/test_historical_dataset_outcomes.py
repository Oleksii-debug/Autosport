import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from autosport.dataset import load_dataset


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_historical_dataset(root: Path, outcomes: dict[str, object]) -> Path:
    event = {
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
    market_path = root / "market.jsonl"
    results_path = root / "results.json"
    market_path.write_text(json.dumps(event, sort_keys=True) + "\n", encoding="utf-8")
    results_path.write_text(
        json.dumps(
            {"schema_version": 1, "quote_outcomes": outcomes},
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
        "governance": {
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
        },
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return root


class HistoricalDatasetOutcomeTests(unittest.TestCase):
    def test_complete_supported_outcome_is_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_historical_dataset(
                Path(tmp),
                {"tt-001|winner|alice": "win"},
            )
            dataset = load_dataset(root)
            self.assertEqual(
                dataset.load_results_after_replay(),
                {"tt-001|winner|alice": "win"},
            )

    def test_missing_quote_outcome_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_historical_dataset(Path(tmp), {})
            with self.assertRaisesRegex(ValueError, "missing quote outcomes"):
                load_dataset(root)

    def test_unsupported_outcome_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_historical_dataset(
                Path(tmp),
                {"tt-001|winner|alice": "push"},
            )
            with self.assertRaisesRegex(ValueError, "unsupported outcome"):
                load_dataset(root)

    def test_non_string_outcome_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = _write_historical_dataset(
                Path(tmp),
                {"tt-001|winner|alice": True},
            )
            with self.assertRaisesRegex(ValueError, "unsupported outcome"):
                load_dataset(root)


if __name__ == "__main__":
    unittest.main()

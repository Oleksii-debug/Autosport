import hashlib
import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from autosport.dataset import load_dataset


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_dataset(root: Path) -> tuple[Path, Path]:
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
            {
                "schema_version": 1,
                "quote_outcomes": {"tt-001|winner|alice": "win"},
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "name": "sealed table-tennis dataset",
        "sport": "table_tennis",
        "market_file": market_path.name,
        "results_file": results_path.name,
        "market_sha256": _sha256(market_path),
        "results_sha256": _sha256(results_path),
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return market_path, results_path


def _write_historical_snapshot_attack_fixture(root: Path) -> tuple[Path, str]:
    invalid_event = {
        "event_id": "tt-001",
        "market_id": "winner",
        "selection_id": "alice",
        "decimal_odds": "1.80",
        "observed_ts": "2026-01-01T10:00:01+00:00",
        "source_id": "uncovered-feed",
        "sequence": 1,
        "market_type": "winner",
        "source_ts": "2026-01-01T10:00:00+00:00",
        "ingest_ts": "2026-01-01T10:00:01+00:00",
        "metadata": {},
    }
    valid_event = dict(invalid_event)
    valid_event["source_id"] = "licensed-feed"

    market_path = root / "market.jsonl"
    results_path = root / "results.json"
    market_path.write_text(json.dumps(invalid_event, sort_keys=True) + "\n", encoding="utf-8")
    alternate_valid_text = json.dumps(valid_event, sort_keys=True) + "\n"
    results_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "quote_outcomes": {"tt-001|winner|alice": "win"},
                "outcome_reveal_after": "2026-01-01T10:02:00+00:00",
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 2,
        "dataset_kind": "historical",
        "name": "sealed historical table-tennis dataset",
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
    return market_path, alternate_valid_text


class DatasetConsumptionHashIntegrityTests(unittest.TestCase):
    def test_manifest_bytes_changed_after_load_fail_closed_at_market_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_dataset(root)
            manifest_path = root / "manifest.json"
            dataset = load_dataset(root)
            self.assertEqual(dataset.manifest_file_sha256, _sha256(manifest_path))

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["name"] = "rewritten dataset identity"
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "dataset manifest hash changed after verification",
            ):
                dataset.load_market_events()

    def test_manifest_bytes_changed_after_market_load_fail_closed_at_results_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_dataset(root)
            manifest_path = root / "manifest.json"
            dataset = load_dataset(root)
            self.assertTrue(dataset.load_market_events())

            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["name"] = "rewritten before outcome reveal"
            manifest_path.write_text(
                json.dumps(manifest, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "dataset manifest hash changed after verification",
            ):
                dataset.load_results_after_replay()

    def test_market_bytes_changed_after_load_fail_closed_at_replay_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            market_path, _ = _write_dataset(root)
            dataset = load_dataset(root)

            tampered = json.loads(market_path.read_text(encoding="utf-8"))
            tampered["decimal_odds"] = "2.10"
            market_path.write_text(
                json.dumps(tampered, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "market dataset hash changed after verification",
            ):
                dataset.load_market_events()

    def test_results_bytes_changed_after_load_fail_closed_at_reveal_access(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, results_path = _write_dataset(root)
            dataset = load_dataset(root)

            results_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "quote_outcomes": {"tt-001|winner|alice": "loss"},
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "sealed results hash changed after verification",
            ):
                dataset.load_results_after_replay()

    def test_historical_governance_validates_the_same_market_bytes_that_are_hashed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            market_path, alternate_valid_text = _write_historical_snapshot_attack_fixture(root)
            original_open = Path.open

            def snapshot_swapping_open(path: Path, mode: str = "r", *args, **kwargs):
                if path == market_path and "r" in mode and "b" not in mode:
                    return StringIO(alternate_valid_text)
                return original_open(path, mode, *args, **kwargs)

            with patch.object(Path, "open", new=snapshot_swapping_open):
                with self.assertRaisesRegex(
                    ValueError,
                    "source_id is outside declared coverage",
                ):
                    load_dataset(root)


if __name__ == "__main__":
    unittest.main()

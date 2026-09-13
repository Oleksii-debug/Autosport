import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from autosport.dataset import load_dataset
from autosport.parlayapi_provider import ParlayApiTableTennisProvider


class HistoricalRetentionAccessTests(unittest.TestCase):
    def test_expired_governance_fails_before_dataset_members_are_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = {
                "schema_version": 2,
                "dataset_kind": "historical",
                "name": "expired fixture",
                "sport": "table_tennis",
                "market_file": "missing-market.jsonl",
                "results_file": "missing-results.json",
                "market_sha256": "0" * 64,
                "results_sha256": "0" * 64,
                "governance": {
                    "source_identity": "parlayapi:test-expired",
                    "terms_reference": "https://parlay-api.com/terms",
                    "retention_basis": "fixture authority through 2026-01-03",
                    "retention_expires_at": "2026-01-03T00:00:00+00:00",
                    "redistribution_policy": "internal_only",
                    "acquired_at": "2026-01-02T00:00:00+00:00",
                    "imported_at": "2026-01-02T00:01:00+00:00",
                    "coverage": {
                        "start_ts": "2026-01-01T10:00:00+00:00",
                        "end_ts": "2026-01-01T10:00:01+00:00",
                        "source_ids": [ParlayApiTableTennisProvider.source_id],
                        "market_types": ["winner"],
                    },
                    "causality": {
                        "strategy_time_field": "observed_ts",
                        "outcome_reveal_after": "2026-01-01T11:00:00+00:00",
                    },
                },
            }
            (root / "manifest.json").write_text(
                json.dumps(manifest, sort_keys=True),
                encoding="utf-8",
            )

            with patch(
                "autosport.dataset._retention_now",
                return_value=datetime(2026, 1, 4, tzinfo=timezone.utc),
            ):
                with self.assertRaisesRegex(ValueError, "retention window expired"):
                    load_dataset(root)

    def test_parlay_governance_without_structured_expiry_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = {
                "schema_version": 2,
                "dataset_kind": "historical",
                "name": "missing expiry fixture",
                "sport": "table_tennis",
                "market_file": "missing-market.jsonl",
                "results_file": "missing-results.json",
                "market_sha256": "0" * 64,
                "results_sha256": "0" * 64,
                "governance": {
                    "source_identity": "parlayapi:test-missing-expiry",
                    "terms_reference": "https://parlay-api.com/terms",
                    "retention_basis": "fixture basis without structured expiry",
                    "redistribution_policy": "internal_only",
                    "acquired_at": "2026-01-02T00:00:00+00:00",
                    "imported_at": "2026-01-02T00:01:00+00:00",
                    "coverage": {
                        "start_ts": "2026-01-01T10:00:00+00:00",
                        "end_ts": "2026-01-01T10:00:01+00:00",
                        "source_ids": [ParlayApiTableTennisProvider.source_id],
                        "market_types": ["winner"],
                    },
                    "causality": {
                        "strategy_time_field": "observed_ts",
                        "outcome_reveal_after": "2026-01-01T11:00:00+00:00",
                    },
                },
            }
            (root / "manifest.json").write_text(
                json.dumps(manifest, sort_keys=True),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "requires structured retention_expires_at"):
                load_dataset(root)


if __name__ == "__main__":
    unittest.main()

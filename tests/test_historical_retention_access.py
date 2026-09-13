import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from autosport.dataset import load_dataset
from autosport.parlayapi_provider import ParlayApiTableTennisProvider


def _acquisition_evidence(
    *,
    captured_at: str,
    retention_expires_at: str,
    extension: str | None = None,
) -> dict:
    return {
        "snapshots": [{"captured_at": captured_at}],
        "retention_expires_at": retention_expires_at,
        "retention_extension_authority_reference": extension,
    }


class HistoricalRetentionAccessTests(unittest.TestCase):
    def test_expired_governance_fails_before_dataset_members_are_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expires = "2026-01-03T00:00:00+00:00"
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
                    "retention_expires_at": expires,
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
                    "acquisition_evidence": _acquisition_evidence(
                        captured_at="2026-01-02T00:00:00+00:00",
                        retention_expires_at=expires,
                    ),
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

    def test_direct_parlay_manifest_cannot_bypass_90_day_ceiling(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expires = "2026-05-01T00:00:00+00:00"
            manifest = {
                "schema_version": 2,
                "dataset_kind": "historical",
                "name": "overlong direct manifest",
                "sport": "table_tennis",
                "market_file": "missing-market.jsonl",
                "results_file": "missing-results.json",
                "market_sha256": "0" * 64,
                "results_sha256": "0" * 64,
                "governance": {
                    "source_identity": "parlayapi:test-overlong",
                    "terms_reference": "https://parlay-api.com/terms",
                    "retention_basis": "unextended fixture claim",
                    "retention_expires_at": expires,
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
                    "acquisition_evidence": _acquisition_evidence(
                        captured_at="2026-01-01T00:00:00+00:00",
                        retention_expires_at=expires,
                    ),
                },
            }
            (root / "manifest.json").write_text(
                json.dumps(manifest, sort_keys=True),
                encoding="utf-8",
            )

            with patch(
                "autosport.dataset._retention_now",
                return_value=datetime(2026, 1, 2, tzinfo=timezone.utc),
            ):
                with self.assertRaisesRegex(ValueError, "beyond 90 days"):
                    load_dataset(root)

    def test_direct_parlay_manifest_requires_capture_provenance_for_ceiling_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expires = "2026-02-01T00:00:00+00:00"
            manifest = {
                "schema_version": 2,
                "dataset_kind": "historical",
                "name": "missing capture provenance",
                "sport": "table_tennis",
                "market_file": "missing-market.jsonl",
                "results_file": "missing-results.json",
                "market_sha256": "0" * 64,
                "results_sha256": "0" * 64,
                "governance": {
                    "source_identity": "parlayapi:test-no-capture-proof",
                    "terms_reference": "https://parlay-api.com/terms",
                    "retention_basis": "fixture claim",
                    "retention_expires_at": expires,
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

            with self.assertRaisesRegex(ValueError, "requires acquisition_evidence"):
                load_dataset(root)

    def test_extension_claim_must_match_acquisition_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            expires = "2026-05-01T00:00:00+00:00"
            manifest = {
                "schema_version": 2,
                "dataset_kind": "historical",
                "name": "mismatched extension claim",
                "sport": "table_tennis",
                "market_file": "missing-market.jsonl",
                "results_file": "missing-results.json",
                "market_sha256": "0" * 64,
                "results_sha256": "0" * 64,
                "governance": {
                    "source_identity": "parlayapi:test-extension-mismatch",
                    "terms_reference": "https://parlay-api.com/terms",
                    "retention_basis": "fixture extension claim",
                    "retention_expires_at": expires,
                    "retention_extension_authority_reference": "authority:top-level",
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
                    "acquisition_evidence": _acquisition_evidence(
                        captured_at="2026-01-01T00:00:00+00:00",
                        retention_expires_at=expires,
                        extension="authority:evidence",
                    ),
                },
            }
            (root / "manifest.json").write_text(
                json.dumps(manifest, sort_keys=True),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ValueError,
                "retention_extension_authority_reference must match",
            ):
                load_dataset(root)


if __name__ == "__main__":
    unittest.main()

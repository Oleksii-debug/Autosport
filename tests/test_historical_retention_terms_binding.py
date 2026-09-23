import json
import tempfile
import unittest
from pathlib import Path

from autosport.dataset import load_dataset


class HistoricalRetentionTermsBindingTests(unittest.TestCase):
    @staticmethod
    def _manifest(*, source_identity: str, source_ids: list[str]) -> dict:
        return {
            "schema_version": 2,
            "dataset_kind": "historical",
            "name": "terms-binding-adversarial",
            "sport": "table_tennis",
            "market_file": "must-not-be-opened-market.jsonl",
            "results_file": "must-not-be-opened-results.json",
            "market_sha256": "0" * 64,
            "results_sha256": "0" * 64,
            "governance": {
                "source_identity": source_identity,
                "terms_reference": "https://example.invalid/substituted-terms",
                "retention_basis": "fixture only",
                "retention_expires_at": "2099-01-01T00:00:00+00:00",
                "redistribution_policy": "internal_only",
                "acquired_at": "2026-09-13T08:30:00+00:00",
                "imported_at": "2026-09-13T08:31:00+00:00",
                "coverage": {
                    "start_ts": "2026-09-13T08:00:00+00:00",
                    "end_ts": "2026-09-13T08:20:00+00:00",
                    "source_ids": source_ids,
                    "market_types": ["winner"],
                },
                "causality": {
                    "strategy_time_field": "observed_ts",
                    "outcome_reveal_after": "2026-09-13T09:00:00+00:00",
                },
            },
        }

    def _assert_terms_binding_fails_before_member_resolution(
        self,
        *,
        source_identity: str,
        source_ids: list[str],
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "manifest.json").write_text(
                json.dumps(
                    self._manifest(
                        source_identity=source_identity,
                        source_ids=source_ids,
                    )
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "canonical terms_reference"):
                load_dataset(root)

    def test_parlay_source_identity_cannot_substitute_terms_reference(self):
        self._assert_terms_binding_fails_before_member_resolution(
            source_identity="parlayapi:account-entitlement-adversarial",
            source_ids=["parlayapi:table_tennis"],
        )

    def test_parlay_coverage_source_id_cannot_substitute_terms_reference(self):
        self._assert_terms_binding_fails_before_member_resolution(
            source_identity="archive:disguised-source-identity",
            source_ids=["parlayapi:table_tennis"],
        )


if __name__ == "__main__":
    unittest.main()

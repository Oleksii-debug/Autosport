import json
import tempfile
import unittest
from pathlib import Path

from autosport.source_continuity import (
    ProviderContinuityWitness,
    SourceContinuityStore,
)


class ForgedContinuityWitness(ProviderContinuityWitness):
    pass


class SourceContinuityProvenanceTests(unittest.TestCase):
    def test_boolean_schema_version_is_not_accepted_as_integer_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source_continuity.json"
            path.write_text(
                json.dumps({"schema_version": True, "sources": {}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "invalid source continuity store"):
                SourceContinuityStore(path)

    def test_cross_field_forged_anchor_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source_continuity.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "sources": {
                            "source": {
                                "source_id": "source",
                                "status": "unknown",
                                "trusted_token": "forged-token",
                                "last_observed_cursor": None,
                                "last_success_at": None,
                                "last_failure_at": None,
                                "reason": "no_continuity_evidence",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "invalid source continuity store"):
                SourceContinuityStore(path)

    def test_witness_subclass_cannot_gain_provider_authority(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SourceContinuityStore(Path(tmp) / "source_continuity.json")
            forged = ForgedContinuityWitness(None, "token-1")

            with self.assertRaisesRegex(
                TypeError, "witness must be ProviderContinuityWitness or null"
            ):
                store.record_success(
                    "source",
                    now="2026-09-21T08:00:00+00:00",
                    cursor="token-1",
                    witness=forged,
                )

            state = store.get("source")
            self.assertEqual(state.status, "unknown")
            self.assertIsNone(state.trusted_token)


    def test_exact_public_witness_cannot_mint_verified_continuity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SourceContinuityStore(Path(tmp) / "source_continuity.json")
            store.record_success(
                "source",
                now="2026-09-21T08:00:00+00:00",
                cursor="token-1",
                witness=ProviderContinuityWitness(None, "token-1"),
            )

            state = store.record_success(
                "source",
                now="2026-09-21T08:01:00+00:00",
                cursor="token-2",
                witness=ProviderContinuityWitness(
                    "token-1", "token-2", backfill_complete=True
                ),
            )

            self.assertEqual(state.status, "unknown")
            self.assertEqual(state.trusted_token, "token-1")
            self.assertEqual(state.last_observed_cursor, "token-2")
            self.assertEqual(state.reason, "provider_native_evidence_required")

    def test_cross_source_witness_cannot_seed_foreign_resume_chain(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SourceContinuityStore(Path(tmp) / "source_continuity.json")
            store.record_success(
                "source-a",
                now="2026-09-21T08:00:00+00:00",
                cursor="shared-token",
                witness=ProviderContinuityWitness(None, "shared-token"),
            )

            state = store.record_success(
                "source-b",
                now="2026-09-21T08:01:00+00:00",
                cursor="next-token",
                witness=ProviderContinuityWitness("shared-token", "next-token"),
            )

            self.assertEqual(state.status, "unknown")
            self.assertIsNone(state.trusted_token)
            self.assertEqual(
                state.reason,
                "witness_previous_token_without_trusted_anchor",
            )

    def test_self_consistent_legacy_verified_payload_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source_continuity.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "sources": {
                            "source": {
                                "source_id": "source",
                                "status": "verified",
                                "trusted_token": "token-2",
                                "last_observed_cursor": "token-2",
                                "last_success_at": "2026-09-21T08:01:00+00:00",
                                "last_failure_at": None,
                                "reason": "provider_chain_and_backfill_verified",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "invalid source continuity store"):
                SourceContinuityStore(path)


if __name__ == "__main__":
    unittest.main()

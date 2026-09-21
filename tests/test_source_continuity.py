import json
import tempfile
import unittest
from pathlib import Path

from autosport.source_continuity import (
    ProviderContinuityWitness,
    SourceContinuityStore,
)


class SourceContinuityTests(unittest.TestCase):
    def _store(self, tmp: str) -> SourceContinuityStore:
        return SourceContinuityStore(Path(tmp) / "source_continuity.json")

    def test_snapshot_only_success_never_implies_continuous_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            state = store.record_success(
                "snapshot-source",
                now="2026-09-21T08:00:00+00:00",
                cursor="local-snapshot-cursor",
                witness=None,
            )
            self.assertEqual(state.status, "unknown")
            self.assertIsNone(state.trusted_token)
            self.assertEqual(state.last_observed_cursor, "local-snapshot-cursor")
            self.assertEqual(state.reason, "provider_continuity_witness_absent")

    def test_caller_complete_chain_cannot_mint_verified_continuity(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            first = store.record_success(
                "cursor-source",
                now="2026-09-21T08:00:00+00:00",
                cursor="token-1",
                witness=ProviderContinuityWitness(None, "token-1"),
            )
            self.assertEqual(first.status, "unknown")
            self.assertEqual(first.trusted_token, "token-1")
            self.assertEqual(
                first.reason,
                "provider_anchor_established_without_prior_continuity",
            )

            second = store.record_success(
                "cursor-source",
                now="2026-09-21T08:01:00+00:00",
                cursor="token-2",
                witness=ProviderContinuityWitness("token-1", "token-2"),
            )
            self.assertEqual(second.status, "unknown")
            self.assertEqual(second.trusted_token, "token-1")
            self.assertEqual(second.reason, "provider_native_evidence_required")

    def test_restart_preserves_trusted_provider_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source_continuity.json"
            store = SourceContinuityStore(path)
            store.record_success(
                "cursor-source",
                now="2026-09-21T08:00:00+00:00",
                cursor="token-1",
                witness=ProviderContinuityWitness(None, "token-1"),
            )
            store.record_success(
                "cursor-source",
                now="2026-09-21T08:01:00+00:00",
                cursor="token-2",
                witness=ProviderContinuityWitness("token-1", "token-2"),
            )

            reopened = SourceContinuityStore(path)
            state = reopened.record_success(
                "cursor-source",
                now="2026-09-21T08:02:00+00:00",
                cursor="token-3",
                witness=ProviderContinuityWitness("token-1", "token-3"),
            )
            self.assertEqual(state.status, "unknown")
            self.assertEqual(state.trusted_token, "token-1")
            self.assertEqual(state.reason, "provider_native_evidence_required")

    def test_provider_failure_keeps_only_unverified_resume_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.record_success(
                "cursor-source",
                now="2026-09-21T08:00:00+00:00",
                cursor="token-1",
                witness=ProviderContinuityWitness(None, "token-1"),
            )
            store.record_success(
                "cursor-source",
                now="2026-09-21T08:01:00+00:00",
                cursor="token-2",
                witness=ProviderContinuityWitness("token-1", "token-2"),
            )

            failed = store.record_failure(
                "cursor-source", now="2026-09-21T08:02:00+00:00"
            )
            self.assertEqual(failed.status, "unknown")
            self.assertEqual(failed.trusted_token, "token-1")
            self.assertEqual(
                failed.reason,
                "provider_failure_since_last_continuity_proof",
            )

            recovered = store.record_success(
                "cursor-source",
                now="2026-09-21T08:03:00+00:00",
                cursor="token-3",
                witness=ProviderContinuityWitness("token-1", "token-3"),
            )
            self.assertEqual(recovered.status, "unknown")
            self.assertEqual(recovered.trusted_token, "token-1")
            self.assertEqual(recovered.reason, "provider_native_evidence_required")

    def test_resume_token_mismatch_fails_closed_without_moving_trusted_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.record_success(
                "cursor-source",
                now="2026-09-21T08:00:00+00:00",
                cursor="token-1",
                witness=ProviderContinuityWitness(None, "token-1"),
            )
            store.record_success(
                "cursor-source",
                now="2026-09-21T08:01:00+00:00",
                cursor="token-2",
                witness=ProviderContinuityWitness("token-1", "token-2"),
            )

            mismatched = store.record_success(
                "cursor-source",
                now="2026-09-21T08:02:00+00:00",
                cursor="token-99",
                witness=ProviderContinuityWitness("wrong-token", "token-99"),
            )
            self.assertEqual(mismatched.status, "unknown")
            self.assertEqual(mismatched.trusted_token, "token-1")
            self.assertEqual(mismatched.reason, "witness_previous_token_mismatch")

    def test_incomplete_backfill_does_not_advance_trusted_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.record_success(
                "cursor-source",
                now="2026-09-21T08:00:00+00:00",
                cursor="token-1",
                witness=ProviderContinuityWitness(None, "token-1"),
            )
            incomplete = store.record_success(
                "cursor-source",
                now="2026-09-21T08:01:00+00:00",
                cursor="token-2",
                witness=ProviderContinuityWitness(
                    "token-1", "token-2", backfill_complete=False
                ),
            )
            self.assertEqual(incomplete.status, "unknown")
            self.assertEqual(incomplete.trusted_token, "token-1")
            self.assertEqual(incomplete.reason, "provider_backfill_incomplete")

    def test_tokens_are_opaque_and_numeric_contiguity_is_never_invented(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            store.record_success(
                "opaque-source",
                now="2026-09-21T08:00:00+00:00",
                cursor="zeta",
                witness=ProviderContinuityWitness(None, "zeta"),
            )
            state = store.record_success(
                "opaque-source",
                now="2026-09-21T08:01:00+00:00",
                cursor="alpha",
                witness=ProviderContinuityWitness("zeta", "alpha"),
            )
            self.assertEqual(state.status, "unknown")
            self.assertEqual(state.trusted_token, "zeta")
            self.assertEqual(state.reason, "provider_native_evidence_required")

    def test_current_token_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = self._store(tmp)
            state = store.record_success(
                "cursor-source",
                now="2026-09-21T08:00:00+00:00",
                cursor="batch-cursor",
                witness=ProviderContinuityWitness(None, "different-token"),
            )
            self.assertEqual(state.status, "unknown")
            self.assertIsNone(state.trusted_token)
            self.assertEqual(state.reason, "witness_current_token_mismatch")

    def test_corrupt_store_is_rejected_fail_closed(self):
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
                                "trusted_token": None,
                                "last_observed_cursor": "cursor",
                                "last_success_at": "2026-09-21T08:00:00+00:00",
                                "last_failure_at": None,
                                "reason": "forged",
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

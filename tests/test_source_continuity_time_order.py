import tempfile
import unittest
from pathlib import Path

from autosport.source_continuity import (
    ProviderContinuityWitness,
    SourceContinuityStore,
)


class SourceContinuityEvidenceTimeTests(unittest.TestCase):
    def test_older_success_cannot_override_newer_failure_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source_continuity.json"
            store = SourceContinuityStore(path)
            store.record_success(
                "source",
                now="2026-09-21T08:00:00+00:00",
                cursor="token-1",
                witness=ProviderContinuityWitness(None, "token-1"),
            )
            store.record_failure("source", now="2026-09-21T08:02:00+00:00")

            reopened = SourceContinuityStore(path)
            with self.assertRaisesRegex(
                ValueError, "cannot move backwards in evidence time"
            ):
                reopened.record_success(
                    "source",
                    now="2026-09-21T08:01:00+00:00",
                    cursor="token-2",
                    witness=ProviderContinuityWitness("token-1", "token-2"),
                )

            state = reopened.get("source")
            self.assertEqual(state.status, "unknown")
            self.assertEqual(state.trusted_token, "token-1")
            self.assertEqual(state.last_failure_at, "2026-09-21T08:02:00+00:00")

    def test_older_failure_cannot_rewrite_newer_unverified_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SourceContinuityStore(Path(tmp) / "source_continuity.json")
            store.record_success(
                "source",
                now="2026-09-21T08:00:00+00:00",
                cursor="token-1",
                witness=ProviderContinuityWitness(None, "token-1"),
            )
            store.record_success(
                "source",
                now="2026-09-21T08:02:00+00:00",
                cursor="token-2",
                witness=ProviderContinuityWitness("token-1", "token-2"),
            )

            with self.assertRaisesRegex(
                ValueError, "cannot move backwards in evidence time"
            ):
                store.record_failure("source", now="2026-09-21T08:01:00+00:00")

            state = store.get("source")
            self.assertEqual(state.status, "unknown")
            self.assertEqual(state.trusted_token, "token-1")
            self.assertEqual(state.reason, "provider_native_evidence_required")
            self.assertIsNone(state.last_failure_at)


if __name__ == "__main__":
    unittest.main()

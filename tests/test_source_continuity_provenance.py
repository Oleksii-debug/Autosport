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


if __name__ == "__main__":
    unittest.main()

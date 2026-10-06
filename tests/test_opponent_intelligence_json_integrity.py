from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from autosport.opponent_intelligence import (
    OpponentIntelligenceError,
    OpponentIntelligenceStore,
)
from autosport.participant_identity import ParticipantIdentityRegistry


class OpponentIntelligenceJsonIntegrityTests(unittest.TestCase):
    def test_duplicate_schema_key_fails_closed_before_last_wins_normalization(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            identity_registry = ParticipantIdentityRegistry.initialize_pristine(
                root / "identity.json"
            )
            store_path = root / "opponents.json"
            store_path.write_text(
                """{
  "schema": "tampered.invalid-schema",
  "schema": "autosport.opponent_intelligence",
  "version": 1,
  "performances": [],
  "rating_snapshots": [],
  "feature_snapshots": [],
  "invalidations": []
}
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                OpponentIntelligenceError,
                r"duplicate JSON object key: schema",
            ):
                OpponentIntelligenceStore(store_path, identity_registry)


if __name__ == "__main__":
    unittest.main()

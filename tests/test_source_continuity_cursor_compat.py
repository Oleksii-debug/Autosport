import tempfile
import unittest
from pathlib import Path

from autosport.source_continuity import SourceContinuityStore


class SourceContinuityCursorCompatibilityTests(unittest.TestCase):
    def test_snapshot_only_legacy_cursor_strings_remain_valid_opaque_telemetry(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SourceContinuityStore(Path(tmp) / "source_continuity.json")
            for index, cursor in enumerate(("", "   ")):
                with self.subTest(cursor=repr(cursor)):
                    state = store.record_success(
                        "snapshot-source",
                        now=f"2026-09-21T08:0{index}:00+00:00",
                        cursor=cursor,
                        witness=None,
                    )
                    self.assertEqual(state.status, "unknown")
                    self.assertEqual(state.last_observed_cursor, cursor)
                    self.assertIsNone(state.trusted_token)


if __name__ == "__main__":
    unittest.main()

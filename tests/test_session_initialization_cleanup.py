import tempfile
import unittest
from unittest.mock import Mock, patch

from autosport.session import AutosportSession


class SessionInitializationCleanupTests(unittest.TestCase):
    def test_source_health_initialization_failure_closes_market_store(self):
        market_store = Mock()

        with tempfile.TemporaryDirectory() as tmp, patch(
            "autosport.session.SQLiteMarketStore",
            return_value=market_store,
        ), patch(
            "autosport.session.SourceHealthStore",
            side_effect=OSError("source health unavailable"),
        ):
            with self.assertRaisesRegex(OSError, "source health unavailable"):
                AutosportSession(tmp)

        market_store.close.assert_called_once_with()

    def test_late_registry_initialization_failure_closes_market_store(self):
        market_store = Mock()

        with tempfile.TemporaryDirectory() as tmp, patch(
            "autosport.session.SQLiteMarketStore",
            return_value=market_store,
        ), patch(
            "autosport.session.RunRegistry.initialize_pristine",
            side_effect=ValueError("invalid registry"),
        ):
            with self.assertRaisesRegex(ValueError, "invalid registry"):
                AutosportSession(tmp)

        market_store.close.assert_called_once_with()

    def test_cleanup_failure_does_not_replace_primary_initialization_error(self):
        market_store = Mock()
        market_store.close.side_effect = RuntimeError("sqlite close failed")

        with tempfile.TemporaryDirectory() as tmp, patch(
            "autosport.session.SQLiteMarketStore",
            return_value=market_store,
        ), patch(
            "autosport.session.SourceHealthStore",
            side_effect=OSError("source health unavailable"),
        ):
            with self.assertRaisesRegex(OSError, "source health unavailable") as caught:
                AutosportSession(tmp)

        market_store.close.assert_called_once_with()
        notes = getattr(caught.exception, "__notes__", ())
        self.assertEqual(len(notes), 1)
        self.assertIn("SQLiteMarketStore cleanup also failed", notes[0])
        self.assertIn("RuntimeError: sqlite close failed", notes[0])


if __name__ == "__main__":
    unittest.main()

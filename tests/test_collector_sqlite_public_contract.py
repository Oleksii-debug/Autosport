import sqlite3
import tempfile
import unittest
from pathlib import Path
from typing import get_type_hints

from autosport.causal_collector import (
    CollectorDeltaStore,
    DesktopDeltaConsumer,
)


class CollectorSQLitePublicContractTests(unittest.TestCase):
    def test_replacement_store_keeps_public_type_and_annotation_identity(self):
        self.assertEqual(
            CollectorDeltaStore.__module__,
            "autosport.causal_collector",
        )
        hints = get_type_hints(DesktopDeltaConsumer.__init__)
        self.assertIs(hints["collector"], CollectorDeltaStore)

    def test_steady_state_journal_keeps_store_byte_budget_on_canonical_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "collector.json"
            CollectorDeltaStore(path)
            connection = sqlite3.connect(path)
            try:
                mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(str(mode).lower(), "delete")
            self.assertFalse(path.with_name(path.name + "-wal").exists())
            self.assertFalse(path.with_name(path.name + "-shm").exists())


if __name__ == "__main__":
    unittest.main()

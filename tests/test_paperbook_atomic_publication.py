import os
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from autosport.domain import TicketLeg
from autosport.paper import PaperBook


class PaperBookAtomicPublicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = self.root / "paper_book.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    @staticmethod
    def _book() -> PaperBook:
        book = PaperBook("100")
        book.open_ticket(
            [TicketLeg("event-1", "winner", "alice", Decimal("2"))],
            "10",
            reason="atomic-publication",
            placed_at="2026-09-14T00:00:00+00:00",
        )
        return book

    def test_save_does_not_follow_preexisting_predictable_temp_hardlink(self) -> None:
        book = self._book()
        outside = self.root / "outside.txt"
        sentinel = b"outside-bytes-must-not-change"
        outside.write_bytes(sentinel)
        predictable_temp = self.path.with_suffix(self.path.suffix + ".tmp")
        try:
            os.link(outside, predictable_temp)
        except OSError as exc:
            self.skipTest(f"hard links unavailable on this filesystem: {exc}")

        book.save(self.path)

        self.assertEqual(outside.read_bytes(), sentinel)
        self.assertEqual(predictable_temp.read_bytes(), sentinel)
        self.assertTrue(os.path.samefile(outside, predictable_temp))
        self.assertFalse(self.path.is_symlink())
        restored = PaperBook.load(self.path)
        self.assertEqual(restored.balance, Decimal("90"))
        self.assertEqual(len(restored.tickets), 1)

    def test_publish_failure_preserves_last_good_snapshot_and_cleans_unique_temp(self) -> None:
        book = self._book()
        book.save(self.path)
        last_good = self.path.read_bytes()

        with patch("autosport.paper.os.replace", side_effect=OSError("injected replace failure")):
            with self.assertRaisesRegex(OSError, "injected replace failure"):
                book.save(self.path)

        self.assertEqual(self.path.read_bytes(), last_good)
        temporary_files = tuple(
            path
            for path in self.root.iterdir()
            if path.name.startswith(f".{self.path.name}.") and path.name.endswith(".tmp")
        )
        self.assertEqual(temporary_files, ())


if __name__ == "__main__":
    unittest.main()

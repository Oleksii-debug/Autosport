from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from autosport._paper_execution_append_recovery import (
    _load_live_pre_action_book,
    _load_pre_action_path,
)
from autosport.paper import PaperBook
from autosport.paper_execution_adoption import PaperExecutionAdoptionError


class PaperPreActionWitnessCorruptionTests(unittest.TestCase):
    def test_truncated_pre_action_witness_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "live_decision_pre_action_book.json"
            path.write_text('{"schema_version":', encoding="utf-8")

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "live recovery pre-action PaperBook is unreadable",
            ):
                _load_pre_action_path(path, label="live recovery")

    def test_duplicate_json_key_pre_action_witness_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "live_decision_pre_action_book.json"
            path.write_text('{"matches":0,"matches":1}', encoding="utf-8")

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "live recovery pre-action PaperBook is unreadable",
            ):
                _load_pre_action_path(path, label="live recovery")

    def test_live_loader_reads_exact_valid_durable_witness(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "live_decision_pre_action_book.json"
            expected = PaperBook("100.00")
            expected.save(path)
            runtime = SimpleNamespace(paper_book_path=root / "paper_book.json")

            observed = _load_live_pre_action_book(runtime)

            self.assertIsNotNone(observed)
            assert observed is not None
            self.assertEqual(observed.to_dict(), expected.to_dict())

    def test_live_loader_returns_none_only_when_witness_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = SimpleNamespace(paper_book_path=root / "paper_book.json")

            self.assertIsNone(_load_live_pre_action_book(runtime))


if __name__ == "__main__":
    unittest.main()

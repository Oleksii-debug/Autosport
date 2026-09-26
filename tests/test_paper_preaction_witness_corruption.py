from __future__ import annotations

import json
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
            expected = PaperBook("100.00")
            expected.save(path)
            canonical = path.read_text(encoding="utf-8")
            canonical_payload = json.loads(canonical)
            stripped = canonical.lstrip()
            self.assertTrue(stripped.startswith("{"))

            duplicate = (
                '{"schema_version":'
                + json.dumps(canonical_payload["schema_version"])
                + ","
                + stripped[1:]
            )
            # Under an ordinary permissive JSON parser, the duplicate same-value
            # key collapses to the exact valid PaperBook payload.  Therefore the
            # failure below specifically exercises PaperBook's duplicate-key
            # rejection rather than a later schema/content validation failure.
            self.assertEqual(json.loads(duplicate), canonical_payload)
            path.write_text(duplicate, encoding="utf-8")

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "live recovery pre-action PaperBook is unreadable",
            ):
                _load_pre_action_path(path, label="live recovery")


    def test_invalid_utf8_pre_action_witness_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "live_decision_pre_action_book.json"
            path.write_bytes(b'{"schema_version":4,"corrupt":"\\xff"}')

            with self.assertRaisesRegex(
                PaperExecutionAdoptionError,
                "live recovery pre-action PaperBook is unreadable",
            ):
                _load_pre_action_path(path, label="live recovery")

    def test_nonfinite_json_constant_pre_action_witness_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "live_decision_pre_action_book.json"
            expected = PaperBook("100.00")
            expected.save(path)
            canonical = path.read_text(encoding="utf-8")
            mutated = canonical.replace(
                '"balance": "100.00"',
                '"balance": NaN',
                1,
            )
            self.assertNotEqual(mutated, canonical)
            path.write_text(mutated, encoding="utf-8")

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
            roundtrip = root / "roundtrip_pre_action_book.json"
            observed.save(roundtrip)
            self.assertEqual(roundtrip.read_bytes(), path.read_bytes())

    def test_live_loader_returns_none_only_when_witness_is_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runtime = SimpleNamespace(paper_book_path=root / "paper_book.json")

            self.assertIsNone(_load_live_pre_action_book(runtime))


if __name__ == "__main__":
    unittest.main()

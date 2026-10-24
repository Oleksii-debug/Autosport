"""Regression: JSON exponent overflow must never be accepted as finite evidence."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.qualify_exact_actions import GateError, _load


class NonfiniteExponentTests(unittest.TestCase):
    def parse(self, raw: str):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text(raw, encoding="utf-8")
            return _load(str(path))

    def test_positive_exponent_overflow_is_blocked(self):
        with self.assertRaisesRegex(GateError, "non-finite"):
            self.parse('{"extra":1e999}')

    def test_negative_exponent_overflow_is_blocked(self):
        with self.assertRaisesRegex(GateError, "non-finite"):
            self.parse('{"extra":-1e999}')

    def test_nested_exponent_overflow_is_blocked(self):
        with self.assertRaisesRegex(GateError, "non-finite"):
            self.parse('{"jobs":[{"step":{"value":1e999}}]}')

    def test_finite_decimal_is_accepted(self):
        self.assertEqual(self.parse('{"extra":1.5}'), {"extra":1.5})

    def test_existing_nonstandard_nan_is_still_blocked(self):
        with self.assertRaisesRegex(GateError, "non-finite"):
            self.parse('{"extra":NaN}')


if __name__ == "__main__":
    unittest.main()

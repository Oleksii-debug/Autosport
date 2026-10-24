"""Regression: deeply nested REST evidence must fail closed without a traceback."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.qualify_exact_actions import GateError, _load


class JsonNestingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "deep.json"

    def test_deep_array_is_rejected_with_stable_reason(self):
        self.path.write_text("[" * 12000 + "0" + "]" * 12000, encoding="utf-8")
        with self.assertRaisesRegex(GateError, "JSON nesting exceeds supported depth"):
            _load(str(self.path))

    def test_deep_object_is_rejected_with_stable_reason(self):
        self.path.write_text('{"a":' * 12000 + "0" + "}" * 12000, encoding="utf-8")
        with self.assertRaisesRegex(GateError, "JSON nesting exceeds supported depth"):
            _load(str(self.path))

    def test_cli_returns_structured_blocked_without_traceback(self):
        self.path.write_text("[" * 12000 + "0" + "]" * 12000, encoding="utf-8")
        script = Path(__file__).resolve().parents[1] / "scripts" / "qualify_exact_actions.py"
        result = subprocess.run([
            sys.executable, "-I", str(script), "--sha", "a" * 40,
            "--ci-run", str(self.path), "--ci-jobs", str(self.path),
            "--windows-run", str(self.path), "--windows-jobs", str(self.path),
        ], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr, "")
        self.assertEqual(json.loads(result.stdout), {
            "status": "BLOCKED", "reason": "JSON nesting exceeds supported depth"
        })


if __name__ == "__main__":
    unittest.main()

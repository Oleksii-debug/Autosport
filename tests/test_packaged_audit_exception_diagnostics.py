import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport import accessibility_audit, keyboard_audit, research_demo_audit


class _BrokenStringError(Exception):
    def __str__(self) -> str:
        raise RuntimeError("diagnostic rendering must not escape")


class PackagedAuditExceptionDiagnosticTests(unittest.TestCase):
    def _assert_fail_closed_payload(self, payload: dict[str, object], error: str) -> None:
        self.assertEqual(payload["status"], "FAIL")
        self.assertFalse(payload["human_tested"])
        self.assertFalse(payload["nvda_verified"])
        self.assertFalse(payload["real_money_execution"])
        if "failures" in payload:
            self.assertEqual(payload["failures"], [error])
        else:
            self.assertEqual(payload["error"], error)

    def test_accessibility_broken_exception_stringification_still_publishes_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "accessibility-audit.json"
            with patch.object(
                accessibility_audit,
                "WindowsAutosportApp",
                side_effect=_BrokenStringError(),
            ):
                self.assertEqual(accessibility_audit.run_accessibility_audit(output), 1)
            payload = json.loads(output.read_text(encoding="utf-8"))

        self._assert_fail_closed_payload(
            payload,
            "_BrokenStringError: exception details unavailable",
        )

    def test_keyboard_broken_exception_stringification_still_publishes_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "keyboard-audit.json"
            with patch.object(
                keyboard_audit,
                "WindowsAutosportApp",
                side_effect=_BrokenStringError(),
            ):
                self.assertEqual(keyboard_audit.run_keyboard_audit(output), 1)
            payload = json.loads(output.read_text(encoding="utf-8"))

        self._assert_fail_closed_payload(
            payload,
            "_BrokenStringError: exception details unavailable",
        )

    def test_research_demo_broken_exception_stringification_still_publishes_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "research-demo-audit.json"
            with patch.object(
                research_demo_audit,
                "load_dataset",
                side_effect=_BrokenStringError(),
            ):
                self.assertEqual(
                    research_demo_audit.run_research_demo_audit(output, root / "demo"),
                    1,
                )
            payload = json.loads(output.read_text(encoding="utf-8"))

        self._assert_fail_closed_payload(
            payload,
            "_BrokenStringError: exception details unavailable",
        )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

from autosport.windows_entry import main


class WindowsEntrypointFailClosedTests(unittest.TestCase):
    @staticmethod
    def _failing_webview_module() -> types.ModuleType:
        module = types.ModuleType("autosport.windows_webview_shell")

        class WindowsWebViewUnavailable(RuntimeError):
            pass

        def unexpected_start() -> int:
            raise AssertionError("malformed packaged args must fail before WebView2 shell import")

        module.WindowsWebViewUnavailable = WindowsWebViewUnavailable
        module.main = unexpected_start
        return module

    def test_unknown_packaged_argument_fails_before_webview_shell(self) -> None:
        with patch.dict(
            sys.modules,
            {"autosport.windows_webview_shell": self._failing_webview_module()},
        ):
            self.assertEqual(main(["--not-a-real-autosport-mode"]), 2)

    def test_known_machine_mode_with_wrong_arity_fails_before_webview_shell(self) -> None:
        with patch.dict(
            sys.modules,
            {"autosport.windows_webview_shell": self._failing_webview_module()},
        ):
            self.assertEqual(main(["--diagnostic-output"]), 2)
            self.assertEqual(
                main(["--research-demo-audit-output", "only-one-path"]),
                2,
            )

    def test_no_args_preserves_normal_webview_startup(self) -> None:
        calls: list[str] = []
        fake_shell = types.ModuleType("autosport.windows_webview_shell")

        class WindowsWebViewUnavailable(RuntimeError):
            pass

        def shell_main() -> int:
            calls.append("webview")
            return 17

        fake_shell.WindowsWebViewUnavailable = WindowsWebViewUnavailable
        fake_shell.main = shell_main
        with patch.dict(
            sys.modules,
            {"autosport.windows_webview_shell": fake_shell},
        ):
            self.assertEqual(main([]), 17)

        self.assertEqual(calls, ["webview"])

    def test_valid_machine_mode_dispatches_without_legacy_layout_install(self) -> None:
        calls: list[str] = []
        fake_diagnostic = types.ModuleType("autosport.diagnostic")

        def run_machine_diagnostic(output: str) -> int:
            calls.append(f"diagnostic:{output}")
            return 23

        fake_diagnostic.run_machine_diagnostic = run_machine_diagnostic
        with patch.dict(
            sys.modules,
            {"autosport.diagnostic": fake_diagnostic},
        ):
            self.assertEqual(main(["--diagnostic-output", "report.json"]), 23)

        self.assertEqual(calls, ["diagnostic:report.json"])

    def test_semantic_audit_flags_route_to_webview_audit_module(self) -> None:
        calls: list[str] = []
        fake_audit = types.ModuleType("autosport.windows_webview_audit")

        def accessibility(output: str) -> int:
            calls.append(f"a11y:{output}")
            return 31

        def keyboard(output: str) -> int:
            calls.append(f"keyboard:{output}")
            return 32

        fake_audit.run_accessibility_audit = accessibility
        fake_audit.run_keyboard_audit = keyboard
        with patch.dict(
            sys.modules,
            {"autosport.windows_webview_audit": fake_audit},
        ):
            self.assertEqual(main(["--accessibility-audit-output", "a.json"]), 31)
            self.assertEqual(main(["--keyboard-audit-output", "k.json"]), 32)

        self.assertEqual(calls, ["a11y:a.json", "keyboard:k.json"])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import types
import unittest
from unittest.mock import patch

from autosport.windows_entry import main


class WindowsEntrypointFailClosedTests(unittest.TestCase):
    @staticmethod
    def _failing_layout_module() -> types.ModuleType:
        module = types.ModuleType("autosport.windows_layout")

        def unexpected_install() -> None:
            raise AssertionError("malformed packaged args must fail before GUI/layout import")

        module.install_compact_windows_layout = unexpected_install
        return module

    def test_unknown_packaged_argument_fails_before_gui_layout(self) -> None:
        fake_gui = types.ModuleType("autosport.windows_gui")

        def unexpected_gui() -> int:
            raise AssertionError("unknown packaged arg must not start GUI")

        fake_gui.main = unexpected_gui
        with patch.dict(
            sys.modules,
            {
                "autosport.windows_layout": self._failing_layout_module(),
                "autosport.windows_gui": fake_gui,
            },
        ):
            self.assertEqual(main(["--not-a-real-autosport-mode"]), 2)

    def test_known_machine_mode_with_wrong_arity_fails_before_gui_layout(self) -> None:
        with patch.dict(
            sys.modules,
            {"autosport.windows_layout": self._failing_layout_module()},
        ):
            self.assertEqual(main(["--diagnostic-output"]), 2)
            self.assertEqual(
                main(["--research-demo-audit-output", "only-one-path"]),
                2,
            )

    def test_no_args_preserves_normal_gui_startup_after_layout_install(self) -> None:
        calls: list[str] = []
        fake_layout = types.ModuleType("autosport.windows_layout")
        fake_gui = types.ModuleType("autosport.windows_gui")

        def install() -> None:
            calls.append("layout")

        def gui_main() -> int:
            calls.append("gui")
            return 17

        fake_layout.install_compact_windows_layout = install
        fake_gui.main = gui_main
        with patch.dict(
            sys.modules,
            {
                "autosport.windows_layout": fake_layout,
                "autosport.windows_gui": fake_gui,
            },
        ):
            self.assertEqual(main([]), 17)

        self.assertEqual(calls, ["layout", "gui"])

    def test_valid_machine_mode_keeps_layout_before_dispatch(self) -> None:
        calls: list[str] = []
        fake_layout = types.ModuleType("autosport.windows_layout")
        fake_diagnostic = types.ModuleType("autosport.diagnostic")

        def install() -> None:
            calls.append("layout")

        def run_machine_diagnostic(output: str) -> int:
            calls.append(f"diagnostic:{output}")
            return 23

        fake_layout.install_compact_windows_layout = install
        fake_diagnostic.run_machine_diagnostic = run_machine_diagnostic
        with patch.dict(
            sys.modules,
            {
                "autosport.windows_layout": fake_layout,
                "autosport.diagnostic": fake_diagnostic,
            },
        ):
            self.assertEqual(main(["--diagnostic-output", "report.json"]), 23)

        self.assertEqual(calls, ["layout", "diagnostic:report.json"])


if __name__ == "__main__":
    unittest.main()

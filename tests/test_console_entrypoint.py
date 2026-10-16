import sys
import unittest
from types import ModuleType
from unittest.mock import Mock, patch

from autosport import console_entrypoint


def _gui_modules(*, result: int) -> tuple[ModuleType, ModuleType, Mock, Mock]:
    canonical = ModuleType("autosport.windows_entry")
    canonical_main = Mock(return_value=result)
    canonical.main = canonical_main

    legacy = ModuleType("autosport.windows_gui")
    legacy_main = Mock(side_effect=AssertionError("legacy Tk GUI must not be routed"))
    legacy.main = legacy_main
    return canonical, legacy, canonical_main, legacy_main


class ConsoleEntrypointTests(unittest.TestCase):
    def test_gui_routes_to_canonical_webview2_windows_entry(self):
        canonical, legacy, canonical_main, legacy_main = _gui_modules(result=17)

        with patch.dict(
            sys.modules,
            {
                "autosport.windows_entry": canonical,
                "autosport.windows_gui": legacy,
            },
        ):
            result = console_entrypoint.main(["gui"])

        self.assertEqual(result, 17)
        canonical_main.assert_called_once_with([])
        legacy_main.assert_not_called()

    def test_non_gui_command_delegates_unchanged_to_existing_cli(self):
        argv = ["demo"]
        with patch("autosport.cli.main", return_value=23) as cli_main:
            result = console_entrypoint.main(argv)

        self.assertEqual(result, 23)
        cli_main.assert_called_once_with(argv)

    def test_gui_with_extra_arguments_remains_owned_by_cli_parser(self):
        argv = ["gui", "--help"]
        with patch("autosport.cli.main", return_value=29) as cli_main:
            result = console_entrypoint.main(argv)

        self.assertEqual(result, 29)
        cli_main.assert_called_once_with(argv)

    def test_process_arguments_consume_gui_before_windows_entry(self):
        canonical, legacy, canonical_main, legacy_main = _gui_modules(result=31)

        with (
            patch("sys.argv", ["autosport", "gui"]),
            patch.dict(
                sys.modules,
                {
                    "autosport.windows_entry": canonical,
                    "autosport.windows_gui": legacy,
                },
            ),
        ):
            result = console_entrypoint.main()

        self.assertEqual(result, 31)
        canonical_main.assert_called_once_with([])
        legacy_main.assert_not_called()


if __name__ == "__main__":
    unittest.main()

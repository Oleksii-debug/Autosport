import unittest
from unittest.mock import patch

from autosport import console_entrypoint


class ConsoleEntrypointTests(unittest.TestCase):
    def test_gui_routes_to_windows_product_surface(self):
        with patch("autosport.windows_gui.main", return_value=17) as windows_main:
            result = console_entrypoint.main(["gui"])

        self.assertEqual(result, 17)
        windows_main.assert_called_once_with()

    def test_non_gui_command_delegates_unchanged_to_existing_cli(self):
        argv = ["demo"]
        with patch("autosport.cli.main", return_value=23) as cli_main:
            result = console_entrypoint.main(argv)

        self.assertEqual(result, 23)
        cli_main.assert_called_once_with(argv)

    def test_process_arguments_are_used_when_argv_is_omitted(self):
        with (
            patch("sys.argv", ["autosport", "gui"]),
            patch("autosport.windows_gui.main", return_value=31) as windows_main,
        ):
            result = console_entrypoint.main()

        self.assertEqual(result, 31)
        windows_main.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()

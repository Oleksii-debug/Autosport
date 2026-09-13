import unittest
from unittest.mock import patch

from autosport import data_tools_entry


class DataToolsEntryTests(unittest.TestCase):
    def test_help_is_success_without_gui(self):
        with patch("builtins.print") as output:
            self.assertEqual(data_tools_entry.main(["--help"]), 0)
        help_text = output.call_args.args[0]
        self.assertIn("portable Windows historical-data tools", help_text)
        self.assertIn("walk-forward-evaluate", help_text)
        self.assertIn("import-betfair-historical", help_text)
        self.assertIn("repair-workspace", help_text)

    def test_acquire_dispatches_exact_arguments(self):
        with patch("autosport.historical_acquisition.main", return_value=7) as target:
            result = data_tools_entry.main(["acquire", "--at", "2026-01-01T00:00:00Z"])
        self.assertEqual(result, 7)
        target.assert_called_once_with(["--at", "2026-01-01T00:00:00Z"])

    def test_betfair_historical_import_dispatches_exact_arguments(self):
        with patch("autosport.betfair_historical_import.main", return_value=12) as target:
            result = data_tools_entry.main(
                [
                    "import-betfair-historical",
                    "market.bz2",
                    "--output-dir",
                    "dataset",
                ]
            )
        self.assertEqual(result, 12)
        target.assert_called_once_with(["market.bz2", "--output-dir", "dataset"])

    def test_build_corpus_dispatches_exact_arguments(self):
        with patch("autosport.historical_corpus.main", return_value=8) as target:
            result = data_tools_entry.main(["build-corpus", "--name", "real-corpus"])
        self.assertEqual(result, 8)
        target.assert_called_once_with(["--name", "real-corpus"])

    def test_build_corpus_from_bundle_dispatches_exact_arguments(self):
        with patch("autosport.historical_bundle_corpus.main", return_value=10) as target:
            result = data_tools_entry.main(["build-corpus-from-bundle", "bundle-dir", "--bundle-sha256", "a" * 64])
        self.assertEqual(result, 10)
        target.assert_called_once_with(["bundle-dir", "--bundle-sha256", "a" * 64])

    def test_verify_dataset_uses_canonical_cli_validator(self):
        with patch("autosport.cli.main", return_value=9) as target:
            result = data_tools_entry.main(["verify-dataset", "dataset-dir"])
        self.assertEqual(result, 9)
        target.assert_called_once_with(["verify-dataset", "dataset-dir"])

    def test_walk_forward_evaluate_uses_canonical_cli_evaluator(self):
        with patch("autosport.cli.main", return_value=11) as target:
            result = data_tools_entry.main(
                ["walk-forward-evaluate", "evaluation.json", "--output", "report.json"]
            )
        self.assertEqual(result, 11)
        target.assert_called_once_with(
            ["walk-forward-evaluate", "evaluation.json", "--output", "report.json"]
        )

    def test_repair_workspace_uses_canonical_fail_closed_cli_recovery(self):
        with patch("autosport.cli.main", return_value=4) as target:
            result = data_tools_entry.main(
                ["repair-workspace", "--workspace", r"C:\\Autosport\\state"]
            )
        self.assertEqual(result, 4)
        target.assert_called_once_with(
            ["repair-workspace", "--workspace", r"C:\\Autosport\\state"]
        )

    def test_unknown_command_fails_closed(self):
        with patch("builtins.print"):
            self.assertEqual(data_tools_entry.main(["unknown"]), 2)


if __name__ == "__main__":
    unittest.main()

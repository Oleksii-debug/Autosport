import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
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
        self.assertIn("checksum-bound rights/retention evidence", help_text)
        self.assertIn("not an independent legal opinion", help_text)

    def test_acquire_dispatches_exact_arguments(self):
        with patch("autosport.historical_acquisition.main", return_value=7) as target:
            result = data_tools_entry.main(["acquire", "--at", "2026-01-01T00:00:00Z"])
        self.assertEqual(result, 7)
        target.assert_called_once_with(["--at", "2026-01-01T00:00:00Z"])

    def test_betfair_historical_import_dispatches_exact_arguments(self):
        with patch("autosport.betfair_historical_read_once.main", return_value=12) as target:
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

    def test_build_corpus_dispatches_through_governance_gate(self):
        with patch("autosport.historical_governance.corpus_main", return_value=8) as target:
            result = data_tools_entry.main(["build-corpus", "--name", "real-corpus"])
        self.assertEqual(result, 8)
        target.assert_called_once_with(["--name", "real-corpus"])

    def test_build_corpus_from_bundle_dispatches_through_governance_gate(self):
        with patch("autosport.historical_governance.bundle_corpus_main", return_value=10) as target:
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

    def test_real_missing_dataset_is_fail_closed_at_portable_boundary(self):
        stderr = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing-dataset"
            with redirect_stderr(stderr):
                result = data_tools_entry.main(["verify-dataset", str(missing)])

        self.assertEqual(result, 3)
        self.assertTrue(
            stderr.getvalue().strip().startswith(
                "Autosport-Data: verify-dataset=FAIL_CLOSED error="
            )
        )
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_expected_validation_failure_is_contained_without_traceback(self):
        stderr = io.StringIO()
        with patch(
            "autosport.cli.main",
            side_effect=ValueError("invalid dataset\nsecond diagnostic line"),
        ):
            with redirect_stderr(stderr):
                result = data_tools_entry.main(["verify-dataset", "broken-dataset"])

        self.assertEqual(result, 3)
        self.assertEqual(
            stderr.getvalue().strip(),
            "Autosport-Data: verify-dataset=FAIL_CLOSED error=ValueError: "
            "invalid dataset second diagnostic line",
        )
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_expected_filesystem_failure_is_contained_without_traceback(self):
        stderr = io.StringIO()
        with patch(
            "autosport.cli.main",
            side_effect=FileNotFoundError("dataset file is missing"),
        ):
            with redirect_stderr(stderr):
                result = data_tools_entry.main(["verify-dataset", "missing-dataset"])

        self.assertEqual(result, 3)
        self.assertEqual(
            stderr.getvalue().strip(),
            "Autosport-Data: verify-dataset=FAIL_CLOSED error=FileNotFoundError: "
            "dataset file is missing",
        )
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_expected_failure_with_broken_stringifier_is_still_fail_closed(self):
        class BrokenStringValueError(ValueError):
            def __str__(self):
                raise RuntimeError("diagnostic formatter failed")

        stderr = io.StringIO()
        with patch(
            "autosport.cli.main",
            side_effect=BrokenStringValueError(),
        ):
            with redirect_stderr(stderr):
                result = data_tools_entry.main(["verify-dataset", "broken-dataset"])

        self.assertEqual(result, 3)
        self.assertEqual(
            stderr.getvalue().strip(),
            "Autosport-Data: verify-dataset=FAIL_CLOSED error=BrokenStringValueError: "
            "BrokenStringValueError",
        )
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_expected_failure_with_hostile_type_metadata_is_still_fail_closed(self):
        class BrokenNameMeta(type):
            def __getattribute__(cls, name):
                if name == "__name__":
                    raise RuntimeError("diagnostic type formatter failed")
                return super().__getattribute__(name)

        class BrokenMetadataValueError(ValueError, metaclass=BrokenNameMeta):
            def __str__(self):
                raise RuntimeError("diagnostic string formatter failed")

        stderr = io.StringIO()
        with patch(
            "autosport.cli.main",
            side_effect=BrokenMetadataValueError(),
        ):
            with redirect_stderr(stderr):
                result = data_tools_entry.main(["verify-dataset", "broken-dataset"])

        self.assertEqual(result, 3)
        self.assertEqual(
            stderr.getvalue().strip(),
            "Autosport-Data: verify-dataset=FAIL_CLOSED error=BrokenMetadataValueError: "
            "BrokenMetadataValueError",
        )
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_unexpected_programming_failure_is_not_mislabeled_as_expected_input_error(self):
        with patch(
            "autosport.cli.main",
            side_effect=RuntimeError("programming defect"),
        ):
            with self.assertRaisesRegex(RuntimeError, "programming defect"):
                data_tools_entry.main(["verify-dataset", "dataset-dir"])

    def test_unknown_command_fails_closed(self):
        with patch("builtins.print"):
            self.assertEqual(data_tools_entry.main(["unknown"]), 2)


if __name__ == "__main__":
    unittest.main()

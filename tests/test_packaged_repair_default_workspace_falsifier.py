from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.cli import build_parser
from autosport.data_tools_entry import _dispatch
from autosport.paths import default_workspace


class PackagedRepairDefaultWorkspaceTests(unittest.TestCase):
    @staticmethod
    def _repair_workspace(*extra: str) -> Path:
        args = build_parser().parse_args(["repair-workspace", *extra])
        if args.command != "repair-workspace":
            raise AssertionError("repair-workspace parser selected the wrong command")
        return args.workspace

    def test_packaged_data_tool_delegates_repair_to_canonical_cli(self) -> None:
        with patch("autosport.cli.main", return_value=17) as cli_main:
            code = _dispatch("repair-workspace", [])
        self.assertEqual(code, 17)
        cli_main.assert_called_once_with(["repair-workspace"])

    def test_omitted_workspace_reuses_canonical_explicit_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            expected = (Path(directory) / "canonical-workspace").resolve()
            with patch.dict(
                os.environ,
                {"AUTOSPORT_WORKSPACE": str(expected)},
                clear=True,
            ):
                observed = self._repair_workspace()
                self.assertEqual(observed, default_workspace())
                self.assertTrue(observed.is_absolute())

    def test_omitted_workspace_reuses_localappdata_independent_of_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            local_app_data = root / "Local App Data"
            first_cwd = root / "launch-a"
            second_cwd = root / "launch-b"
            first_cwd.mkdir()
            second_cwd.mkdir()

            with patch.dict(
                os.environ,
                {"LOCALAPPDATA": str(local_app_data)},
                clear=True,
            ):
                expected = default_workspace()
                original_cwd = Path.cwd()
                try:
                    os.chdir(first_cwd)
                    first = self._repair_workspace()
                    os.chdir(second_cwd)
                    second = self._repair_workspace()
                finally:
                    os.chdir(original_cwd)

            self.assertEqual(first, expected)
            self.assertEqual(second, expected)
            self.assertTrue(first.is_absolute())
            self.assertEqual(expected, local_app_data / "Autosport" / "workspace")

    def test_explicit_workspace_remains_operator_selected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            selected = (Path(directory) / "manual repair target").resolve()
            with patch.dict(
                os.environ,
                {"AUTOSPORT_WORKSPACE": "relative-invalid-environment-workspace"},
                clear=True,
            ):
                observed = self._repair_workspace("--workspace", str(selected))
        self.assertEqual(observed, selected)

    def test_invalid_workspace_environment_does_not_break_unrelated_cli_parse(self) -> None:
        with patch.dict(
            os.environ,
            {"AUTOSPORT_WORKSPACE": "relative-invalid-environment-workspace"},
            clear=True,
        ):
            args = build_parser().parse_args(["verify-dataset", "dataset-dir"])

        self.assertEqual(args.command, "verify-dataset")
        self.assertEqual(args.path, Path("dataset-dir"))

    def test_invalid_workspace_environment_fails_closed_when_default_is_needed(self) -> None:
        with patch.dict(
            os.environ,
            {"AUTOSPORT_WORKSPACE": "relative-invalid-environment-workspace"},
            clear=True,
        ):
            with self.assertRaisesRegex(
                ValueError,
                r"AUTOSPORT_WORKSPACE must be an absolute path",
            ):
                self._repair_workspace()


if __name__ == "__main__":
    unittest.main()

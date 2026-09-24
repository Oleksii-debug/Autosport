from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.paths import default_workspace
from autosport.product_entrypoint import _parser


class ProductEntrypointDefaultWorkspaceTests(unittest.TestCase):
    @staticmethod
    def _default_workspace_argument() -> Path:
        return _parser().parse_args(
            ["--source-factory", "autosport_example_source:make_source"]
        ).workspace

    def test_default_workspace_reuses_canonical_explicit_override(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            expected = (Path(directory) / "canonical-product-workspace").resolve()
            with patch.dict(
                os.environ,
                {"AUTOSPORT_WORKSPACE": str(expected)},
                clear=True,
            ):
                observed = self._default_workspace_argument()
                self.assertEqual(observed, default_workspace())
                self.assertTrue(observed.is_absolute())

    def test_explicit_workspace_wins_even_if_environment_default_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            explicit = (Path(directory) / "explicit-workspace").resolve()
            with patch.dict(
                os.environ,
                {"AUTOSPORT_WORKSPACE": "relative-invalid-default"},
                clear=True,
            ):
                observed = _parser().parse_args(
                    [
                        "--workspace",
                        str(explicit),
                        "--source-factory",
                        "autosport_example_source:make_source",
                    ]
                ).workspace

            self.assertEqual(observed, explicit)

    def test_default_workspace_reuses_localappdata_and_is_cwd_independent(self) -> None:
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
                    first = self._default_workspace_argument()
                    os.chdir(second_cwd)
                    second = self._default_workspace_argument()
                finally:
                    os.chdir(original_cwd)

            self.assertEqual(first, expected)
            self.assertEqual(second, expected)
            self.assertTrue(first.is_absolute())
            self.assertEqual(
                expected,
                local_app_data / "Autosport" / "workspace",
            )


if __name__ == "__main__":
    unittest.main()

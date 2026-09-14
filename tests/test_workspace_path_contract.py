from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.paths import default_workspace


class DefaultWorkspaceContractTests(unittest.TestCase):
    def test_absolute_override_is_stable_and_preserves_unicode_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / "дані Autosport" / "workspace"
            with patch.dict(
                os.environ,
                {
                    "AUTOSPORT_WORKSPACE": str(override),
                    "LOCALAPPDATA": str(Path(tmp) / "ignored-local-app-data"),
                },
                clear=True,
            ):
                self.assertEqual(default_workspace(), override)

    def test_relative_override_fails_closed_instead_of_following_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(
                os.environ,
                {
                    "AUTOSPORT_WORKSPACE": "relative/workspace",
                    "LOCALAPPDATA": str(Path(tmp) / "local-app-data"),
                },
                clear=True,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    r"AUTOSPORT_WORKSPACE must be an absolute path",
                ):
                    default_workspace()

    def test_blank_override_preserves_absolute_local_app_data_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local_app_data = Path(tmp) / "Local App Data"
            with patch.dict(
                os.environ,
                {
                    "AUTOSPORT_WORKSPACE": "   ",
                    "LOCALAPPDATA": str(local_app_data),
                },
                clear=True,
            ):
                self.assertEqual(
                    default_workspace(),
                    local_app_data / "Autosport" / "workspace",
                )

    def test_relative_local_app_data_fails_closed_instead_of_following_cwd(self) -> None:
        with patch.dict(
            os.environ,
            {"LOCALAPPDATA": "relative/local-app-data"},
            clear=True,
        ):
            with self.assertRaisesRegex(
                ValueError,
                r"LOCALAPPDATA must be an absolute path",
            ):
                default_workspace()


if __name__ == "__main__":
    unittest.main()

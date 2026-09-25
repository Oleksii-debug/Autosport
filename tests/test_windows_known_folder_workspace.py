from __future__ import annotations

import ctypes
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from autosport import paths as paths_module


class WindowsKnownFolderWorkspaceTests(unittest.TestCase):
    def test_missing_localappdata_uses_known_folder_and_is_cwd_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            known_folder = root / "Користувач з пробілом" / "AppData" / "Local"
            first_cwd = root / "launch-a"
            second_cwd = root / "launch-b"
            first_cwd.mkdir()
            second_cwd.mkdir()

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("autosport.paths.sys.platform", "win32"),
                patch.object(
                    paths_module,
                    "_windows_known_folder_local_app_data",
                    return_value=known_folder,
                ) as resolve_known_folder,
            ):
                original_cwd = Path.cwd()
                try:
                    os.chdir(first_cwd)
                    first = paths_module.default_workspace()
                    os.chdir(second_cwd)
                    second = paths_module.default_workspace()
                finally:
                    os.chdir(original_cwd)

            expected = known_folder / "Autosport" / "workspace"
            self.assertEqual(first, expected)
            self.assertEqual(second, expected)
            self.assertTrue(first.is_absolute())
            self.assertEqual(resolve_known_folder.call_count, 2)

    def test_explicit_autosport_workspace_precedes_windows_known_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            expected = Path(directory).resolve() / "explicit workspace"
            with (
                patch.dict(
                    os.environ,
                    {"AUTOSPORT_WORKSPACE": str(expected)},
                    clear=True,
                ),
                patch("autosport.paths.sys.platform", "win32"),
                patch.object(
                    paths_module,
                    "_windows_known_folder_local_app_data",
                ) as resolve_known_folder,
            ):
                observed = paths_module.default_workspace()

            self.assertEqual(observed, expected)
            resolve_known_folder.assert_not_called()

    def test_absolute_localappdata_precedes_windows_known_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            local_app_data = Path(directory).resolve() / "Local App Data"
            with (
                patch.dict(
                    os.environ,
                    {"LOCALAPPDATA": str(local_app_data)},
                    clear=True,
                ),
                patch("autosport.paths.sys.platform", "win32"),
                patch.object(
                    paths_module,
                    "_windows_known_folder_local_app_data",
                ) as resolve_known_folder,
            ):
                observed = paths_module.default_workspace()

            self.assertEqual(
                observed,
                local_app_data / "Autosport" / "workspace",
            )
            resolve_known_folder.assert_not_called()

    def test_known_folder_failure_is_actionable_configuration_error(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("autosport.paths.sys.platform", "win32"),
            patch.object(
                paths_module,
                "_windows_known_folder_local_app_data",
                side_effect=OSError("synthetic shell failure"),
            ),
        ):
            with self.assertRaisesRegex(
                ValueError,
                r"Windows LocalAppData known folder could not be resolved",
            ) as caught:
                paths_module.default_workspace()

        self.assertIsInstance(caught.exception.__cause__, OSError)

    def test_non_absolute_known_folder_fails_closed(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("autosport.paths.sys.platform", "win32"),
            patch.object(
                paths_module,
                "_windows_known_folder_local_app_data",
                return_value=Path("relative-local-app-data"),
            ),
        ):
            with self.assertRaisesRegex(
                ValueError,
                r"Windows LocalAppData known folder must resolve to an absolute path",
            ):
                paths_module.default_workspace()

    def test_rpc_e_changed_mode_reuses_existing_com_apartment_without_uninitialize(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            known_folder = Path(directory).resolve() / "Known Local"

            co_initialize_ex = Mock(return_value=0x80010106)
            co_uninitialize = Mock()
            co_task_mem_free = Mock()

            def get_known_folder_path(_folder_id, _flags, _token, output_pointer):
                ctypes.cast(
                    output_pointer,
                    ctypes.POINTER(ctypes.c_void_p),
                ).contents.value = 0x1234
                return 0

            sh_get_known_folder_path = Mock(side_effect=get_known_folder_path)
            shell32 = type(
                "_Shell32",
                (),
                {"SHGetKnownFolderPath": sh_get_known_folder_path},
            )()
            ole32 = type(
                "_Ole32",
                (),
                {
                    "CoInitializeEx": co_initialize_ex,
                    "CoUninitialize": co_uninitialize,
                    "CoTaskMemFree": co_task_mem_free,
                },
            )()

            def load_library(name, **_kwargs):
                if name == "shell32":
                    return shell32
                if name == "ole32":
                    return ole32
                raise AssertionError(f"unexpected library {name!r}")

            with (
                patch("autosport.paths.sys.platform", "win32"),
                patch("ctypes.WinDLL", side_effect=load_library, create=True) as win_dll,
                patch(
                    "ctypes.OleDLL",
                    side_effect=AssertionError("OleDLL must not own raw HRESULT handling"),
                    create=True,
                ),
                patch("ctypes.wstring_at", return_value=str(known_folder)),
            ):
                observed = paths_module._windows_known_folder_local_app_data()

            self.assertEqual(observed, known_folder)
            self.assertEqual(
                [call.args[0] for call in win_dll.call_args_list],
                ["shell32", "ole32"],
            )
            co_initialize_ex.assert_called_once_with(None, 0x2)
            sh_get_known_folder_path.assert_called_once()
            co_task_mem_free.assert_called_once()
            co_uninitialize.assert_not_called()

    @unittest.skipUnless(
        sys.platform == "win32",
        "requires the real Windows Known Folder API",
    )
    def test_real_windows_known_folder_is_absolute(self) -> None:
        observed = paths_module._windows_known_folder_local_app_data()
        self.assertTrue(observed.is_absolute())
        self.assertNotIn("\x00", str(observed))


if __name__ == "__main__":
    unittest.main()

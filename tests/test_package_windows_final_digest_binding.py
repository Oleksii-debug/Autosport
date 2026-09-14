from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import package_windows


class PackageWindowsFinalDigestBindingTests(unittest.TestCase):
    SOURCE_SHA = "a" * 40

    @staticmethod
    def _argv(root: Path) -> list[str]:
        return [
            "package_windows.py",
            "--exe",
            str(root / "Autosport.exe"),
            "--data-exe",
            str(root / "Autosport-Data.exe"),
            "--start-file",
            str(root / "WINDOWS_START_HERE.txt"),
            "--example-dir",
            str(root / "example"),
            "--diagnostic",
            str(root / "diagnostic.json"),
            "--accessibility-audit",
            str(root / "accessibility.json"),
            "--keyboard-audit",
            str(root / "keyboard.json"),
            "--restart-recovery-audit",
            str(root / "restart.json"),
            "--output",
            str(root / "candidate.zip"),
            "--source-sha",
            PackageWindowsFinalDigestBindingTests.SOURCE_SHA,
            "--verification-output",
            str(root / "verification.json"),
        ]

    def test_digest_helper_rejects_stale_binding(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "bound package digest does not match the exact verified package snapshot",
        ):
            package_windows._require_verified_package_digest(
                {"package_sha256": "1" * 64},
                {"package_sha256": "2" * 64},
            )

    def test_main_rejects_package_replacement_between_bind_and_verification(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "candidate.zip"
            base_digest = "0" * 64
            with (
                patch.object(sys, "argv", self._argv(root)),
                patch.object(package_windows, "_bind_source_sha_to_checkout"),
                patch.object(
                    package_windows,
                    "build_windows_package",
                    return_value=(output, base_digest),
                ),
                patch.object(
                    package_windows,
                    "bind_portable_data_tool",
                    return_value={
                        "autosport_data_exe_sha256": "3" * 64,
                        "package_sha256": "1" * 64,
                    },
                ) as bind_mock,
                patch.object(
                    package_windows,
                    "verify_portable_data_tool",
                    return_value={
                        "status": "PASS",
                        "source_sha": self.SOURCE_SHA,
                        "package_sha256": "2" * 64,
                        "autosport_data_exe_sha256": "3" * 64,
                        "portable_historical_data_tools": True,
                    },
                ),
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "bound package digest does not match the exact verified package snapshot",
                ):
                    package_windows.main()

            bind_mock.assert_called_once_with(
                output,
                root / "Autosport-Data.exe",
                expected_base_package_sha256=base_digest,
            )
            self.assertFalse((root / "verification.json").exists())

    def test_main_emits_digest_from_composite_verified_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "candidate.zip"
            base_digest = "0" * 64
            digest = "4" * 64
            verification = {
                "status": "PASS",
                "source_sha": self.SOURCE_SHA,
                "package_sha256": digest,
                "autosport_exe_sha256": "5" * 64,
                "autosport_data_exe_sha256": "6" * 64,
                "portable_historical_data_tools": True,
                "real_money_execution": False,
                "human_tested": False,
                "nvda_verified": False,
            }
            with (
                patch.object(sys, "argv", self._argv(root)),
                patch.object(package_windows, "_bind_source_sha_to_checkout"),
                patch.object(
                    package_windows,
                    "build_windows_package",
                    return_value=(output, base_digest),
                ),
                patch.object(
                    package_windows,
                    "bind_portable_data_tool",
                    return_value={
                        "autosport_data_exe_sha256": "6" * 64,
                        "package_sha256": digest,
                    },
                ) as bind_mock,
                patch.object(
                    package_windows,
                    "verify_portable_data_tool",
                    return_value=verification,
                ),
                patch("builtins.print") as print_mock,
            ):
                self.assertEqual(package_windows.main(), 0)

            bind_mock.assert_called_once_with(
                output,
                root / "Autosport-Data.exe",
                expected_base_package_sha256=base_digest,
            )
            report = json.loads((root / "verification.json").read_text(encoding="utf-8"))
            self.assertEqual(report, verification)
            print_mock.assert_any_call(f"SHA256={digest}")


if __name__ == "__main__":
    unittest.main()

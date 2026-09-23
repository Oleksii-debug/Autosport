from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from autosport.data_tool_package import bind_portable_data_tool
from autosport.release_package import build_windows_package, verify_windows_package


class ReleasePackageNestedChecksumPayloadTests(unittest.TestCase):
    SOURCE_SHA = "a" * 40
    PREFIX = "Autosport-V1/"

    def test_nested_sha256sums_payload_is_hashed_through_final_package_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            exe = root / "Autosport.exe"
            data_exe = root / "Autosport-Data.exe"
            start = root / "WINDOWS_START_HERE.txt"
            diagnostic = root / "diagnostic.json"
            accessibility = root / "accessibility.json"
            keyboard = root / "keyboard.json"
            restart = root / "restart.json"
            example = root / "example"
            nested = example / "reference" / "SHA256SUMS.txt"
            package = root / "candidate.zip"

            exe.write_bytes(b"autosport-exe")
            data_exe.write_bytes(b"autosport-data-exe")
            start.write_text("start\n", encoding="utf-8")
            nested.parent.mkdir(parents=True)
            nested.write_text("reference payload, not the package root checksum list\n", encoding="utf-8")

            common = {
                "status": "PASS",
                "real_money_execution": False,
                "human_tested": False,
                "nvda_verified": False,
            }
            for path in (diagnostic, accessibility, keyboard):
                path.write_text(json.dumps(common) + "\n", encoding="utf-8")
            restart.write_text(
                json.dumps(
                    {
                        **common,
                        "session_restart_status": "PASS",
                        "transaction_recovery_status": "PASS",
                        "recovery_disposition": "aborted_uncommitted",
                        "process_kill_relaunch_status": "PASS",
                        "process_kill_stage_pid": 101,
                        "process_kill_return_code": -15,
                        "process_recovery_pid": 202,
                        "process_recovery_run_id": "nested-sums-regression",
                        "process_recovery_disposition": "committed",
                        "process_recovery_registry_status": "completed",
                        "process_recovery_manifest_phase": "completed",
                        "process_recovery_base_paper_book_sha256": "1" * 64,
                        "process_recovery_base_decision_ledger_sha256": "2" * 64,
                        "process_recovery_new_paper_book_sha256": "3" * 64,
                        "process_recovery_new_decision_ledger_sha256": "4" * 64,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            build_windows_package(
                exe,
                start,
                example,
                diagnostic,
                accessibility,
                keyboard,
                restart,
                package,
                self.SOURCE_SHA,
            )
            base_report = verify_windows_package(
                package,
                expected_source_sha=self.SOURCE_SHA,
            )
            self.assertEqual(base_report["status"], "PASS")

            bind_portable_data_tool(package, data_exe)
            final_report = verify_windows_package(
                package,
                expected_source_sha=self.SOURCE_SHA,
            )
            self.assertEqual(final_report["status"], "PASS")

            nested_relative = "examples/example/reference/SHA256SUMS.txt"
            with zipfile.ZipFile(package, "r") as archive:
                nested_payload = archive.read(self.PREFIX + nested_relative)
                root_sums = archive.read(self.PREFIX + "SHA256SUMS.txt").decode("utf-8")
            expected_line = f"{hashlib.sha256(nested_payload).hexdigest()}  {nested_relative}"
            self.assertIn(expected_line, root_sums.splitlines())


if __name__ == "__main__":
    unittest.main()

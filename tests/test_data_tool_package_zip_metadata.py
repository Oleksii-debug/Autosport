from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from autosport.data_tool_package import bind_portable_data_tool
from autosport.release_package import build_windows_package, verify_windows_package


class PortableDataToolZipMetadataTests(unittest.TestCase):
    SOURCE_SHA = "a" * 40

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def _build_base(self, root: Path) -> tuple[Path, Path]:
        exe = root / "Autosport.exe"
        data_exe = root / "Autosport-Data.exe"
        start = root / "WINDOWS_START_HERE.txt"
        diagnostic = root / "diagnostic.json"
        accessibility = root / "accessibility.json"
        keyboard = root / "keyboard.json"
        restart = root / "restart.json"
        example = root / "example"
        package = root / "candidate.zip"

        exe.write_bytes(b"autosport-gui")
        data_exe.write_bytes(b"autosport-data")
        start.write_text("start\n", encoding="utf-8")
        example.mkdir()
        (example / "market.jsonl").write_text("{}\n", encoding="utf-8")
        common = {
            "status": "PASS",
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
        for path in (diagnostic, accessibility, keyboard):
            self._write_json(path, common)
        self._write_json(
            restart,
            {
                **common,
                "session_restart_status": "PASS",
                "transaction_recovery_status": "PASS",
                "recovery_disposition": "aborted_uncommitted",
                "process_kill_relaunch_status": "PASS",
                "process_kill_stage_pid": 101,
                "process_kill_return_code": -15,
                "process_recovery_pid": 202,
                "process_recovery_run_id": "process-recovery-audit-run",
                "process_recovery_disposition": "committed",
                "process_recovery_registry_status": "completed",
                "process_recovery_manifest_phase": "completed",
                "process_recovery_base_paper_book_sha256": "1" * 64,
                "process_recovery_base_decision_ledger_sha256": "2" * 64,
                "process_recovery_new_paper_book_sha256": "3" * 64,
                "process_recovery_new_decision_ledger_sha256": "4" * 64,
            },
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
        return package, data_exe

    def test_windows_rebind_preserves_release_zip_metadata_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package, data_exe = self._build_base(Path(temporary))

            # Reproduce ZipInfo's Windows defaults during the post-build rebind.
            # The release writer must explicitly restore the canonical metadata.
            with patch("zipfile.sys.platform", "win32"):
                bind_portable_data_tool(package, data_exe)

            report = verify_windows_package(
                package,
                expected_source_sha=self.SOURCE_SHA,
            )
            self.assertEqual(report["status"], "PASS")

            with zipfile.ZipFile(package, "r") as archive:
                infos = archive.infolist()
            self.assertTrue(infos)
            for info in infos:
                self.assertEqual(info.create_system, 3)
                self.assertEqual(info.create_version, 20)
                self.assertEqual(info.extract_version, 20)
                self.assertEqual(info.reserved, 0)
                self.assertEqual(info.volume, 0)
                self.assertEqual(info.internal_attr, 0)


if __name__ == "__main__":
    unittest.main()

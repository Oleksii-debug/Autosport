from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from autosport.release_package import build_windows_package, verify_windows_package


class ReleasePackageLocalHeaderCanonicalityTests(unittest.TestCase):
    SOURCE_SHA = "a" * 40

    def _build_candidate(self, root: Path) -> Path:
        exe = root / "Autosport.exe"
        start = root / "WINDOWS_START_HERE.txt"
        diagnostic = root / "diagnostic.json"
        accessibility = root / "accessibility.json"
        keyboard = root / "keyboard.json"
        restart = root / "restart.json"
        example = root / "example"
        package = root / "candidate.zip"

        exe.write_bytes(b"autosport-exe")
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
                    "process_recovery_run_id": "process-recovery-audit-run",
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
        return package

    @staticmethod
    def _read_members(package: Path) -> dict[str, bytes]:
        with zipfile.ZipFile(package, "r") as archive:
            return {
                info.filename: archive.read(info.filename)
                for info in archive.infolist()
            }

    @staticmethod
    def _inject_last_member_local_extra(package: Path) -> str:
        payload = bytearray(package.read_bytes())
        with zipfile.ZipFile(package, "r") as archive:
            target = max(archive.infolist(), key=lambda info: info.header_offset)

        offset = target.header_offset
        if payload[offset : offset + 4] != b"PK\x03\x04":
            raise AssertionError("expected local file header signature")
        filename_length = int.from_bytes(payload[offset + 26 : offset + 28], "little")
        extra_length = int.from_bytes(payload[offset + 28 : offset + 30], "little")
        injected_extra = b"\x01\x00\x00\x00"
        insertion_offset = offset + 30 + filename_length + extra_length
        payload[offset + 28 : offset + 30] = (
            extra_length + len(injected_extra)
        ).to_bytes(2, "little")
        payload[insertion_offset:insertion_offset] = injected_extra

        eocd_offset = payload.rfind(b"PK\x05\x06")
        if eocd_offset < 0:
            raise AssertionError("expected EOCD record")
        central_offset_field = eocd_offset + 16
        central_offset = int.from_bytes(
            payload[central_offset_field : central_offset_field + 4],
            "little",
        )
        payload[central_offset_field : central_offset_field + 4] = (
            central_offset + len(injected_extra)
        ).to_bytes(4, "little")
        package.write_bytes(payload)
        return target.filename

    def test_local_only_extra_metadata_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(Path(temporary))
            before = self._read_members(package)
            target_name = self._inject_last_member_local_extra(package)

            with zipfile.ZipFile(package, "r") as archive:
                target = archive.getinfo(target_name)
                self.assertEqual(target.extra, b"")
            self.assertEqual(self._read_members(package), before)

            with self.assertRaisesRegex(ValueError, "local header metadata"):
                verify_windows_package(package, expected_source_sha=self.SOURCE_SHA)

    def test_opaque_prefix_is_rejected_even_when_payloads_are_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(Path(temporary))
            before = self._read_members(package)
            package.write_bytes(b"OPAQUE-PREFIX-" + package.read_bytes())

            self.assertEqual(self._read_members(package), before)
            with self.assertRaisesRegex(ValueError, "unclaimed bytes before local header"):
                verify_windows_package(package, expected_source_sha=self.SOURCE_SHA)

    def test_post_eocd_suffix_is_rejected_even_when_payloads_are_readable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(Path(temporary))
            before = self._read_members(package)
            package.write_bytes(package.read_bytes() + b"OPAQUE-SUFFIX")

            self.assertEqual(self._read_members(package), before)
            with self.assertRaisesRegex(ValueError, "end-of-central-directory"):
                verify_windows_package(package, expected_source_sha=self.SOURCE_SHA)


if __name__ == "__main__":
    unittest.main()

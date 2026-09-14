from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from autosport.release_package import build_windows_package, verify_windows_package


class ReleasePackageZipCanonicalityTests(unittest.TestCase):
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
    def _rewrite_archive(
        package: Path,
        *,
        reverse: bool = False,
        mutate_first=None,
        archive_comment: bytes = b"",
    ) -> None:
        with zipfile.ZipFile(package, "r") as archive:
            entries = [
                (info, archive.read(info.filename))
                for info in archive.infolist()
            ]
        if reverse:
            entries.reverse()
        with zipfile.ZipFile(
            package,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for index, (original, payload) in enumerate(entries):
                info = zipfile.ZipInfo(original.filename, original.date_time)
                info.compress_type = original.compress_type
                info.external_attr = original.external_attr
                info.extra = original.extra
                info.comment = original.comment
                if index == 0 and mutate_first is not None:
                    mutate_first(info)
                archive.writestr(
                    info,
                    payload,
                    compress_type=info.compress_type,
                    compresslevel=9,
                )
            archive.comment = archive_comment

    def test_builder_emits_verifier_accepted_canonical_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(Path(temporary))
            report = verify_windows_package(package, expected_source_sha=self.SOURCE_SHA)
            self.assertEqual(report["status"], "PASS")

    def test_payload_equivalent_reordered_archive_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(Path(temporary))
            self._rewrite_archive(package, reverse=True)
            with self.assertRaisesRegex(ValueError, "member order is not canonical"):
                verify_windows_package(package, expected_source_sha=self.SOURCE_SHA)

    def test_payload_equivalent_noncanonical_timestamp_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(Path(temporary))
            self._rewrite_archive(
                package,
                mutate_first=lambda info: setattr(info, "date_time", (2026, 9, 14, 9, 0, 0)),
            )
            with self.assertRaisesRegex(ValueError, "non-canonical timestamp"):
                verify_windows_package(package, expected_source_sha=self.SOURCE_SHA)

    def test_payload_equivalent_noncanonical_permissions_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(Path(temporary))
            self._rewrite_archive(
                package,
                mutate_first=lambda info: setattr(info, "external_attr", 0o600 << 16),
            )
            with self.assertRaisesRegex(ValueError, "non-canonical permissions"):
                verify_windows_package(package, expected_source_sha=self.SOURCE_SHA)

    def test_payload_equivalent_archive_comment_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(Path(temporary))
            self._rewrite_archive(package, archive_comment=b"repacked")
            with self.assertRaisesRegex(ValueError, "archive comment"):
                verify_windows_package(package, expected_source_sha=self.SOURCE_SHA)

    def test_payload_equivalent_member_extra_metadata_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(Path(temporary))
            self._rewrite_archive(
                package,
                mutate_first=lambda info: setattr(info, "extra", b"\x01\x00\x00\x00"),
            )
            with self.assertRaisesRegex(ValueError, "member metadata"):
                verify_windows_package(package, expected_source_sha=self.SOURCE_SHA)


if __name__ == "__main__":
    unittest.main()

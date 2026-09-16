from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from autosport.release_package import build_windows_package, verify_windows_package


class ReleasePackageZipCanonicalityTests(unittest.TestCase):
    SOURCE_SHA = "a" * 40
    PREFIX = "Autosport-V1/"

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
    def _rewrite_archive(
        package: Path,
        *,
        reverse: bool = False,
        mutate_first=None,
        archive_comment: bytes = b"",
        replacements: dict[str, bytes] | None = None,
    ) -> None:
        replacements = replacements or {}
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
                info.create_system = original.create_system
                info.create_version = original.create_version
                info.extract_version = original.extract_version
                info.reserved = original.reserved
                info.flag_bits = original.flag_bits
                info.volume = original.volume
                info.internal_attr = original.internal_attr
                info.external_attr = original.external_attr
                info.extra = original.extra
                info.comment = original.comment
                if index == 0 and mutate_first is not None:
                    mutate_first(info)
                archive.writestr(
                    info,
                    replacements.get(original.filename, payload),
                    compress_type=info.compress_type,
                    compresslevel=9,
                )
            archive.comment = archive_comment

    @staticmethod
    def _canonical_json_bytes(payload: dict) -> bytes:
        return (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")

    @staticmethod
    def _parse_sums(payload: bytes) -> dict[str, str]:
        return {
            relative: digest
            for line in payload.decode("utf-8").splitlines()
            for digest, separator, relative in [line.partition("  ")]
            if separator
        }

    @staticmethod
    def _canonical_sums_bytes(sums: dict[str, str]) -> bytes:
        return "".join(
            f"{sums[relative]}  {relative}\n"
            for relative in sorted(sums)
        ).encode("utf-8")

    def test_builder_emits_verifier_accepted_canonical_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(Path(temporary))
            report = verify_windows_package(package, expected_source_sha=self.SOURCE_SHA)
            self.assertEqual(report["status"], "PASS")

    def test_builder_pins_supported_zip_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(Path(temporary))
            with zipfile.ZipFile(package, "r") as archive:
                infos = archive.infolist()
            self.assertTrue(infos)
            for info in infos:
                self.assertEqual(info.create_system, 3)
                self.assertEqual(info.create_version, 20)
                self.assertEqual(info.extract_version, 20)
                self.assertEqual(info.reserved, 0)
                self.assertEqual(info.flag_bits, 0)
                self.assertEqual(info.volume, 0)
                self.assertEqual(info.internal_attr, 0)

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
                mutate_first=lambda info: setattr(
                    info,
                    "date_time",
                    (2026, 9, 14, 9, 0, 0),
                ),
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

    def test_payload_equivalent_noncanonical_compression_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(Path(temporary))
            self._rewrite_archive(
                package,
                mutate_first=lambda info: setattr(
                    info,
                    "compress_type",
                    zipfile.ZIP_STORED,
                ),
            )
            with self.assertRaisesRegex(ValueError, "non-canonical compression method"):
                verify_windows_package(package, expected_source_sha=self.SOURCE_SHA)

    def test_payload_equivalent_noncanonical_create_system_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(Path(temporary))
            self._rewrite_archive(
                package,
                mutate_first=lambda info: setattr(info, "create_system", 0),
            )
            with self.assertRaisesRegex(ValueError, "non-canonical create system"):
                verify_windows_package(package, expected_source_sha=self.SOURCE_SHA)

    def test_payload_equivalent_noncanonical_create_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(Path(temporary))
            self._rewrite_archive(
                package,
                mutate_first=lambda info: setattr(info, "create_version", 10),
            )
            with self.assertRaisesRegex(ValueError, "non-canonical create version"):
                verify_windows_package(package, expected_source_sha=self.SOURCE_SHA)

    def test_payload_equivalent_noncanonical_extract_version_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(Path(temporary))
            self._rewrite_archive(
                package,
                mutate_first=lambda info: setattr(info, "extract_version", 10),
            )
            with self.assertRaisesRegex(ValueError, "non-canonical extract version"):
                verify_windows_package(package, expected_source_sha=self.SOURCE_SHA)

    def test_payload_equivalent_noncanonical_internal_attr_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(Path(temporary))
            self._rewrite_archive(
                package,
                mutate_first=lambda info: setattr(info, "internal_attr", 1),
            )
            with self.assertRaisesRegex(ValueError, "non-canonical internal attributes"):
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
                mutate_first=lambda info: setattr(
                    info,
                    "extra",
                    b"\x01\x00\x00\x00",
                ),
            )
            with self.assertRaisesRegex(ValueError, "member metadata"):
                verify_windows_package(package, expected_source_sha=self.SOURCE_SHA)

    def test_semantically_equivalent_manifest_serialization_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(Path(temporary))
            members = self._read_members(package)
            manifest_name = self.PREFIX + "PACKAGE_MANIFEST.json"
            sums_name = self.PREFIX + "SHA256SUMS.txt"
            manifest = json.loads(members[manifest_name].decode("utf-8"))
            compact_manifest = (
                json.dumps(manifest, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            sums = self._parse_sums(members[sums_name])
            sums["PACKAGE_MANIFEST.json"] = hashlib.sha256(
                compact_manifest
            ).hexdigest()
            self._rewrite_archive(
                package,
                replacements={
                    manifest_name: compact_manifest,
                    sums_name: self._canonical_sums_bytes(sums),
                },
            )
            with self.assertRaisesRegex(
                ValueError,
                "PACKAGE_MANIFEST.json is not in canonical JSON representation",
            ):
                verify_windows_package(package, expected_source_sha=self.SOURCE_SHA)

    def test_semantically_equivalent_build_info_serialization_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(Path(temporary))
            members = self._read_members(package)
            build_info_name = self.PREFIX + "BUILD_INFO.json"
            manifest_name = self.PREFIX + "PACKAGE_MANIFEST.json"
            sums_name = self.PREFIX + "SHA256SUMS.txt"

            build_info = json.loads(members[build_info_name].decode("utf-8"))
            compact_build_info = (
                json.dumps(build_info, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            build_info_sha = hashlib.sha256(compact_build_info).hexdigest()

            manifest = json.loads(members[manifest_name].decode("utf-8"))
            manifest["files"]["BUILD_INFO.json"] = build_info_sha
            canonical_manifest = self._canonical_json_bytes(manifest)
            manifest_sha = hashlib.sha256(canonical_manifest).hexdigest()

            sums = self._parse_sums(members[sums_name])
            sums["BUILD_INFO.json"] = build_info_sha
            sums["PACKAGE_MANIFEST.json"] = manifest_sha
            self._rewrite_archive(
                package,
                replacements={
                    build_info_name: compact_build_info,
                    manifest_name: canonical_manifest,
                    sums_name: self._canonical_sums_bytes(sums),
                },
            )
            with self.assertRaisesRegex(
                ValueError,
                "BUILD_INFO.json is not in canonical JSON representation",
            ):
                verify_windows_package(package, expected_source_sha=self.SOURCE_SHA)

    def test_semantically_equivalent_checksum_order_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(Path(temporary))
            members = self._read_members(package)
            sums_name = self.PREFIX + "SHA256SUMS.txt"
            lines = members[sums_name].decode("utf-8").splitlines()
            self.assertGreater(len(lines), 1)
            reordered = ("\n".join(reversed(lines)) + "\n").encode("utf-8")
            self._rewrite_archive(
                package,
                replacements={sums_name: reordered},
            )
            with self.assertRaisesRegex(
                ValueError,
                "SHA256SUMS.txt is not in canonical sorted representation",
            ):
                verify_windows_package(package, expected_source_sha=self.SOURCE_SHA)


if __name__ == "__main__":
    unittest.main()

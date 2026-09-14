from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from autosport.release_package import (
    _decode_json_object,
    build_windows_package,
    verify_windows_package,
)


class ReleasePackageJsonIntegrityTests(unittest.TestCase):
    SOURCE_SHA = "a" * 40

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def _build_valid_package(
        self,
        root: Path,
        *,
        include_process_recovery: bool = True,
    ) -> Path:
        executable = root / "Autosport.exe"
        start = root / "WINDOWS_START_HERE.txt"
        example = root / "example"
        diagnostic = root / "diagnostic.json"
        accessibility = root / "accessibility.json"
        keyboard = root / "keyboard.json"
        restart = root / "restart.json"
        package = root / "candidate.zip"

        executable.write_bytes(b"autosport-executable")
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
        restart_payload = {
            **common,
            "session_restart_status": "PASS",
            "transaction_recovery_status": "PASS",
            "recovery_disposition": "aborted_uncommitted",
        }
        if include_process_recovery:
            restart_payload.update(
                {
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
        self._write_json(restart, restart_payload)

        build_windows_package(
            executable,
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
    def _replace_manifest_and_rehash_sums(package: Path, manifest_payload: bytes) -> None:
        with zipfile.ZipFile(package, "r") as archive:
            members = {item.filename: archive.read(item.filename) for item in archive.infolist()}

        manifest_name = "Autosport-V1/PACKAGE_MANIFEST.json"
        sums_name = "Autosport-V1/SHA256SUMS.txt"
        members[manifest_name] = manifest_payload
        manifest_sha = hashlib.sha256(manifest_payload).hexdigest()
        rewritten_sums = []
        for line in members[sums_name].decode("utf-8").splitlines():
            digest, separator, relative = line.partition("  ")
            if relative == "PACKAGE_MANIFEST.json":
                digest = manifest_sha
            rewritten_sums.append(f"{digest}{separator}{relative}")
        members[sums_name] = ("\n".join(rewritten_sums) + "\n").encode("utf-8")

        with zipfile.ZipFile(
            package,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            for name, payload in members.items():
                archive.writestr(name, payload)

    @classmethod
    def _replace_manifest_with_self_consistent_duplicate_key(cls, package: Path) -> None:
        with zipfile.ZipFile(package, "r") as archive:
            manifest = json.loads(
                archive.read("Autosport-V1/PACKAGE_MANIFEST.json").decode("utf-8")
            )
        files_json = json.dumps(
            manifest["files"],
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        ambiguous_manifest = (
            "{\n"
            f'  "files": {files_json},\n'
            '  "schema_version": 1,\n'
            '  "schema_version": 1\n'
            "}\n"
        ).encode("utf-8")
        cls._replace_manifest_and_rehash_sums(package, ambiguous_manifest)

    @classmethod
    def _replace_manifest_with_self_consistent_nonstandard_constant(cls, package: Path) -> None:
        with zipfile.ZipFile(package, "r") as archive:
            manifest = json.loads(
                archive.read("Autosport-V1/PACKAGE_MANIFEST.json").decode("utf-8")
            )
        files_json = json.dumps(
            manifest["files"],
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        invalid_manifest = (
            "{\n"
            f'  "files": {files_json},\n'
            '  "metadata": {"nested_score": NaN},\n'
            '  "schema_version": 1\n'
            "}\n"
        ).encode("utf-8")
        cls._replace_manifest_and_rehash_sums(package, invalid_manifest)

    def test_json_decoder_rejects_duplicate_keys_recursively(self) -> None:
        for payload, duplicate in (
            (b'{"status":"PASS","status":"PASS"}', "status"),
            (b'{"outer":{"source_sha":"a","source_sha":"a"}}', "source_sha"),
        ):
            with self.subTest(duplicate=duplicate):
                with self.assertRaisesRegex(
                    ValueError,
                    rf"duplicate JSON object key: {duplicate}",
                ):
                    _decode_json_object(payload, "evidence.json")

    def test_json_decoder_rejects_nonstandard_constants_recursively(self) -> None:
        for constant in ("NaN", "Infinity", "-Infinity"):
            payload = ('{"outer":{"score":' + constant + '}}').encode("utf-8")
            with self.subTest(constant=constant):
                with self.assertRaisesRegex(
                    ValueError,
                    rf"non-standard JSON constant: {constant}",
                ):
                    _decode_json_object(payload, "evidence.json")

    def test_verifier_rejects_legacy_restart_evidence_without_process_boundary_proof(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_valid_package(
                Path(temporary),
                include_process_recovery=False,
            )
            with self.assertRaisesRegex(
                ValueError,
                "does not prove real process kill/relaunch PASS",
            ):
                verify_windows_package(package, expected_source_sha=self.SOURCE_SHA)

    def test_verifier_rejects_hash_consistent_manifest_with_duplicate_key(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_valid_package(Path(temporary))
            self.assertEqual(
                verify_windows_package(package, expected_source_sha=self.SOURCE_SHA)["status"],
                "PASS",
            )

            self._replace_manifest_with_self_consistent_duplicate_key(package)

            with self.assertRaisesRegex(
                ValueError,
                "PACKAGE_MANIFEST.json contains duplicate JSON object key: schema_version",
            ):
                verify_windows_package(package, expected_source_sha=self.SOURCE_SHA)

    def test_verifier_rejects_hash_consistent_manifest_with_nan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_valid_package(Path(temporary))
            self._replace_manifest_with_self_consistent_nonstandard_constant(package)

            with self.assertRaisesRegex(
                ValueError,
                "PACKAGE_MANIFEST.json contains non-standard JSON constant: NaN",
            ):
                verify_windows_package(package, expected_source_sha=self.SOURCE_SHA)


if __name__ == "__main__":
    unittest.main()

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

    def _build_valid_package(self, root: Path) -> Path:
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
        self._write_json(
            restart,
            {
                **common,
                "session_restart_status": "PASS",
                "transaction_recovery_status": "PASS",
                "recovery_disposition": "aborted_uncommitted",
            },
        )

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
    def _replace_manifest_with_self_consistent_duplicate_key(package: Path) -> None:
        with zipfile.ZipFile(package, "r") as archive:
            members = {item.filename: archive.read(item.filename) for item in archive.infolist()}

        manifest_name = "Autosport-V1/PACKAGE_MANIFEST.json"
        sums_name = "Autosport-V1/SHA256SUMS.txt"
        manifest = json.loads(members[manifest_name].decode("utf-8"))
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
        members[manifest_name] = ambiguous_manifest

        manifest_sha = hashlib.sha256(ambiguous_manifest).hexdigest()
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


if __name__ == "__main__":
    unittest.main()

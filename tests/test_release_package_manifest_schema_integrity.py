from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from autosport.release_package import build_windows_package, verify_windows_package


class ReleasePackageManifestSchemaIntegrityTests(unittest.TestCase):
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
        process_recovery = root / "process-recovery.json"
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
        self._write_json(
            process_recovery,
            {
                **common,
                "audit_id": "real-process-kill-relaunch-recovery-v1",
                "forced_process_kill_observed": True,
                "crash_worker_returncode": -9,
                "recovery_worker_returncode": 0,
                "recovery_disposition": "aborted_uncommitted",
                "run_status": "aborted",
                "manifest_phase": "aborted",
                "economic_base_preserved": True,
                "v1_ready": False,
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
            process_recovery,
            package,
            self.SOURCE_SHA,
        )
        self.assertEqual(
            verify_windows_package(package, expected_source_sha=self.SOURCE_SHA)["status"],
            "PASS",
        )
        return package

    @staticmethod
    def _rewrite_manifest_schema_and_rehash_sums(package: Path, schema_version: object) -> None:
        with zipfile.ZipFile(package, "r") as archive:
            members = {
                item.filename: archive.read(item.filename)
                for item in archive.infolist()
            }

        manifest_name = "Autosport-V1/PACKAGE_MANIFEST.json"
        sums_name = "Autosport-V1/SHA256SUMS.txt"
        manifest = json.loads(members[manifest_name].decode("utf-8"))
        manifest["schema_version"] = schema_version
        manifest_payload = (
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
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

    def test_verifier_rejects_non_integer_manifest_schema_types(self) -> None:
        for schema_version in (True, 1.0, "1"):
            with self.subTest(schema_version=repr(schema_version)):
                with tempfile.TemporaryDirectory() as temporary:
                    package = self._build_valid_package(Path(temporary))
                    self._rewrite_manifest_schema_and_rehash_sums(
                        package,
                        schema_version,
                    )

                    with self.assertRaisesRegex(
                        ValueError,
                        "PACKAGE_MANIFEST schema is invalid",
                    ):
                        verify_windows_package(
                            package,
                            expected_source_sha=self.SOURCE_SHA,
                        )


if __name__ == "__main__":
    unittest.main()

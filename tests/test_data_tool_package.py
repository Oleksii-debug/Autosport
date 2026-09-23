import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from autosport.data_tool_package import bind_portable_data_tool, verify_portable_data_tool
from autosport.release_package import build_windows_package, verify_windows_package


class PortableDataToolPackageTests(unittest.TestCase):
    def _build_base(self, root: Path, source_sha: str) -> tuple[Path, Path]:
        exe = root / "Autosport.exe"
        data_exe = root / "Autosport-Data.exe"
        start = root / "WINDOWS_START_HERE.txt"
        diagnostic = root / "diag.json"
        accessibility = root / "a11y.json"
        keyboard = root / "keyboard.json"
        restart = root / "restart.json"
        example = root / "example"
        package = root / "candidate.zip"
        example.mkdir()
        exe.write_bytes(b"gui-exe")
        data_exe.write_bytes(b"data-exe")
        start.write_text("start\n", encoding="utf-8")
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
            source_sha,
        )
        return package, data_exe

    @staticmethod
    def _read_member(package: Path, relative: str) -> bytes:
        with zipfile.ZipFile(package, "r") as archive:
            return archive.read(f"Autosport-V1/{relative}")

    @staticmethod
    def _rewrite_members(package: Path, replacements: dict[str, bytes]) -> None:
        targets = {f"Autosport-V1/{relative}": payload for relative, payload in replacements.items()}
        with zipfile.ZipFile(package, "r") as archive:
            members = [(item.filename, archive.read(item.filename)) for item in archive.infolist()]
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, current in members:
                archive.writestr(name, targets.get(name, current))

    @classmethod
    def _rewrite_member(cls, package: Path, relative: str, payload: bytes) -> None:
        cls._rewrite_members(package, {relative: payload})

    @classmethod
    def _rewrite_manifest_and_rehash_sums(cls, package: Path, manifest_payload: bytes) -> None:
        sums = cls._read_member(package, "SHA256SUMS.txt").decode("utf-8")
        manifest_sha = hashlib.sha256(manifest_payload).hexdigest()
        rewritten_sums = []
        for line in sums.splitlines():
            digest, separator, relative = line.partition("  ")
            if relative == "PACKAGE_MANIFEST.json":
                digest = manifest_sha
            rewritten_sums.append(f"{digest}{separator}{relative}")
        cls._rewrite_members(
            package,
            {
                "PACKAGE_MANIFEST.json": manifest_payload,
                "SHA256SUMS.txt": ("\n".join(rewritten_sums) + "\n").encode("utf-8"),
            },
        )

    def test_data_tool_is_hash_bound_and_base_verifier_stays_green(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source_sha = "a" * 40
            package, data_exe = self._build_base(root, source_sha)
            binding = bind_portable_data_tool(package, data_exe)
            base_report = verify_windows_package(package, expected_source_sha=source_sha)
            data_report = verify_portable_data_tool(package)
            self.assertEqual(base_report["status"], "PASS")
            self.assertEqual(data_report["status"], "PASS")
            self.assertEqual(binding["autosport_data_exe_sha256"], data_report["autosport_data_exe_sha256"])

    def test_portable_verifier_rejects_tampered_non_data_member(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package, data_exe = self._build_base(root, "9" * 40)
            bind_portable_data_tool(package, data_exe)
            self._rewrite_member(
                package,
                "WINDOWS_START_HERE.txt",
                b"tampered instructions\n",
            )

            with self.assertRaisesRegex(
                ValueError,
                "PACKAGE_MANIFEST hash mismatch: WINDOWS_START_HERE.txt",
            ):
                verify_portable_data_tool(package)

    def test_binding_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            hashes = []
            for index in range(2):
                local = root / str(index)
                local.mkdir()
                package, data_exe = self._build_base(local, "b" * 40)
                hashes.append(bind_portable_data_tool(package, data_exe)["package_sha256"])
            self.assertEqual(hashes[0], hashes[1])

    def test_verifier_rejects_package_without_data_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            package, _data_exe = self._build_base(Path(tmp), "c" * 40)
            with self.assertRaisesRegex(ValueError, "missing Autosport-Data.exe"):
                verify_portable_data_tool(package)

    def test_binding_rejects_ambiguous_build_info_before_mutating_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            package, data_exe = self._build_base(Path(tmp), "d" * 40)
            build_info = self._read_member(package, "BUILD_INFO.json").decode("utf-8")
            ambiguous = build_info.replace(
                '  "human_tested": false,',
                '  "human_tested": true,\n  "human_tested": false,',
            )
            self.assertNotEqual(build_info, ambiguous)
            self._rewrite_member(package, "BUILD_INFO.json", ambiguous.encode("utf-8"))
            before = package.read_bytes()

            with self.assertRaisesRegex(
                ValueError,
                "BUILD_INFO.json contains duplicate JSON object key: human_tested",
            ):
                bind_portable_data_tool(package, data_exe)

            self.assertEqual(package.read_bytes(), before)

    def test_binding_rejects_ambiguous_manifest_before_mutating_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            package, data_exe = self._build_base(Path(tmp), "f" * 40)
            manifest = self._read_member(package, "PACKAGE_MANIFEST.json").decode("utf-8")
            ambiguous = manifest.replace(
                '  "schema_version": 1',
                '  "schema_version": 1,\n  "schema_version": 1',
            )
            self.assertNotEqual(manifest, ambiguous)
            self._rewrite_manifest_and_rehash_sums(package, ambiguous.encode("utf-8"))
            before = package.read_bytes()

            with self.assertRaisesRegex(
                ValueError,
                "PACKAGE_MANIFEST.json contains duplicate JSON object key: schema_version",
            ):
                bind_portable_data_tool(package, data_exe)

            self.assertEqual(package.read_bytes(), before)

    def test_binding_rejects_nonstandard_manifest_before_mutating_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            package, data_exe = self._build_base(Path(tmp), "1" * 40)
            manifest = self._read_member(package, "PACKAGE_MANIFEST.json").decode("utf-8")
            invalid = manifest.replace(
                '  "schema_version": 1',
                '  "metadata": {"nested_score": Infinity},\n  "schema_version": 1',
            )
            self.assertNotEqual(manifest, invalid)
            self._rewrite_manifest_and_rehash_sums(package, invalid.encode("utf-8"))
            before = package.read_bytes()

            with self.assertRaisesRegex(
                ValueError,
                "PACKAGE_MANIFEST.json contains non-standard JSON constant: Infinity",
            ):
                bind_portable_data_tool(package, data_exe)

            self.assertEqual(package.read_bytes(), before)

    def test_binding_rejects_stale_sums_before_mutating_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            package, data_exe = self._build_base(Path(tmp), "2" * 40)
            sums = self._read_member(package, "SHA256SUMS.txt").decode("utf-8")
            rewritten_sums = []
            for line in sums.splitlines():
                digest, separator, relative = line.partition("  ")
                if relative == "BUILD_INFO.json":
                    digest = "0" * 64
                rewritten_sums.append(f"{digest}{separator}{relative}")
            self._rewrite_member(
                package,
                "SHA256SUMS.txt",
                ("\n".join(rewritten_sums) + "\n").encode("utf-8"),
            )
            before = package.read_bytes()

            with self.assertRaisesRegex(
                ValueError,
                "SHA256SUMS hash mismatch: BUILD_INFO.json",
            ):
                bind_portable_data_tool(package, data_exe)

            self.assertEqual(package.read_bytes(), before)

    def test_portable_verifier_rejects_ambiguous_build_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            package, data_exe = self._build_base(Path(tmp), "e" * 40)
            bind_portable_data_tool(package, data_exe)
            build_info = self._read_member(package, "BUILD_INFO.json").decode("utf-8")
            ambiguous = build_info.replace(
                '  "portable_historical_data_tools": true,',
                '  "portable_historical_data_tools": false,\n'
                '  "portable_historical_data_tools": true,',
            )
            self.assertNotEqual(build_info, ambiguous)
            self._rewrite_member(package, "BUILD_INFO.json", ambiguous.encode("utf-8"))

            with self.assertRaisesRegex(
                ValueError,
                "BUILD_INFO.json contains duplicate JSON object key: portable_historical_data_tools",
            ):
                verify_portable_data_tool(package)


if __name__ == "__main__":
    unittest.main()

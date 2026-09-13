import json
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import autosport.data_tool_package as data_tool_package
from autosport.release_package import build_windows_package, verify_windows_package


class PortableDataToolSnapshotBindingTests(unittest.TestCase):
    SOURCE_SHA = "a" * 40

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def _build_base(self, root: Path, name: str) -> tuple[Path, Path]:
        root.mkdir(parents=True, exist_ok=True)
        executable = root / f"{name}-Autosport.exe"
        data_exe = root / f"{name}-Autosport-Data.exe"
        start = root / f"{name}-WINDOWS_START_HERE.txt"
        example = root / f"{name}-example"
        diagnostic = root / f"{name}-diagnostic.json"
        accessibility = root / f"{name}-accessibility.json"
        keyboard = root / f"{name}-keyboard.json"
        restart = root / f"{name}-restart.json"
        process_recovery = root / f"{name}-process-recovery.json"
        package = root / f"{name}.zip"

        executable.write_bytes(f"{name}-autosport".encode("ascii"))
        data_exe.write_bytes(f"{name}-data".encode("ascii"))
        start.write_text(f"{name}-start\n", encoding="utf-8")
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
        return package, data_exe

    @staticmethod
    def _rewrite_start_without_rehashing(package: Path) -> None:
        target = "Autosport-V1/WINDOWS_START_HERE.txt"
        with zipfile.ZipFile(package, "r") as archive:
            members = [(item, archive.read(item.filename)) for item in archive.infolist()]
        with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for info, payload in members:
                archive.writestr(
                    info,
                    b"tampered-start\n" if info.filename == target else payload,
                )

    def test_binding_verifies_the_same_base_bytes_it_repackages(self) -> None:
        """A valid path swap cannot launder previously captured unverified members."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package, data_exe = self._build_base(root / "candidate", "candidate")
            valid_swap, _ = self._build_base(root / "swap", "swap")
            valid_swap_bytes = valid_swap.read_bytes()

            # A is now self-inconsistent: payload bytes changed without updating its
            # manifest/sums.  B remains a fully valid package with the same source SHA.
            self._rewrite_start_without_rehashing(package)
            with self.assertRaisesRegex(ValueError, "hash mismatch: WINDOWS_START_HERE.txt"):
                verify_windows_package(package, expected_source_sha=self.SOURCE_SHA)
            self.assertEqual(
                verify_windows_package(valid_swap, expected_source_sha=self.SOURCE_SHA)["status"],
                "PASS",
            )

            real_verify = verify_windows_package

            def swap_live_path_then_verify(path: str | Path, *, expected_source_sha: str):
                # Simulate a semantically valid replacement of the caller-controlled
                # live path exactly when the binder reaches its verification step.
                package.write_bytes(valid_swap_bytes)
                return real_verify(path, expected_source_sha=expected_source_sha)

            with patch.object(
                data_tool_package,
                "verify_windows_package",
                side_effect=swap_live_path_then_verify,
            ):
                with self.assertRaisesRegex(
                    ValueError,
                    "hash mismatch: WINDOWS_START_HERE.txt",
                ):
                    data_tool_package.bind_portable_data_tool(package, data_exe)

            # The adversary's valid B replacement remains untouched by the rejected
            # bind. In particular, the binder did not repackage invalid captured A
            # and bless it with fresh integrity metadata plus Autosport-Data.exe.
            self.assertEqual(package.read_bytes(), valid_swap_bytes)
            with zipfile.ZipFile(package, "r") as archive:
                self.assertNotIn("Autosport-V1/Autosport-Data.exe", archive.namelist())


if __name__ == "__main__":
    unittest.main()

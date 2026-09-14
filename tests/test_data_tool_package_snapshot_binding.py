from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import autosport.data_tool_package as data_tool_package
import autosport.release_package as release_package
from autosport.release_package import build_windows_package, verify_windows_package
from scripts.package_windows import _require_verified_package_digest


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
        """A valid live-path swap cannot launder previously captured unverified members."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package, data_exe = self._build_base(root / "candidate", "candidate")
            valid_swap, _ = self._build_base(root / "swap", "swap")
            valid_swap_bytes = valid_swap.read_bytes()

            self._rewrite_start_without_rehashing(package)
            with self.assertRaisesRegex(ValueError, "hash mismatch: WINDOWS_START_HERE.txt"):
                verify_windows_package(package, expected_source_sha=self.SOURCE_SHA)
            self.assertEqual(
                verify_windows_package(valid_swap, expected_source_sha=self.SOURCE_SHA)["status"],
                "PASS",
            )

            real_verify = verify_windows_package

            def swap_live_path_then_verify(path: str | Path, *, expected_source_sha: str):
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

            self.assertEqual(package.read_bytes(), valid_swap_bytes)
            with zipfile.ZipFile(package, "r") as archive:
                self.assertNotIn("Autosport-V1/Autosport-Data.exe", archive.namelist())

    def test_member_extraction_uses_immutable_capture_not_verifier_path(self) -> None:
        """Members are parsed from captured bytes, removing the prior pathname split."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package, data_exe = self._build_base(root / "candidate", "candidate")
            seen_sources: list[object] = []
            real_read_members = data_tool_package._read_members

            def record_source(source):
                seen_sources.append(source)
                self.assertIsInstance(source, io.BytesIO)
                return real_read_members(source)

            with patch.object(data_tool_package, "_read_members", side_effect=record_source):
                binding = data_tool_package.bind_portable_data_tool(package, data_exe)

            self.assertEqual(len(seen_sources), 1)
            verification = data_tool_package.verify_portable_data_tool(package)
            self.assertEqual(binding["package_sha256"], verification["package_sha256"])

    def test_writer_digest_remains_bound_when_destination_is_replaced_after_publish(self) -> None:
        """Writer identity is SHA(A), so a post-publish valid B cannot become this invocation's PASS."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            package, data_exe = self._build_base(root / "candidate", "candidate")
            valid_swap, swap_data_exe = self._build_base(root / "swap", "swap")
            data_tool_package.bind_portable_data_tool(valid_swap, swap_data_exe)
            valid_swap_bytes = valid_swap.read_bytes()
            valid_swap_verification = data_tool_package.verify_portable_data_tool(valid_swap)

            real_replace = os.replace

            def replace_then_substitute(source, destination):
                real_replace(source, destination)
                Path(destination).write_bytes(valid_swap_bytes)

            with patch.object(data_tool_package.os, "replace", side_effect=replace_then_substitute):
                binding = data_tool_package.bind_portable_data_tool(package, data_exe)

            self.assertEqual(package.read_bytes(), valid_swap_bytes)
            verification = data_tool_package.verify_portable_data_tool(package)
            self.assertEqual(
                verification["package_sha256"],
                valid_swap_verification["package_sha256"],
            )
            self.assertNotEqual(binding["package_sha256"], verification["package_sha256"])
            with self.assertRaisesRegex(
                ValueError,
                "bound package digest does not match the exact verified package snapshot",
            ):
                _require_verified_package_digest(binding, verification)

    def test_base_writer_digest_rejects_replacement_before_portable_bind(self) -> None:
        """A valid base B cannot replace authored A and become this invocation's bind input."""

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate_root = root / "candidate"
            package, data_exe = self._build_base(candidate_root, "candidate")
            valid_swap, _ = self._build_base(root / "swap", "swap")
            valid_swap_bytes = valid_swap.read_bytes()
            valid_swap_sha = verify_windows_package(
                valid_swap,
                expected_source_sha=self.SOURCE_SHA,
            )["package_sha256"]

            real_replace = os.replace

            def replace_then_substitute(source, destination):
                real_replace(source, destination)
                if Path(destination) == package:
                    Path(destination).write_bytes(valid_swap_bytes)

            with patch.object(
                release_package.os,
                "replace",
                side_effect=replace_then_substitute,
            ):
                output, writer_sha = build_windows_package(
                    candidate_root / "candidate-Autosport.exe",
                    candidate_root / "candidate-WINDOWS_START_HERE.txt",
                    candidate_root / "candidate-example",
                    candidate_root / "candidate-diagnostic.json",
                    candidate_root / "candidate-accessibility.json",
                    candidate_root / "candidate-keyboard.json",
                    candidate_root / "candidate-restart.json",
                    package,
                    self.SOURCE_SHA,
                )

            self.assertEqual(output, package)
            self.assertEqual(package.read_bytes(), valid_swap_bytes)
            self.assertNotEqual(writer_sha, valid_swap_sha)
            with self.assertRaisesRegex(
                ValueError,
                "base release package digest does not match exact writer-bound package bytes",
            ):
                data_tool_package.bind_portable_data_tool(
                    package,
                    data_exe,
                    expected_base_package_sha256=writer_sha,
                )

            with zipfile.ZipFile(package, "r") as archive:
                self.assertNotIn("Autosport-V1/Autosport-Data.exe", archive.namelist())


if __name__ == "__main__":
    unittest.main()

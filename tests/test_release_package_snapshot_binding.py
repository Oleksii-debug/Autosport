from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autosport.release_package as release_package


class ReleasePackageSnapshotBindingTests(unittest.TestCase):
    SOURCE_SHA = "a" * 40

    @staticmethod
    def _write_json(path: Path, payload: dict) -> None:
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    def _build_candidate(self, root: Path, label: str) -> Path:
        root.mkdir(parents=True)
        executable = root / "Autosport.exe"
        start = root / "WINDOWS_START_HERE.txt"
        example = root / "example"
        diagnostic = root / "diagnostic.json"
        accessibility = root / "accessibility.json"
        keyboard = root / "keyboard.json"
        restart = root / "restart.json"
        package = root / f"{label}.zip"

        executable.write_bytes(f"{label}-autosport-executable".encode("ascii"))
        start.write_text(f"{label}-start\n", encoding="utf-8")
        example.mkdir()
        (example / "market.jsonl").write_text(
            f'{{"candidate":"{label}"}}\n',
            encoding="utf-8",
        )

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
                "process_recovery_run_id": f"{label}-recovery-run",
                "process_recovery_disposition": "committed",
                "process_recovery_registry_status": "completed",
                "process_recovery_manifest_phase": "completed",
                "process_recovery_base_paper_book_sha256": "1" * 64,
                "process_recovery_base_decision_ledger_sha256": "2" * 64,
                "process_recovery_new_paper_book_sha256": "3" * 64,
                "process_recovery_new_decision_ledger_sha256": "4" * 64,
            },
        )
        release_package.build_windows_package(
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

    def test_reported_package_hash_is_bound_to_verified_snapshot_not_later_path_contents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = self._build_candidate(root / "candidate", "candidate")
            replacement = self._build_candidate(root / "replacement", "replacement")
            candidate_bytes = candidate.read_bytes()
            replacement_bytes = replacement.read_bytes()
            candidate_sha = hashlib.sha256(candidate_bytes).hexdigest()
            replacement_sha = hashlib.sha256(replacement_bytes).hexdigest()
            self.assertNotEqual(candidate_sha, replacement_sha)

            real_decode = release_package._decode_json_object
            swapped = False

            def replace_live_path_after_snapshot(payload: bytes, label: str):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    candidate.write_bytes(replacement_bytes)
                return real_decode(payload, label)

            with patch.object(
                release_package,
                "_decode_json_object",
                side_effect=replace_live_path_after_snapshot,
            ):
                result = release_package.verify_windows_package(
                    candidate,
                    expected_source_sha=self.SOURCE_SHA,
                )

            self.assertTrue(swapped)
            self.assertEqual(candidate.read_bytes(), replacement_bytes)
            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["package_sha256"], candidate_sha)
            self.assertNotEqual(result["package_sha256"], replacement_sha)


if __name__ == "__main__":
    unittest.main()

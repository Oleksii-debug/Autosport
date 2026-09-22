from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from autosport.release_package import build_windows_package, verify_windows_package


class ReleasePackageSecretContentFalsifierTests(unittest.TestCase):
    """Require secret-content exclusion at the canonical Windows ZIP boundary."""

    SOURCE_SHA = "a" * 40
    SCAN_CHUNK = 1024 * 1024

    @staticmethod
    def _write_json(path: Path, payload: dict[str, object]) -> None:
        path.write_text(
            json.dumps(payload, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    @classmethod
    def _build_candidate(cls, root: Path, *, example_payload: bytes) -> Path:
        exe = root / "Autosport.exe"
        start = root / "WINDOWS_START_HERE.txt"
        example = root / "tt_demo"
        diagnostic = root / "packaged-diagnostic.json"
        accessibility = root / "accessibility-audit.json"
        keyboard = root / "keyboard-audit.json"
        restart = root / "restart-recovery-audit.json"
        package = root / "Autosport.zip"

        exe.write_bytes(b"synthetic executable fixture")
        start.write_text("Synthetic test start guide.\n", encoding="utf-8")
        example.mkdir()
        (example / "operator-notes.txt").write_bytes(example_payload)

        common_audit = {
            "status": "PASS",
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
        for path in (diagnostic, accessibility, keyboard):
            cls._write_json(path, common_audit)

        restart_payload = {
            **common_audit,
            "session_restart_status": "PASS",
            "transaction_recovery_status": "PASS",
            "recovery_disposition": "aborted_uncommitted",
            "process_kill_relaunch_status": "PASS",
            "process_kill_stage_pid": 1001,
            "process_recovery_pid": 1002,
            "process_kill_return_code": -9,
            "process_recovery_run_id": "synthetic-secret-falsifier-run",
            "process_recovery_disposition": "committed",
            "process_recovery_registry_status": "completed",
            "process_recovery_manifest_phase": "completed",
            "process_recovery_base_paper_book_sha256": "1" * 64,
            "process_recovery_base_decision_ledger_sha256": "2" * 64,
            "process_recovery_new_paper_book_sha256": "3" * 64,
            "process_recovery_new_decision_ledger_sha256": "4" * 64,
        }
        cls._write_json(restart, restart_payload)

        build_windows_package(
            exe,
            start,
            example,
            diagnostic,
            accessibility,
            keyboard,
            restart,
            package,
            cls.SOURCE_SHA,
        )
        return package

    def test_control_package_without_secret_content_still_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(
                Path(temporary),
                example_payload=b"synthetic public example data\n",
            )
            result = verify_windows_package(
                package,
                expected_source_sha=self.SOURCE_SHA,
            )
            self.assertEqual(result["status"], "PASS")

    def test_harmless_filename_cannot_hide_secret_assignment_content(self) -> None:
        synthetic_secret = b"not-a-real-secret-" + b"A" * 64
        payload = b'api_key="' + synthetic_secret + b'"\n'
        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(
                Path(temporary),
                example_payload=payload,
            )
            with self.assertRaisesRegex(ValueError, "secret|credential"):
                verify_windows_package(
                    package,
                    expected_source_sha=self.SOURCE_SHA,
                )

    def test_long_secret_assignment_crossing_one_mib_boundary_is_rejected(self) -> None:
        marker = b'api_key="'
        synthetic_secret = b"not-a-real-secret-" + b"B" * 600
        payload = (
            b"A" * (self.SCAN_CHUNK - 520)
            + marker
            + synthetic_secret
            + b'"\n'
        )
        marker_offset = payload.index(marker)
        self.assertEqual(marker_offset, self.SCAN_CHUNK - 520)
        self.assertGreater(payload.index(b'"\n', marker_offset), self.SCAN_CHUNK)

        with tempfile.TemporaryDirectory() as temporary:
            package = self._build_candidate(
                Path(temporary),
                example_payload=payload,
            )
            with self.assertRaisesRegex(ValueError, "secret|credential"):
                verify_windows_package(
                    package,
                    expected_source_sha=self.SOURCE_SHA,
                )


if __name__ == "__main__":
    unittest.main()

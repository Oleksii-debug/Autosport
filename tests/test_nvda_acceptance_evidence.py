from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from autosport.data_tool_package import _write_deterministic
from autosport.data_tools_entry import main as data_tools_main
from autosport.nvda_acceptance import create_template, validate_evidence, write_template


class NvdaAcceptanceEvidenceTests(unittest.TestCase):
    @staticmethod
    def _json_bytes(payload: dict) -> bytes:
        return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")

    def _release_zip(self, root: Path, *, source_sha: str = "a" * 40) -> Path:
        exe = b"fake-autosport-exe-for-evidence-contract"
        data_exe = b"fake-autosport-data-exe-for-evidence-contract"
        build_info = {
            "product": "Autosport",
            "version": "test",
            "source_sha": source_sha,
            "autosport_exe_sha256": hashlib.sha256(exe).hexdigest(),
            "autosport_data_exe_sha256": hashlib.sha256(data_exe).hexdigest(),
            "portable_historical_data_tools": True,
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
            "v1_ready": False,
            "whole_product_complete": False,
        }
        audit = {
            "status": "PASS",
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
        }
        restart_audit = {
            **audit,
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
        members: dict[str, bytes] = {
            "Autosport.exe": exe,
            "Autosport-Data.exe": data_exe,
            "WINDOWS_START_HERE.txt": b"test start guide\n",
            "packaged-diagnostic.json": self._json_bytes(audit),
            "accessibility-audit.json": self._json_bytes(audit),
            "keyboard-audit.json": self._json_bytes(audit),
            "restart-recovery-audit.json": self._json_bytes(restart_audit),
            "BUILD_INFO.json": self._json_bytes(build_info),
        }
        manifest_files = {
            relative: hashlib.sha256(payload).hexdigest()
            for relative, payload in sorted(members.items())
        }
        members["PACKAGE_MANIFEST.json"] = self._json_bytes(
            {"schema_version": 1, "files": manifest_files}
        )
        sums = [
            f"{hashlib.sha256(payload).hexdigest()}  {relative}"
            for relative, payload in sorted(members.items())
        ]
        members["SHA256SUMS.txt"] = ("\n".join(sums) + "\n").encode("utf-8")

        package = root / "Autosport-V1-windows-x64.zip"
        _write_deterministic(package, members)
        return package

    @staticmethod
    def _anchors(package: Path, *, source_sha: str = "a" * 40) -> dict[str, str]:
        return {
            "expected_source_sha": source_sha,
            "expected_package_sha256": hashlib.sha256(package.read_bytes()).hexdigest(),
        }

    def _completed_evidence(self, package: Path) -> dict:
        evidence = create_template(package, **self._anchors(package))
        evidence["environment"]["windows_edition_build"] = "Windows 11 24H2 test-build"
        evidence["environment"]["nvda_version"] = "2026.1 test"
        evidence["tested_at"] = "2026-09-13T07:50:00+02:00"
        evidence["tester_label"] = "physical-tester"
        for check in evidence["checks"]:
            check["status"] = "PASS"
            check["notes"] = "Observed physically with NVDA."
        return evidence

    def test_template_is_bound_to_exact_candidate_and_preserves_false_truth_labels(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = self._release_zip(Path(directory))
            evidence = create_template(package, **self._anchors(package))

            self.assertEqual(evidence["candidate"]["package_sha256"], hashlib.sha256(package.read_bytes()).hexdigest())
            self.assertEqual(evidence["candidate"]["source_sha"], "a" * 40)
            self.assertIn("Anchor provenance/independence is not machine-proven", evidence["attestation_scope"])
            self.assertEqual(len(evidence["checks"]), 7)
            self.assertIn("strategy_replay_settings", {check["id"] for check in evidence["checks"]})
            self.assertTrue(all(check["status"] == "PENDING" for check in evidence["checks"]))
            self.assertFalse(evidence["human_tested"])
            self.assertFalse(evidence["nvda_verified"])
            self.assertFalse(evidence["v1_ready"])

    def test_self_consistent_package_with_wrong_external_source_anchor_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = self._release_zip(Path(directory), source_sha="b" * 40)
            with self.assertRaisesRegex(ValueError, "supplied expected source SHA"):
                create_template(
                    package,
                    expected_source_sha="a" * 40,
                    expected_package_sha256=hashlib.sha256(package.read_bytes()).hexdigest(),
                )

    def test_self_consistent_package_with_wrong_external_package_anchor_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = self._release_zip(Path(directory))
            with self.assertRaisesRegex(ValueError, "supplied expected package SHA-256"):
                create_template(
                    package,
                    expected_source_sha="a" * 40,
                    expected_package_sha256="0" * 64,
                )

    def test_legacy_two_member_fake_release_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            exe = b"fake-autosport-exe-for-evidence-contract"
            build_info = {
                "product": "Autosport",
                "version": "test",
                "source_sha": "a" * 40,
                "autosport_exe_sha256": hashlib.sha256(exe).hexdigest(),
                "real_money_execution": False,
                "human_tested": False,
                "nvda_verified": False,
                "v1_ready": False,
                "whole_product_complete": False,
            }
            package = root / "legacy-fake.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("Autosport-V1/Autosport.exe", exe)
                archive.writestr(
                    "Autosport-V1/BUILD_INFO.json",
                    json.dumps(build_info, sort_keys=True).encode("utf-8"),
                )

            with self.assertRaisesRegex(ValueError, "release package is missing required files"):
                create_template(package, **self._anchors(package))

    def test_completed_human_record_validates_identity_without_machine_nvda_promotion(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = self._release_zip(root)
            evidence_path = root / "nvda-evidence.json"
            evidence_path.write_text(json.dumps(self._completed_evidence(package)), encoding="utf-8")

            result = validate_evidence(package, evidence_path, **self._anchors(package))

            self.assertEqual(result["status"], "PASS")
            self.assertTrue(result["candidate_identity_verified"])
            self.assertTrue(result["source_anchor_match_verified"])
            self.assertTrue(result["package_anchor_match_verified"])
            self.assertFalse(result["anchor_provenance_machine_verified"])
            self.assertFalse(result["machine_verified_physical_execution"])
            self.assertTrue(result["requires_owner_release_decision"])
            self.assertFalse(result["human_tested"])
            self.assertFalse(result["nvda_verified"])
            self.assertFalse(result["v1_ready"])

    def test_candidate_hash_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = self._release_zip(root)
            evidence = self._completed_evidence(package)
            evidence["candidate"]["package_sha256"] = "0" * 64
            evidence_path = root / "nvda-evidence.json"
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "package_sha256 does not match release ZIP"):
                validate_evidence(package, evidence_path, **self._anchors(package))

    def test_missing_required_check_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = self._release_zip(root)
            evidence = self._completed_evidence(package)
            evidence["checks"].pop()
            evidence_path = root / "nvda-evidence.json"
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "check set mismatch"):
                validate_evidence(package, evidence_path, **self._anchors(package))

    def test_failed_check_requires_notes_and_returns_non_acceptance_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = self._release_zip(root)
            evidence = self._completed_evidence(package)
            evidence["checks"][0]["status"] = "FAIL"
            evidence["checks"][0]["notes"] = "Focus was not announced."
            evidence_path = root / "nvda-evidence.json"
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

            result = validate_evidence(package, evidence_path, **self._anchors(package))

            self.assertEqual(result["status"], "FAIL")
            self.assertEqual(result["failed_checks"], ["window_initial_focus"])
            self.assertFalse(result["anchor_provenance_machine_verified"])
            self.assertFalse(result["nvda_verified"])

    def test_fail_check_without_defect_notes_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = self._release_zip(root)
            evidence = self._completed_evidence(package)
            evidence["checks"][0]["status"] = "FAIL"
            evidence["checks"][0]["notes"] = ""
            evidence_path = root / "nvda-evidence.json"
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "requires defect notes"):
                validate_evidence(package, evidence_path, **self._anchors(package))

    def test_portable_dispatch_generates_and_validates_same_candidate_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = self._release_zip(root)
            anchors = self._anchors(package)
            evidence_path = root / "nvda-evidence.json"
            validation_path = root / "nvda-validation.json"

            self.assertEqual(
                data_tools_main(
                    [
                        "nvda-evidence-template",
                        "--release-zip", str(package),
                        "--expected-source-sha", anchors["expected_source_sha"],
                        "--expected-package-sha256", anchors["expected_package_sha256"],
                        "--output", str(evidence_path),
                    ]
                ),
                0,
            )
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            completed = self._completed_evidence(package)
            evidence["environment"] = completed["environment"]
            evidence["tested_at"] = completed["tested_at"]
            evidence["tester_label"] = completed["tester_label"]
            evidence["checks"] = completed["checks"]
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

            self.assertEqual(
                data_tools_main(
                    [
                        "verify-nvda-evidence",
                        "--release-zip", str(package),
                        "--expected-source-sha", anchors["expected_source_sha"],
                        "--expected-package-sha256", anchors["expected_package_sha256"],
                        "--evidence", str(evidence_path),
                        "--output", str(validation_path),
                    ]
                ),
                0,
            )
            validation = json.loads(validation_path.read_text(encoding="utf-8"))
            self.assertEqual(validation["status"], "PASS")
            self.assertTrue(validation["source_anchor_match_verified"])
            self.assertTrue(validation["package_anchor_match_verified"])
            self.assertFalse(validation["anchor_provenance_machine_verified"])
            self.assertFalse(validation["machine_verified_physical_execution"])
            self.assertFalse(validation["nvda_verified"])

    def test_template_writer_and_portable_usage_expose_real_product_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = self._release_zip(root)
            output = root / "nvda-evidence.json"

            write_template(package, output, **self._anchors(package))
            self.assertTrue(output.is_file())
            self.assertEqual(data_tools_main(["--help"]), 0)

            usage = Path("src/autosport/data_tools_entry.py").read_text(encoding="utf-8")
            self.assertIn("nvda-evidence-template", usage)
            self.assertIn("verify-nvda-evidence", usage)
            self.assertIn("--expected-source-sha", usage)
            self.assertIn("--expected-package-sha256", usage)

            guide = Path("WINDOWS_START_HERE.txt").read_text(encoding="utf-8")
            self.assertIn("--expected-source-sha", guide)
            self.assertIn("--expected-package-sha256", guide)
            self.assertIn("не можуть самі собі приписати human proof", guide)


if __name__ == "__main__":
    unittest.main()

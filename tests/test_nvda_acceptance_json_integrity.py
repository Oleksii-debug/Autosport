from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from autosport.nvda_acceptance import _REQUIRED_CHECKS, create_template, validate_evidence


_IDENTITY = {
    "package_sha256": "a" * 64,
    "source_sha": "b" * 40,
    "autosport_exe_sha256": "c" * 64,
}


class NvdaAcceptanceJsonIntegrityTests(unittest.TestCase):
    def _evidence(self) -> dict:
        return {
            "schema_version": 1,
            "kind": "physical_nvda_acceptance",
            "candidate": dict(_IDENTITY),
            "environment": {
                "windows_edition_build": "Windows 11 25H2 test-build",
                "nvda_version": "2026.1 test",
            },
            "tested_at": "2026-09-14T10:30:00+02:00",
            "tester_label": "physical-tester",
            "checks": [
                {
                    "id": check_id,
                    "description": description,
                    "status": "PASS",
                    "notes": "Observed physically with NVDA.",
                }
                for check_id, description in _REQUIRED_CHECKS
            ],
            "real_money_execution": False,
            "human_tested": False,
            "nvda_verified": False,
            "v1_ready": False,
        }

    def _validate(self, evidence_path: Path):
        with patch(
            "autosport.nvda_acceptance._load_candidate_identity",
            return_value=dict(_IDENTITY),
        ):
            return validate_evidence(
                "unused-release.zip",
                evidence_path,
                expected_source_sha="b" * 40,
                expected_package_sha256="a" * 64,
            )

    @staticmethod
    def _write_candidate_zip(path: Path, *, marker: str) -> bytes:
        build_info = {
            "source_sha": "b" * 40,
            "autosport_exe_sha256": "c" * 64,
            "marker": marker,
        }
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "Autosport-V1/BUILD_INFO.json",
                json.dumps(build_info, sort_keys=True),
            )
            archive.writestr("Autosport-V1/Autosport.exe", b"candidate-executable")
            archive.writestr(f"Autosport-V1/{marker}.txt", marker.encode("utf-8"))
        return path.read_bytes()

    def test_valid_unambiguous_record_still_passes_machine_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence_path = Path(directory) / "nvda-evidence.json"
            evidence_path.write_text(json.dumps(self._evidence()), encoding="utf-8")

            result = self._validate(evidence_path)

            self.assertEqual(result["status"], "PASS")
            self.assertFalse(result["machine_verified_physical_execution"])
            self.assertFalse(result["human_tested"])
            self.assertFalse(result["nvda_verified"])
            self.assertFalse(result["v1_ready"])

    def test_duplicate_root_key_is_rejected_before_semantic_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence_path = Path(directory) / "nvda-evidence.json"
            payload = json.dumps(self._evidence())
            payload = payload[:-1] + ', "tester_label": "shadow-tester"}'
            evidence_path.write_text(payload, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "duplicate JSON object key: tester_label"):
                self._validate(evidence_path)

    def test_duplicate_nested_check_status_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence_path = Path(directory) / "nvda-evidence.json"
            payload = json.dumps(self._evidence())
            payload = payload.replace(
                '"status": "PASS"',
                '"status": "FAIL", "status": "PASS"',
                1,
            )
            evidence_path.write_text(payload, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "duplicate JSON object key: status"):
                self._validate(evidence_path)

    def test_nonstandard_json_constant_is_rejected_even_when_semantically_unused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence_path = Path(directory) / "nvda-evidence.json"
            payload = json.dumps(self._evidence())
            payload = payload[:-1] + ', "unexpected_numeric_evidence": NaN}'
            evidence_path.write_text(payload, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "non-standard JSON constant: NaN"):
                self._validate(evidence_path)

    def test_boolean_schema_version_does_not_alias_integer_one(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = self._evidence()
            evidence["schema_version"] = True
            evidence_path = Path(directory) / "nvda-evidence.json"
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "schema/kind mismatch"):
                self._validate(evidence_path)

    def test_human_check_description_cannot_be_redefined_while_reusing_canonical_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            evidence = self._evidence()
            evidence["checks"][0]["description"] = "Application opened."
            evidence_path = Path(directory) / "nvda-evidence.json"
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "canonical physical test contract"):
                self._validate(evidence_path)

    def test_candidate_verification_uses_one_immutable_release_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release.zip"
            replacement = root / "replacement.zip"
            original_bytes = self._write_candidate_zip(release, marker="original")
            replacement_bytes = self._write_candidate_zip(replacement, marker="replacement")
            expected_package_sha = hashlib.sha256(original_bytes).hexdigest()
            snapshot_paths: list[Path] = []

            def verify_windows(candidate_path, *, expected_source_sha):
                snapshot = Path(candidate_path)
                snapshot_paths.append(snapshot)
                self.assertNotEqual(snapshot.resolve(), release.resolve())
                self.assertEqual(snapshot.read_bytes(), original_bytes)
                self.assertEqual(expected_source_sha, "b" * 40)
                release.write_bytes(replacement_bytes)
                return {"status": "PASS"}

            def verify_data_tool(candidate_path):
                snapshot = Path(candidate_path)
                snapshot_paths.append(snapshot)
                self.assertEqual(snapshot_paths[0], snapshot)
                self.assertEqual(snapshot.read_bytes(), original_bytes)
                self.assertEqual(release.read_bytes(), replacement_bytes)
                return {"status": "PASS"}

            with patch(
                "autosport.nvda_acceptance.verify_windows_package",
                side_effect=verify_windows,
            ), patch(
                "autosport.nvda_acceptance.verify_portable_data_tool",
                side_effect=verify_data_tool,
            ):
                template = create_template(
                    release,
                    expected_source_sha="b" * 40,
                    expected_package_sha256=expected_package_sha,
                )

            self.assertEqual(template["candidate"]["package_sha256"], expected_package_sha)
            self.assertEqual(len(snapshot_paths), 2)
            self.assertFalse(snapshot_paths[0].exists())


if __name__ == "__main__":
    unittest.main()

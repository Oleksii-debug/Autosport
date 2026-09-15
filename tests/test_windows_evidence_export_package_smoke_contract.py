from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "evidence_export_package_smoke.ps1"


class WindowsEvidenceExportPackageSmokeContractTests(unittest.TestCase):
    def test_smoke_uses_only_packaged_data_tool_for_export_verify_round_trip(self) -> None:
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("dist/Autosport-Data.exe", script)
        self.assertIn(".build-fresh-extraction/Autosport-V1/Autosport-Data.exe", script)
        self.assertGreaterEqual(script.count("'export-evidence'"), 2)
        self.assertGreaterEqual(script.count("'verify-evidence'"), 3)
        self.assertIn("evidence_export=PASS", script)
        self.assertIn("evidence_verify=PASS", script)
        self.assertIn("evidence_verify=FAIL_CLOSED", script)
        self.assertNotIn("python ", script.lower())
        self.assertNotIn("autosport.evidence_export", script)

    def test_smoke_proves_determinism_metadata_truth_and_tamper_fail_closed(self) -> None:
        script = SCRIPT.read_text(encoding="utf-8")

        for canonical_name in (
            "decisions.jsonl",
            "paper_book.json",
            "run_registry.json",
            "source_health.json",
            "run-00000000-0000-4000-8000-000000000001.json",
        ):
            with self.subTest(canonical_name=canonical_name):
                self.assertIn(canonical_name, script)

        for field in (
            "file_contents_included",
            "market_database_included",
            "raw_historical_or_provider_bytes_included",
            "environment_or_credential_values_included",
            "arbitrary_workspace_files_included",
            "real_money_execution",
        ):
            with self.subTest(field=field):
                self.assertIn(field, script)

        self.assertIn("Get-FileHash", script)
        self.assertIn("manifest_byte_determinism_verified = $true", script)
        self.assertIn("package-smoke-content-sentinel", script)
        self.assertIn("file_content_sentinel_absent = $true", script)
        self.assertIn("AppendAllText", script)
        self.assertIn("$tamperedVerify.ExitCode -ne 3", script)
        self.assertIn("tampered_workspace_fail_closed_verified = $true", script)

    def test_smoke_preserves_release_truth_boundaries(self) -> None:
        script = SCRIPT.read_text(encoding="utf-8")

        self.assertIn("real_money_execution = $false", script)
        self.assertIn("human_tested = $false", script)
        self.assertIn("nvda_verified = $false", script)
        self.assertIn("v1_ready = $false", script)
        self.assertNotIn("real_money_execution = $true", script)
        self.assertNotIn("human_tested = $true", script)
        self.assertNotIn("nvda_verified = $true", script)
        self.assertNotIn("v1_ready = $true", script)


if __name__ == "__main__":
    unittest.main()

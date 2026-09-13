from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class WindowsNvdaArtifactContractTests(unittest.TestCase):
    def test_nvda_smoke_binds_machine_contract_into_fresh_extraction_aggregate(self) -> None:
        script = (ROOT / "scripts" / "nvda_evidence_package_smoke.ps1").read_text(encoding="utf-8")

        self.assertIn("dist/fresh-extraction-verification.json", script)
        self.assertIn("$verification.source_sha -ne $expectedSourceSha", script)
        self.assertIn("$verification.package_sha256 -ne $packageSha", script)
        self.assertNotIn("$verification.source_sha -ne $buildInfo.source_sha", script)

        required_bindings = {
            "extracted_nvda_evidence_contract_status": "$evidence.status",
            "extracted_nvda_candidate_identity_verified": "$packaged.candidate_identity_verified -and $fresh.candidate_identity_verified",
            "extracted_nvda_source_anchor_match_verified": "$evidence.source_anchor_match_verified",
            "extracted_nvda_source_anchor_workflow_context": "$evidence.source_anchor_workflow_context",
            "extracted_nvda_package_anchor_match_verified": "$evidence.package_anchor_match_verified",
            "extracted_nvda_package_anchor_input_scope": "$evidence.package_anchor_input_scope",
            "extracted_nvda_anchor_provenance_machine_verified": "$false",
            "extracted_nvda_package_anchor_external_provenance_verified": "$evidence.package_anchor_external_provenance_verified",
            "extracted_nvda_required_checks_pending": "$packaged.required_checks_pending",
            "extracted_nvda_untouched_pending_template_rejected": "$packaged.untouched_pending_template_rejected -and $fresh.untouched_pending_template_rejected",
            "extracted_nvda_machine_verified_physical_execution": "$evidence.machine_verified_physical_execution",
            "extracted_nvda_template_sha256": "$packaged.template_sha256",
        }
        for field, source in required_bindings.items():
            with self.subTest(field=field):
                self.assertIn(field, script)
                self.assertIn(source, script)

        self.assertIn("$expectedSourceSha = [string]$env:AUTOSPORT_SOURCE_SHA", script)
        self.assertIn("source_anchor_workflow_context = 'github_actions_exact_head'", script)
        self.assertIn("package_anchor_input_scope = 'self_computed_contract_smoke_only'", script)
        self.assertIn("package_anchor_external_provenance_verified = $false", script)
        self.assertIn("anchor_provenance_machine_verified = $false", script)
        self.assertIn("human_tested = $false", script)
        self.assertIn("nvda_verified = $false", script)
        self.assertIn("v1_ready = $false", script)
        self.assertIn("machine_verified_physical_execution = $false", script)

    def test_windows_artifact_keeps_nvda_sidecars_alongside_aggregate(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "windows-build.yml").read_text(encoding="utf-8")

        for artifact in (
            "dist/fresh-extraction-verification.json",
            "dist/nvda-evidence-package-smoke.json",
            "dist/nvda-evidence-packaged-template.json",
            "dist/nvda-evidence-fresh-extracted-template.json",
        ):
            with self.subTest(artifact=artifact):
                self.assertIn(artifact, workflow)


if __name__ == "__main__":
    unittest.main()

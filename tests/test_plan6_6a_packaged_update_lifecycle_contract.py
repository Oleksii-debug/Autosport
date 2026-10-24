from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/windows_packaged_update_lifecycle_smoke.ps1"
WORKFLOW = ROOT / ".github/workflows/windows-build.yml"


def test_windows_workflow_executes_and_uploads_update_lifecycle() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "Execute exact packaged update rollback and interruption recovery" in workflow
    assert "./scripts/windows_packaged_update_lifecycle_smoke.ps1" in workflow
    assert "-CurrentPackageSha256 $env:AUTOSPORT_QUALIFIED_PACKAGE_SHA256" in workflow
    assert "Upload exact packaged update lifecycle evidence" in workflow
    assert "path: dist/packaged-update-lifecycle-evidence.json" in workflow
    assert "if-no-files-found: error" in workflow


def test_smoke_uses_real_packaged_lifecycle_commands_and_fail_closed_scope() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for command in (
        "install-release", "upgrade-release", "verify-installed-release",
        "rollback-release", "finalize-update-release", "recover-update-release",
    ):
        assert command in source
    assert "derive_previous_release_fixture.py" in source
    assert "FIXTURE_ONLY_PREVIOUS_VERSION_ENVELOPE" in source
    assert "fixture_only_same_candidate_bytes_with_prior_version_envelope" in source
    assert "historical_release_qualified = $false" in source
    assert "same_schema_update_qualified = $true" in source
    assert "cross_package_schema_migration_qualified = $false" in source


def test_smoke_never_claims_human_nvda_real_money_or_product_completion() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    for marker in (
        "real_money_execution = $false",
        "human_tested = $false",
        "nvda_verified = $false",
        "whole_product_complete = $false",
    ):
        assert marker in source

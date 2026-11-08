from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/windows_release_lifecycle_smoke.ps1"
WORKFLOW = ROOT / ".github/workflows/windows-build.yml"
QUALIFIER = ROOT / "scripts/qualify_plan6_source.py"


def test_exact_windows_workflow_executes_and_uploads_packaged_lifecycle() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "Execute exact packaged install/uninstall/reinstall lifecycle" in workflow
    assert "./scripts/windows_release_lifecycle_smoke.ps1" in workflow
    assert "-PackagePath (Join-Path $PWD 'dist/Autosport-V1-windows-x64.zip')" in workflow
    assert "-PackagedDataExe $env:AUTOSPORT_PACKAGED_DATA_EXE" in workflow
    assert "-ExpectedPackageSha256 $env:AUTOSPORT_QUALIFIED_PACKAGE_SHA256" in workflow
    assert "Upload exact packaged release lifecycle evidence" in workflow
    assert "path: dist/packaged-release-lifecycle-evidence.json" in workflow
    assert "if-no-files-found: error" in workflow


def test_smoke_uses_canonical_packaged_commands_and_both_exact_tool_copies() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    for command in ("install-release", "verify-installed-release", "uninstall-release"):
        assert command in script
    assert script.count("-ExpectedStatus 'INSTALLED'") == 2
    assert script.count("-ExpectedStatus 'VERIFIED'") == 3
    assert script.count("-ExpectedStatus 'UNINSTALLED'") == 2
    assert "-Executable $packagedTool" in script
    assert "-Executable $freshTool" in script
    assert "$packagedToolSha -cne $freshToolSha" in script
    assert "occupied_destination_rejected = $true" in script
    assert "unexpected quarantine artifact" in script


def test_smoke_binds_exact_source_package_and_preserves_truth_boundaries() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    assert "Get-FileHash -LiteralPath $package -Algorithm SHA256" in script
    assert "[string]$record.source_sha -ne $SourceSha" in script
    assert "[string]$record.package_sha256 -ne $ExpectedPackageSha256" in script
    assert "[int]$record.file_count -le 0" in script
    assert "schema_migration_qualified = $false" in script
    for flag in (
        "real_money_execution = $false",
        "human_tested = $false",
        "nvda_verified = $false",
        "whole_product_complete = $false",
    ):
        assert flag in script
    assert "EXACT_WINDOWS_PACKAGED_RELEASE_LIFECYCLE" in script


def test_smoke_fails_closed_without_logging_success_output_or_private_paths() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    assert "Packaged release lifecycle action failed closed unexpectedly" in script
    assert "$raw | ConvertFrom-Json -ErrorAction Stop" in script
    assert "$duplicateOutput -cne 'install=FAIL_CLOSED reason=unsafe_or_unverified_release'" in script
    assert "first_install_status" in script
    assert "destination =" not in script.split("$report = [ordered]@{", 1)[1]
    assert "sandbox =" not in script.split("$report = [ordered]@{", 1)[1]


def test_plan6_source_qualifier_cannot_omit_lifecycle_gate_contract() -> None:
    qualifier = QUALIFIER.read_text(encoding="utf-8")
    assert '"tests/test_plan6_6a_packaged_lifecycle_smoke_contract.py"' in qualifier

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/windows_packaged_state_migration_smoke.ps1"
WORKFLOW = ROOT / ".github/workflows/windows-build.yml"
QUALIFIER = ROOT / "scripts/qualify_plan6_source.py"


def test_exact_windows_workflow_executes_and_uploads_packaged_state_migration() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert "Execute packaged canonical state migration" in workflow
    assert "./scripts/windows_packaged_state_migration_smoke.ps1" in workflow
    assert "-PackagedDataExe $env:AUTOSPORT_PACKAGED_DATA_EXE" in workflow
    assert "-SourceSha $env:AUTOSPORT_SOURCE_SHA" in workflow
    assert "Upload packaged canonical state migration evidence" in workflow
    assert "path: dist/packaged-state-migration-evidence.json" in workflow
    assert "if-no-files-found: error" in workflow


def test_smoke_invokes_exact_packaged_tool_twice_and_requires_idempotence() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    invocation = "& $PackagedDataExe migrate-market-store --database $database"
    assert script.count(invocation) == 2
    assert "$first.migration_applied -ne $true" in script
    assert "$second.migration_applied -ne $false" in script
    assert "$second.state_sha256 -ne [string]$first.state_sha256" in script
    assert "$first.history_count -ne 1 -or $first.current_count -ne 1" in script
    assert "$second.history_count -ne 1 -or $second.current_count -ne 1" in script
    assert "data_exe_sha256 = (Get-FileHash -LiteralPath $PackagedDataExe" in script


def test_smoke_preserves_state_scope_and_truth_boundaries() -> None:
    script = SCRIPT.read_text(encoding="utf-8")
    assert "AUTOMATED_WINDOWS_PACKAGED_STATE_MIGRATION" in script
    assert "canonical_market_store_legacy_current_quotes_projection" in script
    assert "cross_package_schema_migration_qualified = $false" in script
    for flag in (
        "real_money_execution = $false",
        "human_tested = $false",
        "nvda_verified = $false",
        "whole_product_complete = $false",
    ):
        assert flag in script
    assert "state_kind -cne 'canonical_market_store'" in script
    assert "^[0-9a-f]{64}$" in script


def test_plan6_source_qualifier_cannot_omit_state_migration_contract() -> None:
    qualifier = QUALIFIER.read_text(encoding="utf-8")
    assert '"tests/test_plan6_6a_packaged_state_migration_contract.py"' in qualifier

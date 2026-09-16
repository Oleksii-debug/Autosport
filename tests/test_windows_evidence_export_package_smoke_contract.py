import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE = REPO_ROOT / "scripts" / "evidence_export_package_smoke.ps1"


def _smoke_text() -> str:
    return SMOKE.read_text(encoding="utf-8")


def test_packaged_evidence_smoke_binds_release_identity_before_execution() -> None:
    text = _smoke_text()

    assert "AUTOSPORT_SOURCE_SHA" in text
    assert "AUTOSPORT_QUALIFIED_PACKAGE_SHA256" in text
    assert "AUTOSPORT_PACKAGED_ROOT" in text
    assert "AUTOSPORT_PACKAGED_DATA_EXE" in text
    assert "fresh-extraction-verification.json" in text
    assert "packaged_executable_authority.ps1" in text
    assert "Get-AutosportProducerPackageIdentity" in text
    assert "BUILD_INFO source_sha does not match exact workflow head" in text
    assert "autosport_data_exe_sha256" in text
    assert "BUILD_INFO Autosport-Data.exe hash is not bound to producer package identity" in text
    assert "Qualified packaged Autosport-Data.exe hash mismatch" in text
    assert "Fresh-extracted Autosport-Data.exe hash mismatch" in text
    assert "Fresh-extraction verification package_sha256 mismatch" in text


def test_packaged_evidence_smoke_executes_both_qualified_data_tools() -> None:
    text = _smoke_text()

    assert "function Invoke-EvidenceRoundTrip" in text
    assert "-Exe $packagedDataExe" in text
    assert "-Exe $freshDataExe" in text
    assert "@('export-evidence', $Workspace, '--output', $ManifestPath)" in text
    assert "@('verify-evidence', $ManifestPath, '--workspace', $Workspace)" in text
    assert "evidence_export=PASS" in text
    assert "evidence_verify=PASS" in text
    assert "Evidence manifest identity changed between qualified packaged and fresh-extracted executables" in text
    assert "Evidence manifest bytes changed between qualified packaged and fresh-extracted executables" in text


def test_packaged_evidence_smoke_holds_executable_authority_through_all_launches() -> None:
    text = _smoke_text()

    packaged_acquire = "Open-AutosportQualifiedExecutable -Path $packagedDataExe"
    fresh_acquire = "Open-AutosportQualifiedExecutable -Path $freshDataExe"
    first_round_trip = "$packagedRoundTrip = Invoke-EvidenceRoundTrip"
    negative_probe = "$negative = Invoke-DataTool"
    fresh_dispose = "$freshAuthority.Dispose()"
    packaged_dispose = "$packagedAuthority.Dispose()"

    for fragment in (
        packaged_acquire,
        fresh_acquire,
        first_round_trip,
        negative_probe,
        fresh_dispose,
        packaged_dispose,
    ):
        assert fragment in text

    assert text.index(packaged_acquire) < text.index(first_round_trip)
    assert text.index(fresh_acquire) < text.index(first_round_trip)
    assert text.index(first_round_trip) < text.index(negative_probe)
    assert text.index(negative_probe) < text.index(fresh_dispose)
    assert text.index(negative_probe) < text.index(packaged_dispose)
    assert "-ExpectedSha256 $producerDataExeSha" in text
    assert "qualified_executable_authority_held = $true" in text


def test_packaged_evidence_smoke_preserves_metadata_only_truth_boundary() -> None:
    text = _smoke_text()

    for field in (
        "file_contents_included",
        "market_database_included",
        "raw_historical_or_provider_bytes_included",
        "environment_or_credential_values_included",
        "arbitrary_workspace_files_included",
        "real_money_execution",
    ):
        assert field in text

    for canonical_name in (
        "decisions.jsonl",
        "paper_book.json",
        "run_registry.json",
        "source_health.json",
    ):
        assert canonical_name in text

    assert "AUTOSPORT_PACKAGE_SMOKE_SECRET_SENTINEL_DO_NOT_EXPORT_7D8A6B" in text
    assert "credential.env" in text
    assert "market.sqlite" in text
    assert "raw-provider.bin" in text
    assert "secret_sentinel_excluded = $true" in text
    assert "real_money_execution = $false" in text
    assert "human_tested = $false" in text
    assert "nvda_verified = $false" in text


def test_packaged_evidence_smoke_has_fail_closed_tamper_probe() -> None:
    text = _smoke_text()

    tamper = "[System.IO.File]::AppendAllText((Join-Path $workspace 'paper_book.json'), '{\"tamper\":true}'"
    negative = "@('verify-evidence', $packagedManifestPath, '--workspace', $workspace)"
    assert tamper in text
    assert negative in text
    assert text.index(tamper) < text.index(negative)
    assert "$negative.ExitCode -ne 3" in text
    assert "evidence_verify=FAIL_CLOSED" in text
    assert "tampered_workspace_verify_status = 'FAIL_CLOSED'" in text


def test_packaged_evidence_smoke_parses_with_powershell_on_windows() -> None:
    if os.name != "nt":
        pytest.skip("PowerShell parser check is Windows-specific")

    shell = shutil.which("pwsh") or shutil.which("powershell")
    if shell is None:
        pytest.skip("PowerShell is unavailable")

    target_env = "AUTOSPORT_POWERSHELL_PARSE_TARGET"
    env = os.environ.copy()
    env[target_env] = str(SMOKE)
    parser_command = r"""
$tokens = $null
$errors = $null
$target = $env:AUTOSPORT_POWERSHELL_PARSE_TARGET
if ([string]::IsNullOrWhiteSpace($target)) {
    Write-Error 'AUTOSPORT_POWERSHELL_PARSE_TARGET is required'
    exit 1
}
[System.Management.Automation.Language.Parser]::ParseFile($target, [ref]$tokens, [ref]$errors) | Out-Null
if ($errors.Count -ne 0) {
    $errors | ForEach-Object { Write-Error $_.Message }
    exit 1
}
"""
    subprocess.run(
        [
            shell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            parser_command,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )

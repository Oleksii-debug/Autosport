import os
import shutil
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
SMOKE = REPO_ROOT / "scripts" / "evidence_export_package_smoke.ps1"


def _smoke_text() -> str:
    return SMOKE.read_text(encoding="utf-8")


def _powershell() -> str | None:
    if os.name != "nt":
        return None
    return shutil.which("pwsh") or shutil.which("powershell")


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

    assert "$expectedFileRecordKeys = @('path', 'sha256', 'size_bytes')" in text
    assert "evidence file record keys mismatch" in text
    assert "evidence file record path must be a string" in text
    assert "evidence file record size_bytes must be an integer" in text
    assert "evidence file record size_bytes must be non-negative" in text
    assert "evidence file record sha256 is invalid" in text
    assert "canonical evidence path set/order mismatch" in text
    assert "AUTOSPORT_PACKAGE_SMOKE_SECRET_SENTINEL_DO_NOT_EXPORT_7D8A6B" in text
    assert "credential.env" in text
    assert "market.sqlite" in text
    assert "raw-provider.bin" in text
    assert "secret_sentinel_excluded = $true" in text
    assert "real_money_execution = $false" in text
    assert "human_tested = $false" in text
    assert "nvda_verified = $false" in text


def test_packaged_evidence_smoke_rejects_content_like_file_record_field() -> None:
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell adversarial manifest check is Windows-specific")

    target_env = "AUTOSPORT_POWERSHELL_CONTRACT_TARGET"
    env = os.environ.copy()
    env[target_env] = str(SMOKE)
    command = r"""
$tokens = $null
$errors = $null
$target = $env:AUTOSPORT_POWERSHELL_CONTRACT_TARGET
$ast = [System.Management.Automation.Language.Parser]::ParseFile($target, [ref]$tokens, [ref]$errors)
if ($errors.Count -ne 0) {
    $errors | ForEach-Object { Write-Error $_.Message }
    exit 1
}
foreach ($name in @('Require-CanonicalHex', 'Assert-EvidenceManifestContract')) {
    $definition = $ast.Find(
        {
            param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq $name
        },
        $true
    )
    if ($null -eq $definition) {
        Write-Error "missing function: $name"
        exit 1
    }
    Invoke-Expression $definition.Extent.Text
}

$sha = '0' * 64
$files = @(
    [pscustomobject][ordered]@{path='decisions.jsonl'; size_bytes=1; sha256=$sha; content='FORGED-CONTENT'},
    [pscustomobject][ordered]@{path='paper_book.json'; size_bytes=1; sha256=$sha},
    [pscustomobject][ordered]@{path='run_registry.json'; size_bytes=1; sha256=$sha},
    [pscustomobject][ordered]@{path='source_health.json'; size_bytes=1; sha256=$sha}
)
$manifest = [pscustomobject][ordered]@{
    schema_version = 1
    kind = 'autosport-workspace-evidence-manifest'
    file_count = 4
    files = $files
    fixed_evidence_set_complete = $true
    run_summary_count = 0
    file_contents_included = $false
    market_database_included = $false
    raw_historical_or_provider_bytes_included = $false
    environment_or_credential_values_included = $false
    arbitrary_workspace_files_included = $false
    real_money_execution = $false
    manifest_sha256 = $sha
}
try {
    Assert-EvidenceManifestContract `
        -Manifest $manifest `
        -RawManifest ($manifest | ConvertTo-Json -Depth 6) `
        -Label 'Adversarial' `
        -SecretSentinel 'NOT-PRESENT'
    Write-Error 'forged content-like file record field was accepted'
    exit 2
} catch {
    if ($_.Exception.Message -notmatch 'evidence file record keys mismatch') {
        Write-Error $_.Exception.Message
        exit 3
    }
}
"""
    subprocess.run(
        [
            shell,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            command,
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )


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
    shell = _powershell()
    if shell is None:
        pytest.skip("PowerShell parser check is Windows-specific")

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

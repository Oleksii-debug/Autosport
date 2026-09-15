from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW = _ROOT / ".github" / "workflows" / "windows-build.yml"
_AUTHORITY = _ROOT / "scripts" / "packaged_executable_authority.ps1"


def _step(workflow: str, name: str, next_name: str) -> str:
    start = workflow.index(f"      - name: {name}")
    end = workflow.index(f"      - name: {next_name}", start)
    return workflow[start:end]


def test_post_package_consumers_hold_digest_bound_executable_authority() -> None:
    workflow = _WORKFLOW.read_text(encoding="utf-8")

    cases = (
        ("Execute packaged NVDA evidence contract", "Prove packaged workspace recovery reachability"),
        ("Prove packaged workspace recovery reachability", "Execute packaged walk-forward evaluation"),
        ("Execute packaged walk-forward evaluation", "Execute packaged retrospective forecast-origin evaluation"),
        ("Execute packaged retrospective forecast-origin evaluation", "Audit packaged research demo path"),
        ("Audit packaged research demo path", "External UIA fresh-extraction gate"),
        ("External UIA fresh-extraction gate", "Upload external UIA failure evidence"),
    )
    for name, next_name in cases:
        section = _step(workflow, name, next_name)
        assert ". ./scripts/packaged_executable_authority.ps1" in section
        assert "Get-AutosportProducerPackageIdentity" in section
        assert "Open-AutosportQualifiedExecutable" in section
        assert ".Dispose()" in section

    assert "run: ./scripts/nvda_evidence_package_smoke.ps1" not in workflow
    assert "run: ./scripts/walk_forward_package_smoke.ps1" not in workflow
    assert "run: ./scripts/walk_forward_origin_package_smoke.ps1" not in workflow


def test_executable_authority_binds_package_identity_and_pins_consumer_file() -> None:
    authority = _AUTHORITY.read_text(encoding="utf-8")

    assert "AUTOSPORT_PRODUCER_PACKAGE_SHA256" in authority
    assert "AUTOSPORT_SOURCE_SHA" in authority
    assert "Autosport-V1/BUILD_INFO.json" in authority
    assert "autosport_exe_sha256" in authority
    assert "autosport_data_exe_sha256" in authority
    assert "[System.IO.FileShare]::Read" in authority
    assert "ComputeHash($stream)" in authority
    assert "SHA-256 mismatch before process start" in authority
    assert "real_money_execution -ne $false" in authority
    assert "human_tested -ne $false" in authority
    assert "nvda_verified -ne $false" in authority


@pytest.mark.skipif(
    os.name != "nt" or shutil.which("pwsh") is None,
    reason="real Windows executable authority replacement regression",
)
def test_replaced_executable_is_rejected_before_consumer_start(tmp_path: Path) -> None:
    target = tmp_path / "Autosport-Data.exe"
    marker = tmp_path / "consumer-started.txt"
    original = b"ORIGINAL_QUALIFIED_EXECUTABLE"
    target.write_bytes(original)
    expected = hashlib.sha256(original).hexdigest()

    # Model the already-qualified extraction being replaced during the later
    # pathname-only handoff window.
    target.write_bytes(b"REPLACED_AFTER_INITIAL_EXTRACTION_VERIFICATION")

    command = rf"""
$ErrorActionPreference = 'Stop'
. '{_AUTHORITY}'
$authority = $null
try {{
  $authority = Open-AutosportQualifiedExecutable -Path '{target}' -ExpectedSha256 '{expected}' -Label 'replacement probe'
  [System.IO.File]::WriteAllText('{marker}', 'PROCESS_STARTED')
}} finally {{
  if ($null -ne $authority) {{ $authority.Dispose() }}
}}
"""
    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "SHA-256 mismatch before process start" in (
        completed.stdout + "\n" + completed.stderr
    )
    assert not marker.exists()


@pytest.mark.skipif(
    os.name != "nt" or shutil.which("pwsh") is None,
    reason="real Windows executable authority lock regression",
)
def test_qualified_executable_lock_blocks_write_and_replacement_until_release(
    tmp_path: Path,
) -> None:
    target = tmp_path / "Autosport.exe"
    original = b"QUALIFIED_EXECUTABLE"
    target.write_bytes(original)
    expected = hashlib.sha256(original).hexdigest()

    command = rf"""
$ErrorActionPreference = 'Stop'
. '{_AUTHORITY}'
$authority = Open-AutosportQualifiedExecutable -Path '{target}' -ExpectedSha256 '{expected}' -Label 'lock probe'
try {{
  $blocked = $false
  try {{
    $writer = [System.IO.File]::Open(
      '{target}',
      [System.IO.FileMode]::Open,
      [System.IO.FileAccess]::Write,
      [System.IO.FileShare]::ReadWrite
    )
    $writer.Dispose()
  }} catch [System.IO.IOException] {{
    $blocked = $true
  }}
  if (-not $blocked) {{ throw 'qualified executable authority permitted concurrent writer' }}
}} finally {{
  $authority.Dispose()
}}
"""

    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr
    assert target.read_bytes() == original


@pytest.mark.skipif(
    os.name != "nt" or shutil.which("pwsh") is None,
    reason="real Windows pre-existing writer regression",
)
def test_preexisting_executable_writer_blocks_authority_acquisition(tmp_path: Path) -> None:
    target = tmp_path / "Autosport.exe"
    original = b"QUALIFIED_EXECUTABLE_WITH_PREOPENED_WRITER"
    target.write_bytes(original)
    expected = hashlib.sha256(original).hexdigest()

    command = rf"""
$ErrorActionPreference = 'Stop'
. '{_AUTHORITY}'
$writer = [System.IO.File]::Open(
  '{target}',
  [System.IO.FileMode]::Open,
  [System.IO.FileAccess]::Write,
  [System.IO.FileShare]::ReadWrite
)
try {{
  $rejected = $false
  try {{
    $authority = Open-AutosportQualifiedExecutable -Path '{target}' -ExpectedSha256 '{expected}' -Label 'pre-existing writer probe'
    $authority.Dispose()
  }} catch [System.IO.IOException] {{
    $rejected = $true
  }}
  if (-not $rejected) {{ throw 'qualified executable authority admitted a pre-existing writer' }}
}} finally {{
  $writer.Dispose()
}}
"""

    completed = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr

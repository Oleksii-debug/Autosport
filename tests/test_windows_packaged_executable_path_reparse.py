from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_AUTHORITY = _ROOT / "scripts" / "packaged_executable_authority.ps1"
_REAL_WINDOWS_PWSH = os.name == "nt" and shutil.which("pwsh") is not None


def _run_pwsh(command: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
    )


def _make_junction(link: Path, target: Path) -> None:
    completed = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr


def test_packaged_executable_authority_pins_non_reparse_path_components() -> None:
    authority = _AUTHORITY.read_text(encoding="utf-8")

    assert "FILE_FLAG_OPEN_REPARSE_POINT" in authority
    assert "FILE_ATTRIBUTE_REPARSE_POINT" in authority
    assert "GetFileInformationByHandle" in authority
    assert "componentPaths.Add(root)" in authority
    assert "handles.Add(OpenComponent" in authority
    assert "FILE_SHARE_READ" in authority
    assert "qualified executable path component is a reparse point" in authority
    assert "PackagedExecutablePathFence]::Acquire($fullPath)" in authority
    assert "path must be canonical before qualification" in authority


@pytest.mark.skipif(
    not _REAL_WINDOWS_PWSH,
    reason="real Windows junction rejection regression",
)
def test_ancestor_junction_is_rejected_before_executable_consumer(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted"
    trusted.mkdir()
    executable = trusted / "Autosport.exe"
    payload = b"TRUSTED_EXECUTABLE_BYTES"
    executable.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()

    junction = tmp_path / "qualified-via-junction"
    _make_junction(junction, trusted)
    candidate = junction / executable.name
    marker = tmp_path / "consumer-started.txt"

    command = rf"""
$ErrorActionPreference = 'Stop'
. '{_AUTHORITY}'
$authority = $null
try {{
  $authority = Open-AutosportQualifiedExecutable -Path '{candidate}' -ExpectedSha256 '{expected}' -Label 'junction probe'
  [System.IO.File]::WriteAllText('{marker}', 'PROCESS_STARTED')
}} finally {{
  if ($null -ne $authority) {{ $authority.Dispose() }}
}}
"""
    completed = _run_pwsh(command)
    combined = completed.stdout + "\n" + completed.stderr

    assert completed.returncode != 0
    assert "reparse point" in combined.lower()
    assert not marker.exists()


@pytest.mark.skipif(
    not _REAL_WINDOWS_PWSH,
    reason="real Windows leaf symlink rejection regression",
)
def test_leaf_symlink_is_rejected_before_executable_consumer(tmp_path: Path) -> None:
    trusted = tmp_path / "trusted.exe"
    payload = b"TRUSTED_LEAF_EXECUTABLE_BYTES"
    trusted.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()
    candidate = tmp_path / "Autosport.exe"
    try:
        os.symlink(trusted, candidate, target_is_directory=False)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is unavailable on this runner")
        raise
    marker = tmp_path / "consumer-started.txt"

    command = rf"""
$ErrorActionPreference = 'Stop'
. '{_AUTHORITY}'
$authority = $null
try {{
  $authority = Open-AutosportQualifiedExecutable -Path '{candidate}' -ExpectedSha256 '{expected}' -Label 'leaf symlink probe'
  [System.IO.File]::WriteAllText('{marker}', 'PROCESS_STARTED')
}} finally {{
  if ($null -ne $authority) {{ $authority.Dispose() }}
}}
"""
    completed = _run_pwsh(command)
    combined = completed.stdout + "\n" + completed.stderr

    assert completed.returncode != 0
    assert "reparse point" in combined.lower()
    assert not marker.exists()


@pytest.mark.skipif(
    not _REAL_WINDOWS_PWSH,
    reason="real Windows pathname substitution regression",
)
def test_qualified_parent_cannot_be_replaced_by_junction_before_launch(
    tmp_path: Path,
) -> None:
    comspec = Path(os.environ["ComSpec"])
    active = tmp_path / "active"
    hostile = tmp_path / "hostile"
    moved = tmp_path / "moved"
    active.mkdir()
    hostile.mkdir()
    candidate = active / "Autosport.exe"
    shutil.copyfile(comspec, candidate)
    # A distinct payload makes an unexpected pathname retarget visibly non-equivalent.
    (hostile / "Autosport.exe").write_bytes(b"HOSTILE_REPLACEMENT_EXECUTABLE")
    expected = hashlib.sha256(candidate.read_bytes()).hexdigest()
    trusted_marker = tmp_path / "trusted-started.txt"
    hostile_marker = tmp_path / "hostile-started.txt"

    command = rf"""
$ErrorActionPreference = 'Stop'
. '{_AUTHORITY}'
$authority = Open-AutosportQualifiedExecutable -Path '{candidate}' -ExpectedSha256 '{expected}' -Label 'namespace swap probe'
try {{
  $renameBlocked = $false
  try {{
    [System.IO.Directory]::Move('{active}', '{moved}')
  }} catch {{
    $renameBlocked = $true
  }}
  if (-not $renameBlocked) {{
    cmd.exe /d /c mklink /J "{active}" "{hostile}" | Out-Null
    [System.IO.File]::WriteAllText('{hostile_marker}', 'PATH_SUBSTITUTION_SUCCEEDED')
    throw 'qualified executable parent was replaceable before process start'
  }}

  $process = Start-Process -FilePath '{candidate}' -ArgumentList '/d', '/c', 'echo TRUSTED>"{trusted_marker}"' -Wait -PassThru
  if ($process.ExitCode -ne 0) {{ throw "trusted executable exited $($process.ExitCode)" }}
}} finally {{
  $authority.Dispose()
}}
"""
    completed = _run_pwsh(command)

    assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr
    assert trusted_marker.exists()
    assert not hostile_marker.exists()
    assert candidate.exists()
    assert not moved.exists()

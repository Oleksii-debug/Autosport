"""Protect the Windows candidate's canonical restart/recovery failure diagnostics.

A failed builder must surface its existing machine-readable recovery report,
propagate the original failure and never proceed to stage-neutral repackaging.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_windows_candidate.ps1"


def _failure_section(source: str) -> str:
    start = source.index("try {\n  Invoke-WindowsCandidateCoreText -CoreText $candidateText")
    end = source.index("# The stage-neutral transition", start)
    section = source[start:end]
    assert section.count("Invoke-WindowsCandidateCoreText -CoreText $candidateText") == 1
    assert "} catch {" in section
    assert "dist/restart-recovery-audit.json" in section
    assert "Test-Path -LiteralPath $restartRecoveryAudit -PathType Leaf" in section
    assert "[System.IO.File]::ReadAllText($restartRecoveryAudit)" in section
    assert "packaged restart/recovery audit evidence" in section
    assert section.rstrip().endswith("throw\n}")
    assert source.index("Invoke-WindowsCandidateCoreText") < source.index(
        "# The stage-neutral transition"
    )
    return section


def test_failure_recovery_report_is_preserved_before_repack() -> None:
    _failure_section(SCRIPT.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "removed_contract",
    [
        "dist/restart-recovery-audit.json",
        "[System.IO.File]::ReadAllText($restartRecoveryAudit)",
        "Test-Path -LiteralPath $restartRecoveryAudit -PathType Leaf",
    ],
)
def test_failure_diagnostics_gate_rejects_missing_contract(removed_contract: str) -> None:
    original = SCRIPT.read_text(encoding="utf-8")
    assert removed_contract in original
    altered = original.replace(removed_contract, "REMOVED_FAILURE_FENCE", 1)
    with pytest.raises(AssertionError):
        _failure_section(altered)


@pytest.mark.skipif(
    shutil.which("pwsh") is None,
    reason="PowerShell required for real script failure-path execution",
)
def test_failed_builder_emits_existing_report_and_never_repacks(tmp_path: Path) -> None:
    section = _failure_section(SCRIPT.read_text(encoding="utf-8"))
    report = tmp_path / "dist" / "restart-recovery-audit.json"
    report.parent.mkdir(parents=True)
    report.write_text(
        '{"status":"FAIL","reason":"synthetic-recovery-probe"}',
        encoding="utf-8",
    )
    script = (
        "$ErrorActionPreference = 'Stop'\n"
        "function Invoke-WindowsCandidateCoreText { throw 'synthetic-canonical-build-failure' }\n"
        "$candidateText = 'synthetic'\n"
        + section
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode != 0
    assert "synthetic-recovery-probe" in result.stdout
    assert "synthetic-canonical-build-failure" in result.stderr
    assert not (tmp_path / "dist" / "Autosport-windows-x64.zip").exists()

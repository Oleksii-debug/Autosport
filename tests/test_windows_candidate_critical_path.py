from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_windows_builder_stays_canonical_and_candidate_skip_is_separate() -> None:
    builder = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
    candidate = (ROOT / "scripts" / "build_windows_candidate.ps1").read_text(
        encoding="utf-8"
    )

    # Existing build/security contracts intentionally inspect the canonical builder
    # source. The acceleration must not relocate that authority behind a wrapper.
    assert "[switch] $SkipTests" not in builder
    assert "build_windows_core.ps1" not in builder
    assert builder.count("python -m pytest -v tests") == 1
    assert 'throw "Full pytest gate exited $LASTEXITCODE"' in builder
    assert "$trustedSourceSnapshotVerifierLauncher = @'" in builder
    assert "$trustedGitSourceOracleLauncher = @'" in builder
    assert "$trustedPackageLauncher = @'" in builder

    assert "build_windows.ps1" in candidate
    assert "windows_build_skip_gate.ps1" in candidate
    assert "ConvertTo-WindowsCandidateCoreText" in candidate
    assert "Invoke-WindowsCandidateCoreText -CoreText $candidateText" in candidate
    assert "[scriptblock]::Create" not in candidate


def test_windows_candidate_opts_into_only_builder_local_skip() -> None:
    workflow = (ROOT / ".github" / "workflows" / "windows-build.yml").read_text(
        encoding="utf-8"
    )

    assert "python-version: '3.12'" in workflow
    assert "cache: pip" in workflow
    assert "cache-dependency-path: pyproject.toml" in workflow
    assert "run: ./scripts/build_windows_candidate.ps1" in workflow
    assert "run: ./scripts/build_windows.ps1 -SkipTests" not in workflow

    # The optimization must not remove downstream package qualification gates.
    for required_gate in (
        "Verify exact pristine source checkout before build",
        "Materialize verified independent package extraction",
        "Execute packaged NVDA evidence contract",
        "Prove packaged workspace recovery reachability",
        "Execute packaged walk-forward evaluation",
        "Execute packaged retrospective forecast-origin evaluation",
        "Execute packaged evidence export/verify contract",
        "External UIA fresh-extraction gate",
    ):
        assert required_gate in workflow


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell is Windows-CI coverage")
def test_windows_candidate_skip_transform_executes_fail_closed() -> None:
    helper = ROOT / "scripts" / "windows_build_skip_gate.ps1"
    fixture = """python -c \"print('before')\"\npython -m pytest -v tests\nif ($LASTEXITCODE -ne 0) { throw \"Full pytest gate exited $LASTEXITCODE\" }\npython -c \"print('after')\"\n"""
    escaped_helper = str(helper).replace("'", "''")
    command = f"""
. '{escaped_helper}'
$core = @'
{fixture}
'@
$result = ConvertTo-WindowsCandidateCoreText -CoreText $core
if ($result.Contains('python -m pytest -v tests')) {{ throw 'pytest gate was not removed' }}
if (-not $result.Contains('BUILDER_LOCAL_PYTEST=SKIPPED_BY_EXPLICIT_CALLER')) {{ throw 'skip marker missing' }}
if (-not $result.Contains('python -c "print(''before'')"')) {{ throw 'before Python command was not preserved' }}
if (-not $result.Contains('python -c "print(''after'')"')) {{ throw 'after Python command was not preserved' }}
try {{ ConvertTo-WindowsCandidateCoreText -CoreText ($core + $core) | Out-Null; throw 'duplicate gate unexpectedly accepted' }} catch {{
  if ($_.Exception.Message -notmatch 'exactly one canonical builder-local pytest gate') {{ throw }}
}}
exit 0
"""
    subprocess.run(
        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

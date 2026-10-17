from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _assert_exact_source_materializer(candidate: str) -> None:
    """Permit one scriptblock only after a Git-pinned materializer is read as bytes.

    The earlier blanket syntax ban rejected the safe exact-SHA materializer in the
    canonical #1606 lineage, even though a separate source-contract test requires it.
    Keep a *stronger* provenance and execution-order gate instead of dropping the gate.
    """
    markers = (
        "Invoke-WindowsCandidateCoreText -CoreText $candidateText",
        "ls-tree $sourceSha -- $materializerRepoPath",
        "if (-not $materializerMatch.Success)",
        "ArgumentList.Add('cat-file')",
        "ArgumentList.Add($materializerBlobSha)",
        "StandardOutput.BaseStream.CopyTo($materializerBuffer)",
        "if ($materializerBytes.Length -eq 0)",
        "$strictUtf8.GetString($materializerBytes)",
        "[scriptblock]::Create($materializerText)",
        "& $materializerScript -SourceSha $sourceSha -RepoRoot $repoRoot",
    )
    positions = [candidate.find(marker) for marker in markers]
    assert all(position >= 0 for position in positions)
    assert positions == sorted(positions)
    assert len(set(positions)) == len(positions)

    # Untrusted/dynamic execution outside this one exact-source handoff is forbidden.
    assert candidate.count("[scriptblock]::Create(") == 1
    assert "[scriptblock]::Create" not in candidate[: positions[1]]
    assert "Stage-neutral release source SHA does not match checkout HEAD" in candidate
    assert "GIT_NO_REPLACE_OBJECTS" in candidate
    assert "^(100644|100755) blob ([0-9a-f]{40})" in candidate
    assert "[System.Text.UTF8Encoding]::new($false, $true)" in candidate


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
    _assert_exact_source_materializer(candidate)


@pytest.mark.parametrize(
    ("trusted", "mutated"),
    (
        ("ls-tree $sourceSha -- $materializerRepoPath", "ls-tree HEAD -- $materializerRepoPath"),
        ("ArgumentList.Add($materializerBlobSha)", "ArgumentList.Add('HEAD')"),
        ("GIT_NO_REPLACE_OBJECTS", "GIT_REPLACE_OBJECTS_ALLOWED"),
        (
            "[System.Text.UTF8Encoding]::new($false, $true)",
            "[System.Text.UTF8Encoding]::new($false, $false)",
        ),
        ("[scriptblock]::Create($materializerText)", "[scriptblock]::Create($builderText)"),
    ),
)
def test_windows_candidate_materializer_trust_boundary_rejects_tampering(
    trusted: str, mutated: str
) -> None:
    candidate = (ROOT / "scripts" / "build_windows_candidate.ps1").read_text(
        encoding="utf-8"
    )
    assert candidate.count(trusted) == 1
    with pytest.raises(AssertionError):
        _assert_exact_source_materializer(candidate.replace(trusted, mutated, 1))


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


def test_final_stage_neutral_zip_runs_data_and_uia_from_unicode_extraction() -> None:
    """Enforce runtime smoke on the actual final shipping ZIP, not a precursor."""
    workflow = (ROOT / ".github" / "workflows" / "windows-build.yml").read_text(
        encoding="utf-8"
    )
    section = workflow.split("- name: Verify independent stage-neutral release extraction", 1)[1]
    section = section.split("      - uses: actions/upload-artifact@", 1)[0]
    for marker in (
        ".build-stage-neutral-Перевірка шляху з пробілами",
        "$stageDataExe = Join-Path $stagePackageRoot 'Autosport-Data.exe'",
        "if ([string]$stageInfo.autosport_data_exe_sha256 -ne $stageDataDigest)",
        "& $stageDataExe verify-dataset $stageDemo",
        "if ($LASTEXITCODE -ne 0)",
        "./scripts/external_uia_audit.ps1 -Exe $stageExe -Output $stageUiaAuditPath",
        "if ($stageUiaAudit.status -ne 'PASS')",
        "evidence_class = 'AUTOMATED_WINDOWS_SHIPPING_ZIP_SMOKE'",
        "whole_product_complete = $false",
    ):
        assert marker in section
    assert "continue-on-error:" not in section
    upload = workflow.split("name: Autosport-Windows-stage-neutral-prehuman", 1)[1]
    assert "dist/stage-neutral-dataset-smoke.json" in upload
    assert "dist/stage-neutral-external-uia-audit.json" in upload

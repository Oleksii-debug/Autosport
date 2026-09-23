from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _script(name: str) -> str:
    return (REPO_ROOT / "scripts" / name).read_text(encoding="utf-8")


def test_materializer_wires_exact_stage_neutral_release_contract() -> None:
    script = _script("materialize_stage_neutral_release.ps1")

    assert "Autosport-V1-windows-x64.zip" in script
    assert "Autosport-windows-x64.zip" in script
    assert "stage-neutral-release-verification.json" in script
    assert "src/autosport/stage_neutral_release.py" in script
    assert "src/autosport/release_package.py" in script
    assert "Get-ExactGitBlobBytes" in script
    assert "ls-tree $SourceSha" in script
    assert "cat-file" in script
    assert "-I -S -B -c $stageNeutralLauncher" in script
    assert "$trustedEvidenceLines = @(" in script
    assert "evidenceBindingFields" in script
    assert "package digest does not match exact repack process evidence" in script
    assert 'types.ModuleType("autosport")' in script
    assert "spec_from_file_location" in script
    assert "-m autosport.stage_neutral_release" not in script
    assert "$env:PYTHONPATH" not in script
    assert "package_sha256" in script
    assert "Get-FileHash" in script
    assert "real_money_execution -ne $false" in script
    assert "human_tested -ne $false" in script
    assert "nvda_verified -ne $false" in script
    assert "whole_product_complete -ne $false" in script


def test_canonical_candidate_build_executes_exact_git_materializer() -> None:
    script = _script("build_windows_candidate.ps1")

    build_index = script.index("Invoke-WindowsCandidateCoreText")
    materializer_index = script.index("$materializerRepoPath")
    cat_file_index = script.index("cat-file", materializer_index)
    scriptblock_index = script.index("[scriptblock]::Create", cat_file_index)
    invocation_index = script.index("& $materializerScript", scriptblock_index)

    assert build_index < materializer_index < cat_file_index < scriptblock_index < invocation_index
    assert "scripts/materialize_stage_neutral_release.ps1" in script
    assert "ls-tree $sourceSha" in script
    assert "GIT_NO_REPLACE_OBJECTS" in script
    assert "Stage-neutral release source SHA does not match checkout HEAD" in script
    assert "-SourceSha $sourceSha -RepoRoot $repoRoot" in script
    assert "build_stage_neutral_windows_candidate.ps1" not in script
    assert "& $materializer -SourceSha" not in script

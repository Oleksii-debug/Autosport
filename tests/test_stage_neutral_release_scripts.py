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
    assert "-m autosport.stage_neutral_release" in script
    assert "--source-sha $SourceSha" in script
    assert "Get-Command python -CommandType Application" in script
    assert "$env:PYTHONPATH = $sourceRoot" in script
    assert "package_sha256" in script
    assert "Get-FileHash" in script
    assert "real_money_execution -ne $false" in script
    assert "human_tested -ne $false" in script
    assert "nvda_verified -ne $false" in script
    assert "whole_product_complete -ne $false" in script


def test_canonical_candidate_build_materializes_stage_neutral_release() -> None:
    script = _script("build_windows_candidate.ps1")

    build_index = script.index("Invoke-WindowsCandidateCoreText")
    materializer_index = script.index("materialize_stage_neutral_release.ps1")
    assert build_index < materializer_index
    assert "^[0-9a-f]{40}$" in script
    assert "& $materializer -SourceSha $sourceSha" in script
    assert "build_stage_neutral_windows_candidate.ps1" not in script

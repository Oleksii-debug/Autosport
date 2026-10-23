from __future__ import annotations

import ast
from pathlib import Path
import re


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
    assert '"--require-source-manifest"' in script
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


def test_stage_neutral_release_is_independently_checked_and_published() -> None:
    """The production Windows CI artifact must include the *new* exact ZIP."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "windows-build.yml").read_text(
        encoding="utf-8"
    )
    verification = workflow.index("name: Verify independent stage-neutral release extraction")
    legacy_upload = workflow.index("name: Autosport-V1-Windows-prehuman")
    stage_upload = workflow.index("name: Autosport-Windows-stage-neutral-prehuman")
    assert verification < legacy_upload < stage_upload
    stage_gate = workflow[verification:legacy_upload]
    for required in (
        "stage-neutral-release-verification.json",
        "stageReport.source_sha",
        "stageReport.package_sha256",
        "stageReport.real_money_execution",
        "Get-FileHash -LiteralPath $stageArchive -Algorithm SHA256",
        "Expand-Archive -LiteralPath $stageArchive",
        "BUILD_INFO.json",
        "stageInfo.autosport_exe_sha256",
        "stageInfo.nvda_verified",
        "whole_product_complete",
    ):
        if required == "stageReport.real_money_execution":
            # Truth-label checks are iterated through all fields.
            assert "@('real_money_execution', 'human_tested', 'nvda_verified', 'whole_product_complete')" in stage_gate
        elif required == "stageInfo.nvda_verified":
            assert "$stageInfo.$field -ne $false" in stage_gate
        else:
            assert required in stage_gate
    stage_artifact = workflow[stage_upload:]
    assert "dist/Autosport-windows-x64.zip" in stage_artifact
    assert "dist/stage-neutral-release-verification.json" in stage_artifact
    assert "if-no-files-found: error" in stage_artifact



def _staged_release_module_names(script: str) -> set[str]:
    """Read only the explicitly pinned Git-blob staging map."""
    match = re.search(
        r"\\$trustedSources = \\[ordered\\]@\\{(?P<entries>.*?)\\n    \\}",
        script,
        flags=re.DOTALL,
    )
    assert match is not None, "exact-source module staging map missing"
    pairs = re.findall(
        r"(?m)^\\s*'src/autosport/([a-z_]+\\.py)'\\s*=\\s*'([a-z_]+\\.py)'\\s*$",
        match.group("entries"),
    )
    assert len(pairs) == len(set(pairs)), "duplicate staged source entry"
    assert all(source == target for source, target in pairs), (
        "staged source module name must preserve the package-relative import name"
    )
    return {source for source, _ in pairs}


def _missing_relative_imports(staged: set[str]) -> set[str]:
    missing: set[str] = set()
    for name in staged:
        source = (REPO_ROOT / "src" / "autosport" / name).read_text(
            encoding="utf-8"
        )
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.ImportFrom) or node.level != 1:
                continue
            if node.module is None:
                dependencies = [alias.name + ".py" for alias in node.names]
            else:
                dependencies = [node.module.split(".", 1)[0] + ".py"]
            missing.update(dependency for dependency in dependencies if dependency not in staged)
    return missing


def test_stage_neutral_exact_git_source_import_closure() -> None:
    """A real Windows failure: release_package imported absent release_sbom.

    Isolated python -I -S must not find omitted autosport modules in the live
    checkout. Pin the full transitive import graph to exact Git source blobs.
    """
    script = _script("materialize_stage_neutral_release.ps1")
    staged = _staged_release_module_names(script)
    assert staged == {
        "stage_neutral_release.py",
        "release_package.py",
        "release_sbom.py",
        "release_dependency_graph.py",
        "release_binary_attribution.py",
        "windows_update_release_metadata.py",
        "windows_update_admission.py",
    }
    assert _missing_relative_imports(staged) == set()
    assert "Get-ExactGitBlobBytes -RepoPath" in script
    assert "[System.IO.File]::WriteAllBytes(" in script
    assert "$pythonExecutable -I -S -B -c $stageNeutralLauncher" in script


def test_stage_neutral_import_closure_detects_each_missing_transitive_module() -> None:
    """Negative regression: dropping a required Git blob fails the source gate."""
    staged = _staged_release_module_names(
        _script("materialize_stage_neutral_release.ps1")
    )
    for required in (
        "release_package.py",
        "release_sbom.py",
        "release_dependency_graph.py",
        "release_binary_attribution.py",
        "windows_update_release_metadata.py",
        "windows_update_admission.py",
    ):
        assert required in _missing_relative_imports(staged - {required}), required

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_windows_builder_skip_is_explicit_and_default_safe() -> None:
    wrapper = (ROOT / "scripts" / "build_windows.ps1").read_text(encoding="utf-8")
    core = (ROOT / "scripts" / "build_windows_core.ps1").read_text(encoding="utf-8")

    assert "[switch] $SkipTests" in wrapper
    assert "if (-not $SkipTests)" in wrapper
    assert "& $coreScript" in wrapper
    assert wrapper.index("if (-not $SkipTests)") < wrapper.index("function python")
    assert "BUILDER_LOCAL_PYTEST=SKIPPED_BY_EXPLICIT_CALLER" in wrapper
    assert "skippedBuilderPytestGate -ne 1" in wrapper
    assert core.count("python -m pytest -v tests") == 1
    assert 'throw "Full pytest gate exited $LASTEXITCODE"' in core


def test_windows_candidate_opts_into_only_builder_local_skip() -> None:
    workflow = (ROOT / ".github" / "workflows" / "windows-build.yml").read_text(
        encoding="utf-8"
    )

    assert "python-version: '3.12'" in workflow
    assert "cache: pip" in workflow
    assert "cache-dependency-path: pyproject.toml" in workflow
    assert "run: ./scripts/build_windows.ps1 -SkipTests" in workflow

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

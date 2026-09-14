from pathlib import Path


_BUILD_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_windows.ps1"


def _build_script_text() -> str:
    return _BUILD_SCRIPT.read_text(encoding="utf-8")


def test_local_windows_build_proves_pristine_source_before_mutation() -> None:
    script = _build_script_text()

    source_sha = "$sourceSha = $env:AUTOSPORT_SOURCE_SHA"
    preflight = "python scripts/verify_source_checkout.py --source-sha $sourceSha"
    first_mutation = "python -m pip install --upgrade pip"

    assert script.index(source_sha) < script.index(preflight) < script.index(first_mutation)
    assert 'if ($LASTEXITCODE -ne 0) { throw "Source checkout preflight exited $LASTEXITCODE" }' in script


def test_local_windows_build_runs_canonical_full_pytest_gate() -> None:
    script = _build_script_text()

    dependency_install = "python -m pip install -e '.[build,test]'"
    pytest_gate = "python -m pytest -v tests"
    first_build = "python -m PyInstaller"

    assert script.index(dependency_install) < script.index(pytest_gate) < script.index(first_build)
    assert "python -m unittest discover" not in script
    assert 'if ($LASTEXITCODE -ne 0) { throw "build/test dependency install exited $LASTEXITCODE" }' in script
    assert 'if ($LASTEXITCODE -ne 0) { throw "Full pytest gate exited $LASTEXITCODE" }' in script

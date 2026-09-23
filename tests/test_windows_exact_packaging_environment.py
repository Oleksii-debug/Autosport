from __future__ import annotations

import json
import pathlib
import shutil
import subprocess
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
BUILD_SCRIPT = ROOT / "scripts" / "build_windows.ps1"


def _build_script() -> str:
    return BUILD_SCRIPT.read_text(encoding="utf-8")


def _extract_here_string(script: str, variable: str) -> str:
    marker = f"${variable} = @'\n"
    start = script.index(marker) + len(marker)
    end = script.index("\n'@", start)
    return script[start:end]


def _git(*args: str, cwd: pathlib.Path) -> subprocess.CompletedProcess[str]:
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is required for exact-source packaging-environment regression")
    return subprocess.run(
        [git, *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


def test_packaging_interpreter_is_created_after_late_source_gate_and_owns_both_builds() -> None:
    script = _build_script()

    late_gate = script.index(
        "python $sourceVerifier --source-sha $sourceSha --late-build-boundary\n"
    )
    requirement_oracle = script.index(
        "$trustedPackagingRequirementLines = @(", late_gate
    )
    venv_create = script.index(
        "& $pythonExecutable -I -S -m venv --clear $packagingVenv",
        requirement_oracle,
    )
    dependency_install = script.index(
        "& $packagingPython -I -m pip --isolated install ", venv_create
    )
    exact_snapshot = script.index(
        "$trustedBuildManifestLines = @(", dependency_install
    )

    assert late_gate < requirement_oracle < venv_create < dependency_install < exact_snapshot
    assert script.count("& $packagingPython -I -m PyInstaller ") == 2
    assert "& $pythonExecutable -I -m PyInstaller " not in script
    assert script.count("& $packagingPython -I -c $autosportResolutionProbe") == 2
    assert "--disable-pip-version-check --no-input" in script


def test_requirement_oracle_uses_committed_pyproject_not_mutated_worktree(
    tmp_path: pathlib.Path,
) -> None:
    script = _build_script()
    launcher = _extract_here_string(script, "trustedPackagingRequirementsLauncher")

    _git("init", "-q", cwd=tmp_path)
    _git("config", "user.email", "autosport-test@example.invalid", cwd=tmp_path)
    _git("config", "user.name", "Autosport Test", cwd=tmp_path)

    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[project]\n"
        'name = "fixture"\n'
        'dependencies = ["runtime-dep==1.2.3"]\n'
        "\n[project.optional-dependencies]\n"
        'build = ["builder-dep==4.5.6"]\n',
        encoding="utf-8",
    )
    _git("add", "pyproject.toml", cwd=tmp_path)
    _git("commit", "-q", "-m", "fixture", cwd=tmp_path)
    source_sha = _git("rev-parse", "HEAD", cwd=tmp_path).stdout.strip()

    # Simulate the release blocker: the live checkout is mutated after the exact
    # commit was selected. The requirement oracle must continue to read only the
    # source_sha Git object and must never consume this attacker-controlled text.
    pyproject.write_text(
        "[project]\n"
        'name = "fixture"\n'
        'dependencies = ["attacker-package==9.9.9"]\n'
        "\n[project.optional-dependencies]\n"
        'build = ["attacker-builder==9.9.9"]\n',
        encoding="utf-8",
    )

    git = shutil.which("git")
    assert git is not None
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            launcher,
            git,
            str(tmp_path),
            source_sha,
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == [
        "builder-dep==4.5.6",
        "runtime-dep==1.2.3",
    ]
    assert "attacker" not in completed.stdout


def test_requirement_oracle_rejects_non_exact_packaging_requirement(
    tmp_path: pathlib.Path,
) -> None:
    script = _build_script()
    launcher = _extract_here_string(script, "trustedPackagingRequirementsLauncher")

    _git("init", "-q", cwd=tmp_path)
    _git("config", "user.email", "autosport-test@example.invalid", cwd=tmp_path)
    _git("config", "user.name", "Autosport Test", cwd=tmp_path)

    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        'name = "fixture"\n'
        'dependencies = ["runtime-dep>=1"]\n'
        "\n[project.optional-dependencies]\n"
        'build = ["builder-dep==4.5.6"]\n',
        encoding="utf-8",
    )
    _git("add", "pyproject.toml", cwd=tmp_path)
    _git("commit", "-q", "-m", "fixture", cwd=tmp_path)
    source_sha = _git("rev-parse", "HEAD", cwd=tmp_path).stdout.strip()

    git = shutil.which("git")
    assert git is not None
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            launcher,
            git,
            str(tmp_path),
            source_sha,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )

    assert completed.returncode != 0
    assert "must be a simple == pin" in completed.stderr

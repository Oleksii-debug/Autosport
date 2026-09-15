from __future__ import annotations

import subprocess
import zipfile
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_BUILD_SCRIPT = _ROOT / "scripts" / "build_windows.ps1"


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_windows_build_runs_both_pyinstaller_consumers_from_exact_source_snapshot() -> None:
    script = _BUILD_SCRIPT.read_text(encoding="utf-8")

    trusted_gate = "python $sourceVerifier --source-sha $sourceSha --late-build-boundary"
    snapshot_archive = (
        '& $gitExecutable archive --format=zip "--output=$trustedBuildArchive" $sourceSha'
    )
    snapshot_expand = (
        "Expand-Archive -LiteralPath $trustedBuildArchive "
        "-DestinationPath $trustedBuildRoot -Force"
    )
    snapshot_install = "python -m pip install --no-deps --force-reinstall $trustedBuildRoot"
    enter_snapshot = "Push-Location $trustedBuildRoot"
    leave_snapshot = "Pop-Location"
    gui_build = (
        "python -m PyInstaller --noconfirm --clean --onefile --windowed "
        "--name Autosport src/autosport/windows_entry.py"
    )
    data_build = (
        "python -m PyInstaller --noconfirm --clean --onefile --console "
        "--name Autosport-Data src/autosport/data_tools_entry.py"
    )
    gui_bound_source = "$builtAutosportExe = Join-Path $trustedBuildRoot 'dist/Autosport.exe'"
    data_bound_source = "$builtDataExe = Join-Path $trustedBuildRoot 'dist/Autosport-Data.exe'"

    gate_index = script.index(trusted_gate)
    archive_index = script.index(snapshot_archive)
    expand_index = script.index(snapshot_expand)
    install_index = script.index(snapshot_install)
    first_enter_index = script.index(enter_snapshot, install_index)
    gui_index = script.index(gui_build, first_enter_index)
    first_leave_index = script.index(leave_snapshot, gui_index)
    gui_bound_index = script.index(gui_bound_source, first_leave_index)
    second_gate_index = script.index(
        trusted_gate + " --allow-release-outputs",
        gui_bound_index,
    )
    second_enter_index = script.index(enter_snapshot, second_gate_index)
    data_index = script.index(data_build, second_enter_index)
    second_leave_index = script.index(leave_snapshot, data_index)
    data_bound_index = script.index(data_bound_source, second_leave_index)

    assert (
        gate_index
        < archive_index
        < expand_index
        < install_index
        < first_enter_index
        < gui_index
        < first_leave_index
        < gui_bound_index
        < second_gate_index
        < second_enter_index
        < data_index
        < second_leave_index
        < data_bound_index
    )
    assert script.count(snapshot_archive) == 1
    assert script.count(enter_snapshot) == 2
    assert "$builtAutosportExe = Join-Path $PWD 'dist/Autosport.exe'" not in script
    assert "$builtDataExe = Join-Path $PWD 'dist/Autosport-Data.exe'" not in script


def test_exact_source_archive_ignores_post_proof_live_entry_mutation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    package_dir = repo / "src" / "autosport"
    package_dir.mkdir(parents=True)

    canonical_gui = b"GUI = 'canonical'\n"
    canonical_data = b"DATA = 'canonical'\n"
    gui_entry = package_dir / "windows_entry.py"
    data_entry = package_dir / "data_tools_entry.py"
    gui_entry.write_bytes(canonical_gui)
    data_entry.write_bytes(canonical_data)
    (repo / "pyproject.toml").write_text(
        "[build-system]\nrequires = []\nbuild-backend = 'setuptools.build_meta'\n",
        encoding="utf-8",
    )

    _git(repo, "init")
    _git(repo, "config", "user.email", "autosport-test@example.invalid")
    _git(repo, "config", "user.name", "Autosport Test")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "canonical exact source")
    source_sha = _git(repo, "rev-parse", "HEAD")

    # Reproduce the reviewed race after source proof: both tracked entry files are
    # replaced and an untracked hook is introduced before the source consumer runs.
    gui_entry.write_bytes(b"GUI = 'hostile replacement'\n")
    data_entry.write_bytes(b"DATA = 'hostile replacement'\n")
    (package_dir / "hostile_hook.py").write_text("HOSTILE = True\n", encoding="utf-8")

    archive = tmp_path / "trusted-build-source.zip"
    subprocess.run(
        ["git", "archive", "--format=zip", f"--output={archive}", source_sha],
        cwd=repo,
        check=True,
    )
    snapshot = tmp_path / "trusted-build-source"
    with zipfile.ZipFile(archive) as handle:
        handle.extractall(snapshot)

    assert (snapshot / "src" / "autosport" / "windows_entry.py").read_bytes() == canonical_gui
    assert (snapshot / "src" / "autosport" / "data_tools_entry.py").read_bytes() == canonical_data
    assert not (snapshot / "src" / "autosport" / "hostile_hook.py").exists()

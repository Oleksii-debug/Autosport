from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


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


def _trusted_snapshot_verifier_source() -> str:
    script = _BUILD_SCRIPT.read_text(encoding="utf-8")
    start_marker = "$trustedSourceSnapshotVerifierLauncher = @'\n"
    end_marker = "\n'@\n"
    assert start_marker in script
    tail = script.split(start_marker, 1)[1]
    assert end_marker in tail
    return tail.split(end_marker, 1)[0]


def test_windows_build_runs_both_pyinstaller_consumers_from_locked_exact_source_snapshot() -> None:
    script = _BUILD_SCRIPT.read_text(encoding="utf-8")

    trusted_gate = "python $sourceVerifier --source-sha $sourceSha --late-build-boundary"
    snapshot_archive = (
        '& $gitExecutable archive --format=zip "--output=$trustedBuildArchive" $sourceSha'
    )
    snapshot_expand = (
        "Expand-Archive -LiteralPath $trustedBuildArchive "
        "-DestinationPath $trustedBuildRoot -Force"
    )
    write_fence = '& icacls $trustedBuildRoot /deny "*${currentSid}:(OI)(CI)(W,D,DC)" /T /C'
    remove_write_fence = '& icacls $trustedBuildRoot /remove:d "*${currentSid}" /T /C'
    snapshot_verify = (
        "$trustedBuildManifestJson | & $pythonExecutable -I -S -c "
        "$trustedSourceSnapshotVerifierLauncher $trustedBuildRoot"
    )
    gui_build = (
        "& $pythonExecutable -I -m PyInstaller --noconfirm --clean --onefile --windowed "
        "--paths $trustedBuildSrc --distpath $pyInstallerDist --workpath $pyInstallerWork "
        "--specpath $pyInstallerSpec --name Autosport $trustedGuiEntry"
    )
    data_build = (
        "& $pythonExecutable -I -m PyInstaller --noconfirm --clean --onefile --console "
        "--paths $trustedBuildSrc --distpath $pyInstallerDist --workpath $pyInstallerWork "
        "--specpath $pyInstallerSpec --name Autosport-Data $trustedDataEntry"
    )
    gui_bound_source = "$builtAutosportExe = Join-Path $pyInstallerDist 'Autosport.exe'"
    data_bound_source = "$builtDataExe = Join-Path $pyInstallerDist 'Autosport-Data.exe'"

    gate_index = script.index(trusted_gate)
    archive_index = script.index(snapshot_archive)
    expand_index = script.index(snapshot_expand)
    fence_index = script.index(write_fence, expand_index)
    verify_index = script.index(snapshot_verify, fence_index)
    gui_index = script.index(gui_build, verify_index)
    first_post_verify = script.index(snapshot_verify, gui_index)
    second_gate_index = script.index(
        trusted_gate + " --allow-release-outputs",
        first_post_verify,
    )
    second_pre_verify = script.index(snapshot_verify, second_gate_index)
    data_index = script.index(data_build, second_pre_verify)
    second_post_verify = script.index(snapshot_verify, data_index)
    unfence_index = script.index(remove_write_fence, second_post_verify)
    gui_bound_index = script.index(gui_bound_source, gui_index)
    data_bound_index = script.index(data_bound_source, data_index)

    assert (
        gate_index
        < archive_index
        < expand_index
        < fence_index
        < verify_index
        < gui_index
        < gui_bound_index
        < first_post_verify
        < second_gate_index
        < second_pre_verify
        < data_index
        < data_bound_index
        < second_post_verify
        < unfence_index
    )
    assert script.count(snapshot_archive) == 1
    assert script.count(write_fence) == 1
    assert script.count(remove_write_fence) == 1
    assert script.count(snapshot_verify) >= 4
    assert "pip install --no-deps --force-reinstall $trustedBuildRoot" not in script
    assert "$builtAutosportExe = Join-Path $trustedBuildRoot 'dist/Autosport.exe'" not in script
    assert "$builtDataExe = Join-Path $trustedBuildRoot 'dist/Autosport-Data.exe'" not in script
    assert "Push-Location $trustedBuildRoot" not in script

    post_gate = script[gate_index:]
    assert "python -m PyInstaller" not in post_gate
    assert post_gate.count("& $pythonExecutable -I -m PyInstaller") == 2


def test_snapshot_verifier_rejects_added_membership_after_materialization(tmp_path: Path) -> None:
    launcher = _trusted_snapshot_verifier_source()
    root = tmp_path / "trusted-source"
    root.mkdir()
    canonical = root / "canonical.py"
    canonical_bytes = b"VALUE = 'canonical'\n"
    canonical.write_bytes(canonical_bytes)
    manifest = {
        "canonical.py": hashlib.sha256(canonical_bytes).hexdigest(),
    }

    clean = subprocess.run(
        [sys.executable, "-I", "-S", "-c", launcher, str(root)],
        input=json.dumps(manifest, sort_keys=True),
        capture_output=True,
        text=True,
    )
    assert clean.returncode == 0, clean.stderr
    assert "SOURCE_SNAPSHOT=PASS" in clean.stdout

    (root / "post-proof-hook.py").write_text("HOSTILE = True\n", encoding="utf-8")
    changed = subprocess.run(
        [sys.executable, "-I", "-S", "-c", launcher, str(root)],
        input=json.dumps(manifest, sort_keys=True),
        capture_output=True,
        text=True,
    )

    assert changed.returncode != 0
    assert "source snapshot membership mismatch" in changed.stderr
    assert "post-proof-hook.py" in changed.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL enforcement regression")
def test_windows_snapshot_write_fence_blocks_post_proof_replacement_and_addition(
    tmp_path: Path,
) -> None:
    root = tmp_path / "trusted-source"
    root.mkdir()
    canonical = root / "canonical.py"
    canonical.write_text("VALUE = 'canonical'\n", encoding="utf-8")

    sid_result = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    sid = sid_result.stdout.strip()
    assert sid.startswith("S-")
    principal = f"*{sid}"

    subprocess.run(
        [
            "icacls",
            str(root),
            "/deny",
            f"{principal}:(OI)(CI)(W,D,DC)",
            "/T",
            "/C",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        with pytest.raises(PermissionError):
            canonical.write_text("VALUE = 'hostile replacement'\n", encoding="utf-8")
        with pytest.raises(PermissionError):
            (root / "post-proof-hook.py").write_text("HOSTILE = True\n", encoding="utf-8")
        replacement = tmp_path / "replacement.py"
        replacement.write_text("VALUE = 'replacement'\n", encoding="utf-8")
        with pytest.raises(PermissionError):
            os.replace(replacement, canonical)
    finally:
        subprocess.run(
            ["icacls", str(root), "/remove:d", principal, "/T", "/C"],
            check=True,
            capture_output=True,
            text=True,
        )


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

    # Bind the oracle to archive materialization of the exact commit itself.
    # Archive EOL materialization may differ by platform/attributes, so a literal
    # LF byte string is not a portable oracle for the Windows release path.
    baseline_archive = tmp_path / "baseline-trusted-build-source.zip"
    subprocess.run(
        ["git", "archive", "--format=zip", f"--output={baseline_archive}", source_sha],
        cwd=repo,
        check=True,
    )
    with zipfile.ZipFile(baseline_archive) as handle:
        canonical_gui = handle.read("src/autosport/windows_entry.py")
        canonical_data = handle.read("src/autosport/data_tools_entry.py")

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

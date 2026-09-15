from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_BUILD_SCRIPT = _ROOT / "scripts" / "build_windows.ps1"
_GUARDED_PYINSTALLER = _ROOT / "scripts" / "guarded_pyinstaller_bind.py"
_SOURCE_VERIFIER = _ROOT / "scripts" / "verify_source_checkout.py"


def _trusted_snapshot_verifier_source() -> str:
    script = _BUILD_SCRIPT.read_text(encoding="utf-8")
    start_marker = "$trustedSourceSnapshotVerifierLauncher = @'\n"
    end_marker = "\n'@\n"
    assert start_marker in script
    tail = script.split(start_marker, 1)[1]
    assert end_marker in tail
    return tail.split(end_marker, 1)[0]


def _run_real_pyinstaller_probe(
    tmp_path: Path,
    *,
    write_before_final_fence: bool = False,
    replace_before_final_fence: bool = False,
    replace_after_final_fence: bool = False,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    app = tmp_path / "probe.py"
    app.write_text("print('probe')\n", encoding="utf-8")
    dist = tmp_path / "dist"
    work = tmp_path / "work"
    spec = tmp_path / "spec"
    bound = tmp_path / "bound" / "Probe.exe"
    digest = tmp_path / "bound" / "Probe.sha256"
    artifact = dist / "Probe.exe"
    verifier_sha256 = hashlib.sha256(_SOURCE_VERIFIER.read_bytes()).hexdigest()

    env = os.environ.copy()
    if write_before_final_fence:
        env["AUTOSPORT_TEST_WRITE_BEFORE_FINAL_FENCE"] = "1"
    if replace_before_final_fence:
        env["AUTOSPORT_TEST_REPLACE_BEFORE_FINAL_FENCE"] = "1"
    if replace_after_final_fence:
        env["AUTOSPORT_TEST_REPLACE_PYINSTALLER_OUTPUT"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-I",
            str(_GUARDED_PYINSTALLER),
            "--artifact",
            str(artifact),
            "--bound-output",
            str(bound),
            "--digest-output",
            str(digest),
            "--verifier",
            str(_SOURCE_VERIFIER),
            "--verifier-sha256",
            verifier_sha256,
            "--",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--console",
            "--distpath",
            str(dist),
            "--workpath",
            str(work),
            "--specpath",
            str(spec),
            "--name",
            "Probe",
            str(app),
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    return completed, artifact, bound, digest


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point regression")
def test_trusted_snapshot_verifier_rejects_directory_junction(tmp_path: Path) -> None:
    launcher = _trusted_snapshot_verifier_source()
    root = tmp_path / "trusted-source"
    target = tmp_path / "junction-target"
    root.mkdir()
    target.mkdir()
    payload = b"VALUE = 'outside-snapshot'\n"
    (target / "module.py").write_bytes(payload)
    junction = root / "pkg"

    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
    )
    assert created.returncode == 0, (created.stdout, created.stderr)

    manifest = {"pkg/module.py": hashlib.sha256(payload).hexdigest()}
    verified = subprocess.run(
        [sys.executable, "-I", "-S", "-c", launcher, str(root)],
        input=json.dumps(manifest, sort_keys=True),
        capture_output=True,
        text=True,
    )

    assert verified.returncode != 0
    assert "reparse point" in verified.stderr.lower()


@pytest.mark.skipif(
    os.name != "nt" or importlib.util.find_spec("PyInstaller") is None,
    reason="real Windows PyInstaller regression",
)
def test_real_pyinstaller_normal_handoff_binds_final_output(tmp_path: Path) -> None:
    completed, artifact, bound, digest = _run_real_pyinstaller_probe(tmp_path)

    assert completed.returncode == 0, completed.stdout + "\n" + completed.stderr
    assert artifact.is_file()
    assert bound.is_file()
    assert digest.is_file()
    digest_text = digest.read_text(encoding="utf-8").strip()
    assert len(digest_text) == 64
    assert all(character in "0123456789abcdef" for character in digest_text)
    assert hashlib.sha256(bound.read_bytes()).hexdigest() == digest_text


@pytest.mark.skipif(
    os.name != "nt" or importlib.util.find_spec("PyInstaller") is None,
    reason="real Windows PyInstaller regression",
)
def test_real_pyinstaller_same_object_write_fails_before_trusted_bind(tmp_path: Path) -> None:
    completed, _artifact, bound, digest = _run_real_pyinstaller_probe(
        tmp_path,
        write_before_final_fence=True,
    )

    assert completed.returncode != 0
    combined = completed.stdout + "\n" + completed.stderr
    assert "same-object write blocked by retained producer artifact fence" in combined
    assert not bound.exists()
    assert not digest.exists()


@pytest.mark.skipif(
    os.name != "nt" or importlib.util.find_spec("PyInstaller") is None,
    reason="real Windows PyInstaller regression",
)
def test_real_pyinstaller_same_path_replacement_fails_before_final_fence(tmp_path: Path) -> None:
    completed, _artifact, bound, digest = _run_real_pyinstaller_probe(
        tmp_path,
        replace_before_final_fence=True,
    )

    assert completed.returncode != 0
    combined = completed.stdout + "\n" + completed.stderr
    assert "replacement blocked by producer continuity anchor" in combined
    assert not bound.exists()
    assert not digest.exists()


@pytest.mark.skipif(
    os.name != "nt" or importlib.util.find_spec("PyInstaller") is None,
    reason="real Windows PyInstaller regression",
)
def test_real_pyinstaller_same_path_replacement_fails_after_final_fence(tmp_path: Path) -> None:
    completed, _artifact, bound, digest = _run_real_pyinstaller_probe(
        tmp_path,
        replace_after_final_fence=True,
    )

    assert completed.returncode != 0
    combined = completed.stdout + "\n" + completed.stderr
    assert "replacement blocked by retained final artifact fence" in combined
    assert not bound.exists()
    assert not digest.exists()


def test_windows_build_invokes_guarded_pyinstaller_for_both_release_executables() -> None:
    script = _BUILD_SCRIPT.read_text(encoding="utf-8")
    guarded_calls = script.count("scripts/guarded_pyinstaller_bind.py")
    assert guarded_calls >= 1
    assert script.count("--verifier-sha256 $sourceVerifierSha256") == 2
    assert script.count("--bound-output $boundAutosportExe") == 1
    assert script.count("--bound-output $boundDataExe") == 1

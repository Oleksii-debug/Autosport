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


def _load_guarded_pyinstaller_module():
    spec = importlib.util.spec_from_file_location(
        "autosport_guarded_pyinstaller_bind_test",
        _GUARDED_PYINSTALLER,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


@pytest.mark.skipif(os.name != "nt", reason="Windows producer-handle regression")
def test_initial_artifact_copy_blocks_replacement_before_identity_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guarded = _load_guarded_pyinstaller_module()
    source = tmp_path / "bootloader.exe"
    artifact = tmp_path / "Probe.exe"
    payload = b"canonical bootloader bytes\n"
    source.write_bytes(payload)

    identity = guarded._copy_artifact_and_capture_identity(source, artifact)
    assert identity == guarded._object_identity(artifact.stat())
    assert artifact.read_bytes() == payload

    monkeypatch.setenv("AUTOSPORT_TEST_REPLACE_PYINSTALLER_OUTPUT_BEFORE_IDENTITY", "1")
    with pytest.raises(OSError):
        guarded._copy_artifact_and_capture_identity(source, artifact)

    assert artifact.read_bytes() == payload
    assert not list(tmp_path.glob(".Probe.exe.pre-identity-replacement-*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows producer-handle regression")
def test_initial_artifact_copy_blocks_write_before_identity_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    guarded = _load_guarded_pyinstaller_module()
    source = tmp_path / "bootloader.exe"
    artifact = tmp_path / "Probe.exe"
    payload = b"canonical bootloader bytes\n"
    source.write_bytes(payload)

    monkeypatch.setenv("AUTOSPORT_TEST_WRITE_PYINSTALLER_OUTPUT_BEFORE_IDENTITY", "1")
    with pytest.raises(OSError):
        guarded._copy_artifact_and_capture_identity(source, artifact)

    assert artifact.read_bytes() == payload


@pytest.mark.skipif(
    os.name != "nt" or importlib.util.find_spec("PyInstaller") is None,
    reason="real Windows PyInstaller regression",
)
def test_real_pyinstaller_same_path_replacement_fails_before_bind(tmp_path: Path) -> None:
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

    assert completed.returncode != 0
    combined = completed.stdout + "\n" + completed.stderr
    assert "object identity changed before binding" in combined
    assert not bound.exists()
    assert not digest.exists()


def test_windows_build_invokes_guarded_pyinstaller_for_both_release_executables() -> None:
    script = _BUILD_SCRIPT.read_text(encoding="utf-8")
    guarded_calls = script.count("scripts/guarded_pyinstaller_bind.py")
    assert guarded_calls >= 1
    assert script.count("--verifier-sha256 $sourceVerifierSha256") == 2
    assert script.count("--bound-output $boundAutosportExe") == 1
    assert script.count("--bound-output $boundDataExe") == 1

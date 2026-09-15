from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_BUILD_SCRIPT = _ROOT / "scripts" / "build_windows.ps1"
_PACKAGE_SCRIPT = _ROOT / "scripts" / "package_windows.py"


def _load_package_windows():
    spec = importlib.util.spec_from_file_location("autosport_package_windows_handoff_test", _PACKAGE_SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_windows_build_carries_bound_executable_digests_into_packager() -> None:
    script = _BUILD_SCRIPT.read_text(encoding="utf-8")

    package_command = "python scripts/package_windows.py `"
    exe_argument = "--exe $boundAutosportExe `"
    exe_digest_argument = "--exe-sha256 $autosportExeSha256 `"
    data_argument = "--data-exe $boundDataExe `"
    data_digest_argument = "--data-exe-sha256 $dataExeSha256 `"

    package_index = script.index(package_command)
    assert package_index < script.index(exe_argument, package_index)
    assert script.index(exe_argument, package_index) < script.index(
        exe_digest_argument, package_index
    )
    assert script.index(exe_digest_argument, package_index) < script.index(
        data_argument, package_index
    )
    assert script.index(data_argument, package_index) < script.index(
        data_digest_argument, package_index
    )


def test_packager_rejects_handoff_replacement_after_outer_verify(tmp_path: Path) -> None:
    package_windows = _load_package_windows()
    handoff = tmp_path / "Autosport.exe"
    trusted_bytes = b"MZ-trusted-autosport-executable"
    replacement_bytes = b"MZ-replaced-after-outer-verifier"
    handoff.write_bytes(trusted_bytes)
    expected = _sha256(handoff)

    # This is the PowerShell verifier boundary: the trusted digest matches here.
    assert _sha256(handoff) == expected
    handoff.write_bytes(replacement_bytes)

    snapshot_dir = tmp_path / "private-package-snapshots"
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        package_windows._capture_verified_executable(
            handoff,
            expected,
            snapshot_dir=snapshot_dir,
            snapshot_name="Autosport.exe",
        )

    assert not (snapshot_dir / "Autosport.exe").exists()


def test_packager_consumes_private_snapshot_not_live_handoff(tmp_path: Path) -> None:
    package_windows = _load_package_windows()
    handoff = tmp_path / "Autosport-Data.exe"
    trusted_bytes = b"MZ-trusted-data-executable"
    handoff.write_bytes(trusted_bytes)
    expected = _sha256(handoff)
    snapshot_dir = tmp_path / "private-package-snapshots"

    snapshot = package_windows._capture_verified_executable(
        handoff,
        expected,
        snapshot_dir=snapshot_dir,
        snapshot_name="Autosport-Data.exe",
    )
    handoff.write_bytes(b"MZ-live-handoff-mutated-after-capture")

    assert snapshot != handoff
    assert snapshot.read_bytes() == trusted_bytes
    assert _sha256(snapshot) == expected

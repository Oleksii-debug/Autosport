from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_BUILD_SCRIPT = _ROOT / "scripts" / "build_windows.ps1"
_EXPECTED_PYINSTALLER_VERSION = "6.22.3"


def _write_package(root: Path, *, marker: str) -> Path:
    package = root / "autosport"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "marker.py").write_text(
        f"VALUE = {marker!r}\n",
        encoding="utf-8",
    )
    entry = package / "resolution_probe.py"
    entry.write_text(
        "from autosport.marker import VALUE\n"
        "print(VALUE)\n",
        encoding="utf-8",
    )
    return entry


def test_windows_release_invokes_both_pyinstaller_analyses_from_trusted_snapshot_path() -> None:
    script = _BUILD_SCRIPT.read_text(encoding="utf-8")

    guarded_call = "& $packagingPython -I $trustedPyInstallerBinder `"
    trusted_path = "--paths $trustedBuildSrc `"

    assert script.count(guarded_call) == 2
    assert script.count(trusted_path) == 2
    assert "$trustedGuiEntry" in script
    assert "$trustedDataEntry" in script
    assert "Push-Location $trustedBuildRoot" not in script


@pytest.mark.skipif(
    importlib.util.find_spec("PyInstaller") is None,
    reason="PyInstaller is unavailable in this test environment",
)
def test_pinned_pyinstaller_prefers_trusted_entry_tree_over_hostile_live_checkout(
    tmp_path: Path,
) -> None:
    import PyInstaller

    assert PyInstaller.__version__ == _EXPECTED_PYINSTALLER_VERSION

    live_src = tmp_path / "live-checkout" / "src"
    live_entry = _write_package(live_src, marker="CANONICAL_BEFORE_SNAPSHOT")

    trusted_src = tmp_path / "trusted-snapshot" / "src"
    trusted_entry = _write_package(trusted_src, marker="CANONICAL_BEFORE_SNAPSHOT")

    # Model a live-checkout mutation after the exact trusted snapshot has already
    # been materialized. The trusted entry uses an absolute autosport.* import,
    # exactly the resolution shape used by the release entry modules.
    (live_entry.parent / "marker.py").write_text(
        "VALUE = 'HOSTILE_POST_SNAPSHOT_LIVE_MUTATION'\n",
        encoding="utf-8",
    )

    dist = tmp_path / "dist"
    work = tmp_path / "work"
    spec = tmp_path / "spec"
    harness = tmp_path / "run_pyinstaller_with_hostile_live_path.py"
    harness.write_text(
        "from __future__ import annotations\n"
        "import sys\n"
        "hostile = sys.argv[1]\n"
        "sys.path.insert(0, hostile)\n"
        "from PyInstaller import __version__\n"
        f"assert __version__ == {_EXPECTED_PYINSTALLER_VERSION!r}\n"
        "from PyInstaller.__main__ import run\n"
        "run(sys.argv[2:])\n",
        encoding="utf-8",
    )

    command = [
        sys.executable,
        "-I",
        str(harness),
        str(live_src),
        "--noconfirm",
        "--clean",
        "--onefile",
        "--console",
        "--paths",
        str(trusted_src),
        "--distpath",
        str(dist),
        "--workpath",
        str(work),
        "--specpath",
        str(spec),
        "--name",
        "TrustedResolutionProbe",
        str(trusted_entry),
    ]
    build = subprocess.run(
        command,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert build.returncode == 0, (
        f"PyInstaller build failed\nstdout:\n{build.stdout}\nstderr:\n{build.stderr}"
    )

    suffix = ".exe" if os.name == "nt" else ""
    artifact = dist / f"TrustedResolutionProbe{suffix}"
    assert artifact.is_file()

    probe = subprocess.run(
        [str(artifact)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "CANONICAL_BEFORE_SNAPSHOT"
    assert "HOSTILE_POST_SNAPSHOT_LIVE_MUTATION" not in probe.stdout

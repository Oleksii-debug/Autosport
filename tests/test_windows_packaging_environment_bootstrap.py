from __future__ import annotations

import os
from pathlib import Path
import subprocess
import venv


_ROOT = Path(__file__).resolve().parents[1]
_BUILD_WINDOWS = _ROOT / "scripts" / "build_windows.ps1"


def test_current_release_path_reuses_live_install_interpreter_for_pyinstaller() -> None:
    """Reproduce the source-to-environment provenance gap on the bound parent head.

    This is intentionally a proof-branch reproducer, not the final acceptance test.
    A canonical repair should replace this positive reproduction with the inverse
    invariant: the PyInstaller interpreter/environment must be rebuilt from exact
    source-bound requirements after the late source boundary and must not inherit
    site/startup authority created while the live checkout was executable.
    """

    script = _BUILD_WINDOWS.read_text(encoding="utf-8")
    editable_install = script.index("python -m pip install -e '.[build,test]'")
    editable_uninstall = script.index(
        "python -m pip uninstall --yes autosport-lab", editable_install
    )
    late_gate = script.index(
        "python $sourceVerifier --source-sha $sourceSha --late-build-boundary",
        editable_uninstall,
    )
    gui_binder = script.index(
        "& $pythonExecutable -I $trustedPyInstallerBinder", late_gate
    )
    data_binder = script.index(
        "& $pythonExecutable -I $trustedPyInstallerBinder", gui_binder + 1
    )

    assert editable_install < editable_uninstall < late_gate < gui_binder < data_binder
    assert "$packagingPythonExecutable" not in script[late_gate:data_binder]


def test_isolated_mode_still_executes_venv_pth_but_clean_bootstrap_does_not(
    tmp_path: Path,
) -> None:
    """Prove both the live-environment gap and the smallest bootstrap closure.

    A transient hostile PEP-517/backend execution can persist executable ``.pth``
    authority in site-packages independently of tracked checkout bytes. Python
    isolated mode ignores PYTHON* and user-site inputs, but still initializes the
    active environment's site-packages. Bootstrapping a fresh packaging venv through
    ``-I -S -m venv`` prevents the contaminated environment's site startup authority
    from running and the new venv does not inherit it.
    """

    outer_env_dir = tmp_path / "live-build-venv"
    venv.EnvBuilder(with_pip=False, clear=True).create(outer_env_dir)
    if os.name == "nt":
        outer_python = outer_env_dir / "Scripts" / "python.exe"
    else:
        outer_python = outer_env_dir / "bin" / "python"

    site_probe = subprocess.run(
        [
            str(outer_python),
            "-c",
            "import site; print(site.getsitepackages()[0])",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    site_packages = Path(site_probe.stdout.strip())
    assert site_packages.is_dir()

    sentinel = tmp_path / "startup-authority-executed.txt"
    executable_pth = site_packages / "autosport_packaging_poison.pth"
    executable_pth.write_text(
        "import os,pathlib; "
        "target=os.environ.get('AUTOSPORT_TEST_STARTUP_SENTINEL'); "
        "target and pathlib.Path(target).write_text('executed', encoding='utf-8')\n",
        encoding="utf-8",
    )

    child_env = os.environ.copy()
    child_env["AUTOSPORT_TEST_STARTUP_SENTINEL"] = str(sentinel)

    subprocess.run(
        [str(outer_python), "-I", "-c", "print('isolated-with-site')"],
        check=True,
        env=child_env,
        capture_output=True,
        text=True,
    )
    assert sentinel.read_text(encoding="utf-8") == "executed"

    sentinel.unlink()
    subprocess.run(
        [str(outer_python), "-I", "-S", "-c", "print('isolated-without-site')"],
        check=True,
        env=child_env,
        capture_output=True,
        text=True,
    )
    assert not sentinel.exists()

    clean_env_dir = tmp_path / "clean-packaging-venv"
    subprocess.run(
        [
            str(outer_python),
            "-I",
            "-S",
            "-m",
            "venv",
            str(clean_env_dir),
            "--without-pip",
        ],
        check=True,
        env=child_env,
        capture_output=True,
        text=True,
    )
    assert not sentinel.exists()

    if os.name == "nt":
        clean_python = clean_env_dir / "Scripts" / "python.exe"
    else:
        clean_python = clean_env_dir / "bin" / "python"
    subprocess.run(
        [str(clean_python), "-I", "-c", "print('clean-packaging-env')"],
        check=True,
        env=child_env,
        capture_output=True,
        text=True,
    )
    assert not sentinel.exists()

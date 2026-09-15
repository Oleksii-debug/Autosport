from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[1]
_BUILD_SCRIPT = _ROOT / "scripts" / "build_windows.ps1"


def _write_editable_project(root: Path) -> Path:
    package = root / "src" / "autosport"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "marker.py").write_text(
        "VALUE = 'CANONICAL_BEFORE_SNAPSHOT'\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text(
        textwrap.dedent(
            """
            [build-system]
            requires = ["setuptools>=68"]
            build-backend = "setuptools.build_meta"

            [project]
            name = "autosport-resolution-probe"
            version = "0.0.0"

            [tool.setuptools.packages.find]
            where = ["src"]
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    return package


def test_windows_release_removes_editable_autosport_before_trusted_pyinstaller() -> None:
    script = _BUILD_SCRIPT.read_text(encoding="utf-8")

    editable_install = script.index("python -m pip install -e '.[build,test]'")
    full_pytest = script.index("python -m pytest -v tests", editable_install)
    demo_smoke = script.index("python -m autosport dataset examples/tt_demo", full_pytest)
    editable_uninstall = script.index(
        "python -m pip uninstall --yes autosport-lab",
        demo_smoke,
    )
    late_gate = script.index(
        "python $sourceVerifier --source-sha $sourceSha --late-build-boundary",
        editable_uninstall,
    )
    isolated_probe = script.index(
        "& $pythonExecutable -I -c $autosportResolutionProbe",
        late_gate,
    )
    trusted_snapshot = script.index(
        "$trustedBuildManifestLines = @(& $pythonExecutable -I -S -c $trustedGitSourceOracleLauncher",
        isolated_probe,
    )
    first_pyinstaller = script.index(
        "& $packagingPython -I $trustedPyInstallerBinder `",
        trusted_snapshot,
    )

    assert (
        editable_install
        < full_pytest
        < demo_smoke
        < editable_uninstall
        < late_gate
        < isolated_probe
        < trusted_snapshot
        < first_pyinstaller
    )
    assert script.count("python -m pip uninstall --yes autosport-lab") == 1
    assert 'find_spec("autosport")' in script
    assert "installed autosport remains import-resolvable" in script


@pytest.mark.skipif(
    importlib.util.find_spec("setuptools") is None,
    reason="setuptools is unavailable in this test environment",
)
def test_real_editable_install_exposes_post_snapshot_live_mutation_until_mapping_removed(
    tmp_path: Path,
) -> None:
    live_project = tmp_path / "live-project"
    live_package = _write_editable_project(live_project)
    editable_site = tmp_path / "editable-site"

    install = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-deps",
            "--no-build-isolation",
            "--target",
            str(editable_site),
            "-e",
            str(live_project),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert install.returncode == 0, (
        f"editable probe install failed\nstdout:\n{install.stdout}\nstderr:\n{install.stderr}"
    )

    editable_pth = tuple(editable_site.glob("*.pth"))
    assert editable_pth, "editable install did not publish a site .pth mapping"
    assert any(str(live_project) in path.read_text(encoding="utf-8") for path in editable_pth)

    # The trusted snapshot would already exist at this point in the release race.
    # Mutating the live source must therefore be observable through the actual
    # editable mapping until that mapping is removed from interpreter authority.
    (live_package / "marker.py").write_text(
        "VALUE = 'HOSTILE_POST_SNAPSHOT_LIVE_MUTATION'\n",
        encoding="utf-8",
    )

    editable_probe = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            (
                "import site,sys; "
                "site.addsitedir(sys.argv[1]); "
                "from autosport.marker import VALUE; "
                "print(VALUE)"
            ),
            str(editable_site),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert editable_probe.returncode == 0, editable_probe.stderr
    assert editable_probe.stdout.strip() == "HOSTILE_POST_SNAPSHOT_LIVE_MUTATION"

    shutil.rmtree(editable_site)
    neutralized_probe = subprocess.run(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            (
                "import importlib.util; "
                "raise SystemExit(0 if importlib.util.find_spec('autosport') is None else 9)"
            ),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert neutralized_probe.returncode == 0, (
        "isolated interpreter still resolved autosport after editable mapping removal\n"
        f"stdout:\n{neutralized_probe.stdout}\nstderr:\n{neutralized_probe.stderr}"
    )

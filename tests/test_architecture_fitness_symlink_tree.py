from __future__ import annotations

import os
from pathlib import Path

import pytest

from autosport.architecture_fitness import ArchitectureFitnessError, analyze_package


def _symlink_or_skip(link: Path, target: Path, *, directory: bool) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink creation unavailable on this runner: {type(exc).__name__}")


def test_nested_symlink_directory_cannot_hide_python_modules(tmp_path: Path) -> None:
    package = tmp_path / "autosport_probe"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "hidden.py").write_text("VALUE = 1\n", encoding="utf-8")
    _symlink_or_skip(package / "linked", outside, directory=True)

    # pathlib intentionally does not recurse through this directory symlink.  The
    # analyzer must reject the tree rather than silently reporting an inventory
    # that omits outside/hidden.py.
    assert [path.name for path in package.rglob("*.py")] == ["__init__.py"]
    with pytest.raises(ArchitectureFitnessError, match="symlinked package entry"):
        analyze_package(package)


def test_symlinked_package_root_is_not_accepted_as_canonical(tmp_path: Path) -> None:
    real_package = tmp_path / "real_package"
    real_package.mkdir()
    (real_package / "__init__.py").write_text("", encoding="utf-8")

    linked_package = tmp_path / "linked_package"
    _symlink_or_skip(linked_package, real_package, directory=True)

    assert os.path.isdir(linked_package)
    with pytest.raises(ArchitectureFitnessError, match="package_dir symlink"):
        analyze_package(linked_package, package_name="autosport_probe")

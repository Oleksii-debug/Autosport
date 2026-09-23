from __future__ import annotations

import json
from pathlib import Path

import pytest

from autosport.architecture_fitness import (
    ArchitectureFitnessError,
    analyze_package,
)


def _write(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def test_reports_cycles_size_order_and_duplicate_authority(tmp_path: Path) -> None:
    package = tmp_path / "autosport"
    _write(package, "__init__.py", "from . import a\n")
    _write(
        package,
        "a.py",
        'AUTHORITY_FAMILY = "risk.shared"\nfrom . import b\nVALUE = 1\n',
    )
    _write(
        package,
        "b.py",
        'RISK_AUTHORITY_FAMILY = "risk.shared"\nfrom .sub import c\n',
    )
    _write(package, "sub/__init__.py", "")
    _write(package, "sub/c.py", "from .. import a\n")

    report = analyze_package(package)

    assert report.module_count == 5
    assert report.internal_edge_count == 4
    assert report.dependency_cycles == (("autosport.a", "autosport.b", "autosport.sub.c"),)
    assert report.duplicate_authority_families == (
        ("risk.shared", ("autosport.a", "autosport.b")),
    )
    assert report.modules_by_size[0] == ("autosport.a", 3)
    assert not report.release_authority
    assert not report.execution_authority
    assert not report.readiness_authority
    assert not report.whole_product_complete


def test_report_identity_is_deterministic_across_creation_order(tmp_path: Path) -> None:
    first = tmp_path / "first" / "autosport"
    second = tmp_path / "second" / "autosport"
    files = {
        "__init__.py": "from .alpha import VALUE\n",
        "alpha.py": "import autosport.beta\nVALUE = 1\n",
        "beta.py": "import json\nVALUE = 2\n",
    }
    for relative in ("beta.py", "__init__.py", "alpha.py"):
        _write(first, relative, files[relative])
    for relative in ("alpha.py", "beta.py", "__init__.py"):
        _write(second, relative, files[relative])

    left = analyze_package(first)
    right = analyze_package(second)

    assert left.to_dict() == right.to_dict()
    assert len(left.report_sha256) == 64
    json.dumps(left.to_dict(), allow_nan=False)


def test_external_imports_and_nested_authority_assignment_are_not_minted(tmp_path: Path) -> None:
    package = tmp_path / "autosport"
    _write(package, "__init__.py", "")
    _write(
        package,
        "one.py",
        "import json\nfrom pathlib import Path\ndef f():\n    AUTHORITY_FAMILY = 'nested.not.authority'\n    return Path('.')\n",
    )

    report = analyze_package(package)
    one = next(item for item in report.modules if item.module == "autosport.one")

    assert one.internal_imports == ()
    assert one.authority_families == ()
    assert report.duplicate_authority_families == ()


def test_from_package_symbol_resolves_only_when_it_is_a_real_module(tmp_path: Path) -> None:
    package = tmp_path / "autosport"
    _write(package, "__init__.py", "VALUE = 1\n")
    _write(package, "consumer.py", "from autosport import helper, VALUE\n")
    _write(package, "helper.py", "VALUE = 2\n")

    report = analyze_package(package)
    consumer = next(item for item in report.modules if item.module == "autosport.consumer")

    assert consumer.internal_imports == ("autosport", "autosport.helper")


def test_syntax_error_fails_closed_without_executing_package(tmp_path: Path) -> None:
    package = tmp_path / "autosport"
    _write(package, "__init__.py", "raise RuntimeError('must not execute')\n")
    _write(package, "broken.py", "def nope(:\n")

    with pytest.raises(ArchitectureFitnessError, match="cannot parse autosport.broken"):
        analyze_package(package)


def test_invalid_package_name_and_empty_package_fail_closed(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ArchitectureFitnessError, match="no Python modules"):
        analyze_package(empty, package_name="autosport")
    with pytest.raises(ArchitectureFitnessError, match="canonical Python identifier"):
        analyze_package(empty, package_name="not-valid")

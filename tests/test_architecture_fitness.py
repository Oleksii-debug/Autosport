from __future__ import annotations

import json
from pathlib import Path

import pytest

from autosport.architecture_fitness import (
    ArchitectureFitnessError,
    main,
    measure_architecture_fitness,
)


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _package(tmp_path: Path) -> Path:
    root = tmp_path / "autosport"
    root.mkdir()
    _write(root, "__init__.py", "")
    return root


def test_measurement_finds_internal_cycle_without_importing_modules(tmp_path: Path) -> None:
    root = _package(tmp_path)
    _write(root, "a.py", "from . import b\nraise RuntimeError('must not execute')\n")
    _write(root, "b.py", "from autosport import a\n")
    _write(root, "c.py", "import json\n")

    report = measure_architecture_fitness(root)

    assert report.import_cycles == (("autosport.a", "autosport.b"),)
    assert ("autosport.a", "autosport.b") in report.internal_edges
    assert ("autosport.b", "autosport.a") in report.internal_edges
    assert not any(source == "autosport.c" for source, _ in report.internal_edges)
    assert report.budget_passed is True
    assert report.budget_violations == ()


def test_relative_imports_across_nested_package_are_resolved(tmp_path: Path) -> None:
    root = _package(tmp_path)
    _write(root, "core.py", "VALUE = 1\n")
    _write(root, "sub/__init__.py", "from .. import core\n")
    _write(root, "sub/worker.py", "from ..core import VALUE\nfrom . import helper\n")
    _write(root, "sub/helper.py", "from .worker import object_that_need_not_exist_at_scan_time\n")

    report = measure_architecture_fitness(root)

    assert ("autosport.sub", "autosport.core") in report.internal_edges
    assert ("autosport.sub.worker", "autosport.core") in report.internal_edges
    assert ("autosport.sub.worker", "autosport.sub.helper") in report.internal_edges
    assert ("autosport.sub.helper", "autosport.sub.worker") in report.internal_edges
    assert report.import_cycles == (("autosport.sub.helper", "autosport.sub.worker"),)


def test_module_metrics_are_deterministic_and_use_utf8_byte_size(tmp_path: Path) -> None:
    root = _package(tmp_path)
    content = "LABEL = '\u0423\u043a\u0440\u0430\u0457\u043d\u0430'\nVALUE = 2"
    _write(root, "zeta.py", content)
    _write(root, "alpha.py", "X = 1\n")

    first = measure_architecture_fitness(root)
    second = measure_architecture_fitness(root)

    assert first == second
    assert [item.module for item in first.modules] == [
        "autosport",
        "autosport.alpha",
        "autosport.zeta",
    ]
    zeta = next(item for item in first.modules if item.module == "autosport.zeta")
    assert zeta.byte_size == len(content.encode("utf-8"))
    assert zeta.line_count == 2


def test_explicit_budgets_fail_closed_but_measurement_has_no_implicit_threshold(tmp_path: Path) -> None:
    root = _package(tmp_path)
    _write(root, "a.py", "from . import b\n" + "X = 1\n" * 10)
    _write(root, "b.py", "from . import a\n")

    measured = measure_architecture_fitness(root)
    constrained = measure_architecture_fitness(
        root,
        max_module_bytes=20,
        max_cycle_count=0,
    )

    assert measured.max_module_bytes is None
    assert measured.max_cycle_count is None
    assert measured.budget_passed is True
    assert constrained.budget_passed is False
    assert "autosport.a" in constrained.oversized_modules
    assert constrained.budget_violations == (
        "1 module(s) exceed max_module_bytes=20",
        "1 import cycle(s) exceed max_cycle_count=0",
    )


def test_syntax_error_reports_only_relative_path_and_exception_class(tmp_path: Path) -> None:
    root = _package(tmp_path)
    secret = "Bearer secret-should-never-enter-error"
    _write(root, "broken.py", f"value = ({secret!r}\n")

    with pytest.raises(ArchitectureFitnessError) as caught:
        measure_architecture_fitness(root)

    message = str(caught.value)
    assert message == "cannot parse package source: broken.py: SyntaxError"
    assert secret not in message
    assert str(root) not in message


def test_rejects_invalid_root_package_name_and_budgets(tmp_path: Path) -> None:
    root = _package(tmp_path)

    with pytest.raises(ArchitectureFitnessError, match="existing directory"):
        measure_architecture_fitness(tmp_path / "missing")
    with pytest.raises(ArchitectureFitnessError, match="dotted Python identifier"):
        measure_architecture_fitness(root, package_name="autosport-bad")
    with pytest.raises(ArchitectureFitnessError, match="max_module_bytes must be >= 1"):
        measure_architecture_fitness(root, max_module_bytes=0)
    with pytest.raises(ArchitectureFitnessError, match="max_cycle_count must be >= 0"):
        measure_architecture_fitness(root, max_cycle_count=-1)


def test_cli_outputs_machine_readable_report_and_distinct_budget_exit_code(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _package(tmp_path)
    _write(root, "a.py", "from . import b\n")
    _write(root, "b.py", "from . import a\n")

    rc = main(
        [
            "--package-root",
            str(root),
            "--package-name",
            "autosport",
            "--max-cycle-count",
            "0",
        ]
    )

    assert rc == 3
    output = json.loads(capsys.readouterr().out)
    assert output["schema"] == "autosport.architecture_fitness"
    assert output["schema_version"] == 1
    assert output["budget_passed"] is False
    assert output["import_cycles"] == [["autosport.a", "autosport.b"]]


def test_cli_structural_error_is_json_and_has_no_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["--package-root", str(tmp_path / "missing")])

    assert rc == 2
    output = json.loads(capsys.readouterr().out)
    assert output == {"error": "package_root must be an existing directory"}

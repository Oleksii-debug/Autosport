from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "architecture_fitness.py"
SPEC = importlib.util.spec_from_file_location("architecture_fitness", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = module
SPEC.loader.exec_module(module)

ArchitectureFitnessError = module.ArchitectureFitnessError
analyze_package = module.analyze_package
main = module.main


def _write(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_detects_relative_and_absolute_package_cycle_deterministically(tmp_path):
    root = tmp_path / "autosport"
    _write(root, "__init__.py", "")
    _write(root, "a.py", "from . import b\n")
    _write(root, "b.py", "from autosport.c import value\n")
    _write(root, "c.py", "from .a import thing\nvalue = 1\n")

    report = analyze_package(root, max_source_lines=50)

    assert report.cycles == (("autosport.a", "autosport.b", "autosport.c"),)
    assert report.edges == (
        ("autosport.a", "autosport.b"),
        ("autosport.b", "autosport.c"),
        ("autosport.c", "autosport.a"),
    )
    assert report.healthy is False


def test_from_package_import_submodule_is_counted_as_local_dependency(tmp_path):
    root = tmp_path / "autosport"
    _write(root, "__init__.py", "")
    _write(root, "a.py", "from autosport import b\n")
    _write(root, "b.py", "VALUE = 1\n")

    report = analyze_package(root)

    assert ("autosport.a", "autosport") in report.edges
    assert ("autosport.a", "autosport.b") in report.edges


def test_external_imports_are_not_misreported_as_local_dependencies(tmp_path):
    root = tmp_path / "autosport"
    _write(root, "__init__.py", "")
    _write(root, "a.py", "import json\nfrom pathlib import Path\nvalue = 1\n")

    report = analyze_package(root)

    assert report.edges == ()
    assert report.cycles == ()
    assert report.healthy is True


def test_line_metrics_ignore_blank_and_comment_only_lines_for_source_threshold(tmp_path):
    root = tmp_path / "autosport"
    _write(root, "__init__.py", "")
    _write(
        root,
        "large.py",
        "# comment\n\nvalue = 1\n# another\nother = 2\nthird = (\n    value\n    + other\n)\n",
    )

    report = analyze_package(root, max_source_lines=4)
    metric = next(item for item in report.modules if item.module == "autosport.large")

    assert metric.physical_lines == 9
    assert metric.source_lines == 6
    assert report.oversized_modules == (metric,)
    assert report.healthy is False


def test_nested_package_relative_import_is_resolved_without_execution(tmp_path):
    root = tmp_path / "autosport"
    _write(root, "__init__.py", "raise RuntimeError('must not execute')\n")
    _write(root, "feature/__init__.py", "from . import helper\n")
    _write(root, "feature/helper.py", "VALUE = 1\n")

    report = analyze_package(root)

    assert ("autosport.feature", "autosport.feature.helper") in report.edges
    assert report.cycles == ()


def test_parse_failure_is_fail_closed(tmp_path):
    root = tmp_path / "autosport"
    _write(root, "__init__.py", "")
    _write(root, "broken.py", "def nope(:\n")

    with pytest.raises(ArchitectureFitnessError, match="cannot parse"):
        analyze_package(root)


def test_json_output_is_stable_and_truth_bounded(tmp_path, capsys):
    root = tmp_path / "autosport"
    _write(root, "__init__.py", "")
    _write(root, "a.py", "value = 1\n")

    exit_code = main(
        [
            "--package-root",
            str(root),
            "--max-source-lines",
            "20",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["summary"] == {
        "module_count": 2,
        "local_import_edge_count": 0,
        "cycle_count": 0,
        "oversized_module_count": 0,
        "healthy": True,
    }
    assert payload["truth"] == {
        "runtime_modules_executed": False,
        "latency_measured": False,
        "release_readiness_claim": False,
    }


def test_check_mode_fails_only_on_reported_architecture_findings(tmp_path, capsys):
    root = tmp_path / "autosport"
    _write(root, "__init__.py", "")
    _write(root, "a.py", "from . import b\n")
    _write(root, "b.py", "from . import a\n")

    assert main(["--package-root", str(root), "--check"]) == 1
    output = capsys.readouterr().out
    assert "cycles=1" in output
    assert "runtime_modules_executed=false" in output


def test_repository_package_measurement_is_deterministic_when_tree_is_available():
    package_root = Path(__file__).parents[1] / "src" / "autosport"
    if not package_root.is_dir():
        pytest.skip("full repository tree is not present in this local harness")

    first = analyze_package(package_root, max_source_lines=100_000)
    second = analyze_package(package_root, max_source_lines=100_000)

    assert first.to_payload() == second.to_payload()
    assert len(first.modules) > 0
    assert all(metric.path.startswith("autosport/") for metric in first.modules)
    assert first.to_payload()["truth"]["runtime_modules_executed"] is False

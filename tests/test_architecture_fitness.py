from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.architecture_fitness import (
    ArchitectureFitnessError,
    main,
    measure_architecture,
)


def _write(root: Path, relative: str, content: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _sample_tree(root: Path) -> None:
    _write(root, "pkg/__init__.py", "")
    _write(root, "pkg/a.py", "import pkg.b\nVALUE = 1\n")
    _write(root, "pkg/b.py", "from . import a\nVALUE = 2\n")
    _write(root, "pkg/c.py", "import json\nVALUE = 3\n")


def test_report_is_deterministic_across_equivalent_roots(tmp_path: Path) -> None:
    first = tmp_path / "one" / "src"
    second = tmp_path / "two" / "src"

    _sample_tree(first)
    _write(second, "pkg/c.py", "import json\nVALUE = 3\n")
    _write(second, "pkg/b.py", "from . import a\nVALUE = 2\n")
    _write(second, "pkg/__init__.py", "")
    _write(second, "pkg/a.py", "import pkg.b\nVALUE = 1\n")

    first_report = measure_architecture(first, top_n=3)
    second_report = measure_architecture(second, top_n=3)

    assert first_report == second_report
    assert first_report["report_sha256"] == second_report["report_sha256"]


def test_internal_cycle_and_external_import_are_classified(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _sample_tree(root)

    report = measure_architecture(root, top_n=10)

    assert report["dependency_cycles"] == [["pkg.a", "pkg.b"]]
    assert report["summary"]["dependency_cycle_count"] == 1
    assert report["summary"]["internal_import_edge_count"] == 2

    modules = {row["module"]: row for row in report["modules"]}
    assert modules["pkg.a"]["internal_imports"] == ["pkg.b"]
    assert modules["pkg.b"]["internal_imports"] == ["pkg.a"]
    assert modules["pkg.c"]["internal_imports"] == []
    assert modules["pkg.a"]["fan_in"] == 1
    assert modules["pkg.a"]["fan_out"] == 1


def test_report_hash_changes_when_source_content_changes(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _sample_tree(root)

    before = measure_architecture(root)
    _write(root, "pkg/c.py", "import json\nVALUE = 4\n")
    after = measure_architecture(root)

    assert before["source_tree_sha256"] != after["source_tree_sha256"]
    assert before["report_sha256"] != after["report_sha256"]


def test_rankings_are_descriptive_and_carry_no_threshold(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _sample_tree(root)

    report = measure_architecture(root, top_n=2)

    assert len(report["top_modules_by_lines"]) == 2
    assert len(report["top_modules_by_ast_nodes"]) == 2
    assert "threshold" not in report["summary"]
    assert "NO_PASS_FAIL_THRESHOLD" in report["non_claims"]


@pytest.mark.parametrize("top_n", [0, -1, True, 1.5])
def test_invalid_top_n_fails_closed(tmp_path: Path, top_n: object) -> None:
    root = tmp_path / "src"
    _sample_tree(root)

    with pytest.raises(ValueError, match="positive integer"):
        measure_architecture(root, top_n=top_n)  # type: ignore[arg-type]


def test_syntax_error_reports_relative_location(tmp_path: Path) -> None:
    root = tmp_path / "src"
    _write(root, "pkg/broken.py", "def broken(:\n    pass\n")

    with pytest.raises(ArchitectureFitnessError, match=r"pkg/broken\.py:1:"):
        measure_architecture(root)


def test_empty_source_root_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "src"
    root.mkdir()

    with pytest.raises(ArchitectureFitnessError, match="no Python modules"):
        measure_architecture(root)


def test_compact_cli_emits_machine_readable_report(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "src"
    _sample_tree(root)

    assert main([str(root), "--top-n", "2", "--compact"]) == 0
    captured = capsys.readouterr()
    report = json.loads(captured.out)

    assert report["schema_version"] == 1
    assert report["measurement_kind"] == "STATIC_PYTHON_ARCHITECTURE_FITNESS"
    assert report["source_root"] == "src"
    assert len(report["top_fan_in"]) == 2
    assert len(report["top_fan_out"]) == 2
    assert len(report["report_sha256"]) == 64

from __future__ import annotations

from pathlib import Path

import pytest

from autosport.architecture_fitness import (
    ArchitectureFitnessError,
    ArchitectureFitnessPolicy,
    assess_architecture_fitness,
    collect_architecture_fitness,
)


def _write(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")


def _module(snapshot, name: str):
    return next(
        item for item in snapshot.modules if item.module == name
    )


def test_collects_internal_imports_without_importing_project_modules(
    tmp_path: Path,
) -> None:
    root = tmp_path / "autosport"
    _write(root, "__init__.py", "")
    _write(
        root,
        "danger.py",
        "raise RuntimeError('must never execute')\n",
    )
    _write(
        root,
        "consumer.py",
        "import json\n"
        "import autosport.danger\n"
        "from autosport import helper\n",
    )
    _write(root, "helper.py", "VALUE = 1\n")

    snapshot = collect_architecture_fitness(root)

    assert _module(
        snapshot, "autosport.consumer"
    ).internal_imports == (
        "autosport.danger",
        "autosport.helper",
    )
    assert snapshot.dependency_cycles == ()


def test_relative_imports_resolve_and_type_checking_edges_are_excluded(
    tmp_path: Path,
) -> None:
    root = tmp_path / "autosport"
    _write(root, "__init__.py", "")
    _write(root, "shared.py", "VALUE = 1\n")
    _write(root, "typing_only.py", "VALUE = 2\n")
    _write(root, "sub/__init__.py", "")
    _write(root, "sub/helper.py", "VALUE = 3\n")
    _write(
        root,
        "sub/worker.py",
        "from typing import TYPE_CHECKING\n"
        "from . import helper\n"
        "from .. import shared\n"
        "if TYPE_CHECKING:\n"
        "    from .. import typing_only\n",
    )

    snapshot = collect_architecture_fitness(root)

    assert _module(
        snapshot, "autosport.sub.worker"
    ).internal_imports == (
        "autosport.shared",
        "autosport.sub.helper",
    )


def test_cycle_detection_is_deterministic_and_reports_scc(
    tmp_path: Path,
) -> None:
    root = tmp_path / "autosport"
    _write(root, "__init__.py", "")
    _write(root, "c.py", "from . import a\n")
    _write(root, "a.py", "from . import b\n")
    _write(root, "b.py", "from . import c\n")

    first = collect_architecture_fitness(root)
    second = collect_architecture_fitness(root)

    assert tuple(
        cycle.modules for cycle in first.dependency_cycles
    ) == (
        ("autosport.a", "autosport.b", "autosport.c"),
    )
    assert first.canonical_dict() == second.canonical_dict()
    assert first.evidence_sha256 == second.evidence_sha256


def test_policy_has_no_hidden_large_module_threshold(
    tmp_path: Path,
) -> None:
    root = tmp_path / "autosport"
    _write(root, "__init__.py", "")
    _write(
        root,
        "large.py",
        "\n".join(f"v{i} = {i}" for i in range(6)) + "\n",
    )

    snapshot = collect_architecture_fitness(root)
    no_size_limit = assess_architecture_fitness(
        snapshot,
        ArchitectureFitnessPolicy(
            max_module_lines=None,
            forbid_dependency_cycles=False,
        ),
    )
    strict = assess_architecture_fitness(
        snapshot,
        ArchitectureFitnessPolicy(
            max_module_lines=5,
            forbid_dependency_cycles=False,
        ),
    )

    assert no_size_limit.compliant is True
    assert no_size_limit.oversized_modules == ()
    assert strict.compliant is False
    assert strict.oversized_modules == ("autosport.large",)
    assert strict.snapshot_sha256 == snapshot.evidence_sha256


def test_cycle_policy_is_explicit_not_implicit(
    tmp_path: Path,
) -> None:
    root = tmp_path / "autosport"
    _write(root, "__init__.py", "")
    _write(root, "a.py", "from . import b\n")
    _write(root, "b.py", "from . import a\n")
    snapshot = collect_architecture_fitness(root)

    allowed = assess_architecture_fitness(
        snapshot,
        ArchitectureFitnessPolicy(
            forbid_dependency_cycles=False
        ),
    )
    forbidden = assess_architecture_fitness(
        snapshot,
        ArchitectureFitnessPolicy(
            forbid_dependency_cycles=True
        ),
    )

    assert allowed.dependency_cycle_count == 1
    assert allowed.compliant is True
    assert forbidden.dependency_cycle_count == 1
    assert forbidden.compliant is False
    assert allowed.policy_sha256 != forbidden.policy_sha256


def test_source_change_changes_bound_evidence_hash(
    tmp_path: Path,
) -> None:
    root = tmp_path / "autosport"
    _write(root, "__init__.py", "")
    _write(root, "a.py", "VALUE = 1\n")
    first = collect_architecture_fitness(root)

    _write(root, "a.py", "VALUE = 2\n")
    second = collect_architecture_fitness(root)

    assert _module(
        first, "autosport.a"
    ).source_sha256 != _module(
        second, "autosport.a"
    ).source_sha256
    assert first.evidence_sha256 != second.evidence_sha256


def test_invalid_source_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "autosport"
    _write(root, "__init__.py", "")
    _write(root, "broken.py", "def broken(:\n")

    with pytest.raises(
        ArchitectureFitnessError,
        match="cannot parse module source",
    ):
        collect_architecture_fitness(root)


def test_policy_rejects_bool_as_integer_threshold() -> None:
    with pytest.raises(
        ArchitectureFitnessError,
        match="positive integer",
    ):
        ArchitectureFitnessPolicy(max_module_lines=True)

"""Plan 6 §5Б: dependency extras must not bypass bounded graph capture.

Untrusted build-host distribution metadata can request many distinct extras for
one child distribution without increasing the edge or component count.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from autosport.release_dependency_graph import capture_build_host_dependency_graph


_SOURCE = {
    "source_sha": "a" * 40,
    "declared_runtime_dependencies": ["rootpkg==1.0"],
    "declared_build_dependencies": ["buildpkg==1.0"],
}


def _capture(*, extras: int):
    dependency = "childpkg[" + ",".join(f"feature{n}" for n in range(extras)) + "]>=1.0"
    def distribution(name):
        if name not in {"rootpkg", "buildpkg", "childpkg"}:
            raise AssertionError("unexpected distribution lookup")
        return SimpleNamespace(
            version="1.0",
            metadata={"Name": name, "License-Expression": None},
            requires=[dependency] if name == "rootpkg" else [],
        )
    with patch("autosport.release_dependency_graph.metadata.distribution", side_effect=distribution):
        return capture_build_host_dependency_graph(_SOURCE)


def test_small_extra_graph_remains_reachable_and_honestly_scoped():
    graph = _capture(extras=2)
    assert [part["name"] for part in graph["components"]] == [
        "buildpkg", "childpkg", "rootpkg",
    ]
    assert graph["evidence_class"] == "BUILD_HOST_REQUIREMENT_GRAPH_ONLY"
    assert graph["components"][-1]["requires"] == ["childpkg"]


def test_many_extras_on_one_distribution_fail_closed_without_more_edges():
    with pytest.raises(ValueError, match="distribution extra context limit"):
        _capture(extras=70)


def test_total_extra_context_bound_cannot_be_bypassed_by_few_components():
    with patch("autosport.release_dependency_graph._MAX_TOTAL_EXTRA_CONTEXTS", 4):
        with pytest.raises(ValueError, match="graph extra context limit"):
            _capture(extras=4)


def test_pending_extra_contexts_cannot_grow_without_bound():
    with patch("autosport.release_dependency_graph._MAX_QUEUED_EXTRA_CONTEXTS", 2):
        with pytest.raises(ValueError, match="pending extra context limit"):
            _capture(extras=6)

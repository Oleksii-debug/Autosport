"""Plan 6 §6Б: a signed update cannot carry contradictory source-bound dependency evidence."""
from __future__ import annotations

from copy import deepcopy

import pytest

from autosport.release_package import _require_dependency_evidence_coherence


def _evidence():
    direct = {"distributions": [
        {"name": "apscheduler", "version": "3.11.3", "roles": ["runtime"],
         "license_expression": None, "license_evidence": "NOT_PROVIDED"},
    ]}
    graph = {"components": [
        {"name": "apscheduler", "version": "3.11.3", "direct_roles": ["runtime"],
         "license_expression": None, "license_evidence": "NOT_PROVIDED"},
    ]}
    return direct, graph


def test_matching_evidence_is_admitted():
    direct, graph = _evidence()
    _require_dependency_evidence_coherence(direct, graph)


@pytest.mark.parametrize("field,value", [
    ("version", "9.9.9"),
    ("direct_roles", ["build"]),
    ("license_expression", "MIT"),
    ("license_evidence", "UNVERIFIED"),
    ("name", "other"),
])
def test_conflicting_graph_is_rejected(field, value):
    direct, graph = _evidence()
    graph = deepcopy(graph)
    graph["components"][0][field] = value
    with pytest.raises(ValueError, match="build-host evidence disagree"):
        _require_dependency_evidence_coherence(direct, graph)

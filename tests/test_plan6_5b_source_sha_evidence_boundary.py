"""Plan 6 5Б: build-host evidence must never bind to an absent Git source SHA.

A direct/graph pair sharing None is not proof of an immutable source commit.
Both validators reuse the same canonical direct-pin normalization boundary.
"""
from copy import deepcopy
from unittest.mock import patch

import pytest

from autosport.release_sbom import (
    capture_direct_installed_dependencies, validate_direct_dependency_metadata,
)
from autosport.release_dependency_graph import (
    capture_build_host_dependency_graph, validate_build_host_dependency_graph,
)


SHA = "a" * 40
SOURCE = {
    "source_sha": SHA,
    "declared_runtime_dependencies": ["APScheduler==3.11.3"],
    "declared_build_dependencies": ["pyinstaller==6.22.3"],
}
DIRECT = {
    "schema_version": 1,
    "source_sha": SHA,
    "evidence_class": "BUILD_HOST_DIRECT_DEPENDENCIES_ONLY",
    "distributions": [
        {"name": "apscheduler", "version": "3.11.3", "roles": ["runtime"],
         "license_expression": None, "license_evidence": "NOT_PROVIDED"},
        {"name": "pyinstaller", "version": "6.22.3", "roles": ["build"],
         "license_expression": None, "license_evidence": "NOT_PROVIDED"},
    ],
}
GRAPH = {
    "schema_version": 1,
    "source_sha": SHA,
    "evidence_class": "BUILD_HOST_REQUIREMENT_GRAPH_ONLY",
    "components": [
        {"name": entry["name"], "version": entry["version"],
         "direct_roles": entry["roles"], "requires": [],
         "license_expression": None, "license_evidence": "NOT_PROVIDED"}
        for entry in DIRECT["distributions"]
    ],
}


def test_valid_git_sha_evidence_unchanged():
    validate_direct_dependency_metadata(deepcopy(DIRECT), deepcopy(SOURCE))
    validate_build_host_dependency_graph(deepcopy(GRAPH), deepcopy(SOURCE))


@pytest.mark.parametrize("bad_sha", [
    None, "", "A" * 40, "a" * 39, "a" * 41, "g" * 40, True, 10,
])
def test_missing_or_malformed_source_sha_rejected_in_both_evidence_classes(bad_sha):
    source = deepcopy(SOURCE)
    source["source_sha"] = bad_sha
    direct = deepcopy(DIRECT)
    direct["source_sha"] = bad_sha
    graph = deepcopy(GRAPH)
    graph["source_sha"] = bad_sha
    with pytest.raises(ValueError, match="canonical Git commit SHA"):
        validate_direct_dependency_metadata(direct, source)
    with pytest.raises(ValueError, match="canonical Git commit SHA"):
        validate_build_host_dependency_graph(graph, source)


def test_missing_source_sha_must_not_qualify_against_itself():
    source = deepcopy(SOURCE)
    source.pop("source_sha")
    for payload, validator in (
        (DIRECT, validate_direct_dependency_metadata),
        (GRAPH, validate_build_host_dependency_graph),
    ):
        evidence = deepcopy(payload)
        evidence["source_sha"] = None
        with pytest.raises(ValueError, match="canonical Git commit SHA"):
            validator(evidence, source)


def test_missing_source_sha_fails_before_any_distribution_probe():
    source = deepcopy(SOURCE)
    source.pop("source_sha")
    with patch("autosport.release_sbom.metadata.distribution") as direct_read, \
         patch("autosport.release_dependency_graph.metadata.distribution") as graph_read:
        with pytest.raises(ValueError, match="canonical Git commit SHA"):
            capture_direct_installed_dependencies(source)
        with pytest.raises(ValueError, match="canonical Git commit SHA"):
            capture_build_host_dependency_graph(source)
        direct_read.assert_not_called()
        graph_read.assert_not_called()

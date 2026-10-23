"""Plan 6 §5Б: bound untrusted host graphs and ownership metadata.

The PyInstaller inventory is the sealed binary fact. Installed distributions
are non-authorizing hints and must never establish license clearance.
"""
from __future__ import annotations

from copy import deepcopy
from unittest.mock import patch

import pytest

from autosport.release_binary_attribution import (
    capture_binary_module_attribution,
    validate_binary_module_attribution,
)


SHA = "a" * 40
EXE = "b" * 64
INVENTORY = {
    "schema_version": 2,
    "source_sha": SHA,
    "executable_sha256": EXE,
    "evidence_class": "EMBEDDED_PYINSTALLER_CARCHIVE_AND_PYZ_TOC_ONLY",
    "components": [{
        "name": "PYZ-00.pyz", "typecode": "z",
        "packed_bytes": 100, "unpacked_bytes": 500,
    }],
    "pyz_modules": [{
        "name": "example.module", "typecode": 0,
        "position": 1, "packed_bytes": 25,
    }],
}
GRAPH = {
    "source_sha": SHA,
    "components": [{"name": "example-dist"}],
}


@pytest.mark.parametrize("mutation", [
    "wrong_source", "missing_source", "nonlist", "empty",
    "nonmapping_component", "nonstring_name", "too_many",
    "duplicate", "unsorted", "noncanonical", "unsafe_name",
])
def test_invalid_host_graph_fails_before_installed_distribution_scan(mutation):
    graph = deepcopy(GRAPH)
    if mutation == "wrong_source":
        graph["source_sha"] = "0" * 40
    elif mutation == "missing_source":
        del graph["source_sha"]
    elif mutation == "nonlist":
        graph["components"] = "example-dist"
    elif mutation == "empty":
        graph["components"] = []
    elif mutation == "nonmapping_component":
        graph["components"] = [None]
    elif mutation == "nonstring_name":
        graph["components"] = [{"name": ["example-dist"]}]
    elif mutation == "too_many":
        graph["components"] = [{"name": f"dist-{i:03d}"} for i in range(257)]
    elif mutation == "duplicate":
        graph["components"] *= 2
    elif mutation == "unsorted":
        graph["components"] = [{"name": "zeta"}, {"name": "alpha"}]
    elif mutation == "noncanonical":
        graph["components"] = [{"name": "Example_Dist"}]
    elif mutation == "unsafe_name":
        graph["components"] = [{"name": "../../secrets"}]
    with patch(
        "autosport.release_binary_attribution.metadata.packages_distributions",
        side_effect=AssertionError("host scan must not execute"),
    ) as scan:
        with pytest.raises(ValueError):
            capture_binary_module_attribution(graph, deepcopy(INVENTORY))
        scan.assert_not_called()


def test_valid_canonical_graph_still_resolves_review_only_owner_hints():
    with patch(
        "autosport.release_binary_attribution.metadata.packages_distributions",
        return_value={"example": ["Example_Dist"]},
    ) as scan:
        result = capture_binary_module_attribution(deepcopy(GRAPH), deepcopy(INVENTORY))
    scan.assert_called_once_with()
    assert result["modules"] == [{
        "module": "example.module",
        "host_candidates": ["example-dist"],
        "review": "HOST_MATCH_REVIEW_REQUIRED",
    }]


@pytest.mark.parametrize("source_identity", [None, True, "", "0" * 40])
def test_injected_owner_mapping_does_not_bypass_source_identity(source_identity):
    graph = deepcopy(GRAPH)
    graph["source_sha"] = source_identity
    with patch(
        "autosport.release_binary_attribution.metadata.packages_distributions",
        side_effect=AssertionError("injected mapping must avoid host metadata"),
    ) as scan:
        with pytest.raises(ValueError):
            capture_binary_module_attribution(
                graph, deepcopy(INVENTORY),
                packages_distributions={"example": ["Example_Dist"]},
            )
        scan.assert_not_called()


def test_valid_injected_owner_mapping_avoids_host_metadata():
    with patch(
        "autosport.release_binary_attribution.metadata.packages_distributions",
        side_effect=AssertionError("host metadata must not execute"),
    ) as scan:
        result = capture_binary_module_attribution(
            deepcopy(GRAPH), deepcopy(INVENTORY),
            packages_distributions={"example": ["Example_Dist"]},
        )
    scan.assert_not_called()
    assert result["modules"][0]["host_candidates"] == ["example-dist"]
    assert result["modules"][0]["review"] == "HOST_MATCH_REVIEW_REQUIRED"


@pytest.mark.parametrize("name", ["a" * 513, "z" * 4096], ids=["513", "4096"])
def test_oversized_graph_name_fails_before_host_metadata_scan(name):
    graph = deepcopy(GRAPH)
    graph["components"] = [{"name": name}]
    with patch(
        "autosport.release_binary_attribution.metadata.packages_distributions",
        side_effect=AssertionError("host scan must not execute"),
    ) as scan:
        with pytest.raises(ValueError, match="graph|canonical"):
            capture_binary_module_attribution(graph, deepcopy(INVENTORY))
        scan.assert_not_called()


@pytest.mark.parametrize("owner", ["a" * 513, "z" * 4096], ids=["513", "4096"])
def test_oversized_host_owner_hint_fails_closed(owner):
    with patch(
        "autosport.release_binary_attribution.metadata.packages_distributions",
        side_effect=AssertionError("host metadata must not execute"),
    ) as scan:
        with pytest.raises(ValueError, match="ownership|identity"):
            capture_binary_module_attribution(
                deepcopy(GRAPH), deepcopy(INVENTORY),
                packages_distributions={"example": [owner]},
            )
        scan.assert_not_called()


def test_independent_verifier_rejects_oversized_graph_name():
    report = capture_binary_module_attribution(
        deepcopy(GRAPH), deepcopy(INVENTORY), packages_distributions={},
    )
    graph = deepcopy(GRAPH)
    graph["components"] = [{"name": "a" * 513}]
    with pytest.raises(ValueError, match="component identities"):
        validate_binary_module_attribution(report, graph, deepcopy(INVENTORY))


def test_maximum_bounded_graph_name_is_still_accepted():
    graph = deepcopy(GRAPH)
    graph["components"] = [{"name": "a" * 512}]
    report = capture_binary_module_attribution(
        graph, deepcopy(INVENTORY), packages_distributions={},
    )
    assert report["modules"][0]["review"] == "UNKNOWN_OWNER_REVIEW_REQUIRED"
    validate_binary_module_attribution(report, graph, deepcopy(INVENTORY))

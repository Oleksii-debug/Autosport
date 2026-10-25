"""Conservative binary-to-build-host distribution hints for Windows release review.

The sealed PyInstaller PYZ inventory is the binary fact. Host package metadata is
merely a candidate mapping: never an embedded-wheel SBOM or license clearance.
"""
from __future__ import annotations

from importlib import metadata
import platform
import re
import sys
from typing import Any

from .release_sbom import _normalize_name, validate_embedded_executable_inventory

_EVIDENCE_CLASS = "SEALED_PYZ_TO_BUILD_HOST_OWNERSHIP_HINTS_NOT_CLEARANCE"
_COMPLETE_EVIDENCE_CLASS = "SEALED_PYZ_COMPLETE_OWNER_SBOM_NOT_LEGAL_CLEARANCE"


def _validated_graph_names(dependency_graph: dict[str, Any], source_sha: str) -> list[str]:
    if type(dependency_graph) is not dict or type(dependency_graph.get("components")) is not list:
        raise ValueError("build-host graph is required")
    components = dependency_graph["components"]
    if (
        dependency_graph.get("source_sha") != source_sha
        or not 0 < len(components) <= 256
        or any(
            type(component) is not dict or type(component.get("name")) is not str
            or not 1 <= len(component["name"]) <= 512
            for component in components
        )
    ):
        raise ValueError("binary attribution host graph identity is invalid")
    ordered_names = [component["name"] for component in components]
    if (
        ordered_names != sorted(set(ordered_names))
        or any(_normalize_name(name) != name for name in ordered_names)
    ):
        raise ValueError("binary attribution requires canonical host distribution graph")
    return ordered_names


def _candidate_owners(
    root: str, packages_distributions: dict[str, list[str]], graph_names: set[str],
) -> list[str]:
    raw = packages_distributions.get(root, ())
    if type(raw) not in (tuple, list) or len(raw) > 32:
        raise ValueError("unbounded build-host ownership candidates")
    candidates = set()
    for value in raw:
        if type(value) is not str or not 1 <= len(value) <= 512:
            raise ValueError("malformed build-host ownership identity")
        normalized = _normalize_name(value)
        if normalized in graph_names:
            candidates.add(normalized)
    matches = sorted(candidates)
    if len(matches) > 16:
        raise ValueError("ambiguous build-host ownership exceeds bound")
    return matches


def validate_binary_module_attribution(
    report: dict[str, Any], dependency_graph: dict[str, Any],
    inventory: dict[str, Any],
) -> None:
    """Reject missing binary modules, forged clearances and unbound host owners."""
    schema_version = report.get("schema_version") if type(report) is dict else None
    if schema_version == 2:
        _validate_complete_binary_sbom(report, dependency_graph, inventory)
        return
    if (
        type(report) is not dict
        or set(report) != {
            "schema_version", "source_sha", "executable_sha256",
            "evidence_class", "modules",
        }
        or type(report["schema_version"]) is not int or report["schema_version"] != 1
        or type(inventory) is not dict
        or report["source_sha"] != inventory.get("source_sha")
        or report["executable_sha256"] != inventory.get("executable_sha256")
        or report["evidence_class"] != _EVIDENCE_CLASS
        or type(dependency_graph) is not dict
        or report["source_sha"] != dependency_graph.get("source_sha")
        or type(dependency_graph.get("components")) is not list
    ):
        raise ValueError("binary attribution identity/evidence class mismatch")
    validate_embedded_executable_inventory(
        inventory, source_sha=report["source_sha"],
        executable_sha256=report["executable_sha256"],
    )
    if inventory.get("schema_version") != 2:
        raise ValueError("binary attribution requires sealed nested PYZ census")
    components = dependency_graph["components"]
    # Reject hostile, omitted or nonhashable component identities BEFORE
    # running set/sort. Even a source-bound release verifier may parse JSON.
    if not 0 < len(components) <= 256 or any(
        type(component) is not dict or type(component.get("name")) is not str
        or not 1 <= len(component["name"]) <= 512
        for component in components
    ):
        raise ValueError("binary attribution host component identities are invalid")
    names = [component["name"] for component in components]
    if (
        not 0 < len(names) <= 256 or names != sorted(set(names))
        or any(type(name) is not str or _normalize_name(name) != name for name in names)
    ):
        raise ValueError("binary attribution requires canonical host distribution graph")
    entries = report["modules"]
    originals = inventory["pyz_modules"]
    if (
        type(entries) is not list or len(entries) != len(originals)
        or not 1 <= len(entries) <= 20000
    ):
        raise ValueError("binary attribution misses sealed modules")
    for entry, original in zip(entries, originals):
        if type(entry) is not dict or set(entry) != {
            "module", "host_candidates", "review",
        }:
            raise ValueError("binary attribution module schema mismatch")
        if entry["module"] != original["name"]:
            raise ValueError("binary attribution module inventory mismatch")
        candidates = entry["host_candidates"]
        # Reject malformed JSON primitives before using set()/sort(). Otherwise
        # dict/list candidates can raise TypeError instead of a bounded
        # fail-closed validation error at the independent ZIP verifier.
        if (
            type(candidates) is not list or len(candidates) > 16
            # Reject oversized report candidates before hashing or sorting.
            # Capture bounds host names, but the independent ZIP verifier must
            # enforce the same limit on untrusted serialized report content.
            or any(type(name) is not str or not 1 <= len(name) <= 512
                   for name in candidates)
            or candidates != sorted(set(candidates))
            or any(name not in names for name in candidates)
        ):
            raise ValueError("binary attribution host candidates unbounded/untrusted")
        if entry["review"] != (
            "HOST_MATCH_REVIEW_REQUIRED" if candidates
            else "UNKNOWN_OWNER_REVIEW_REQUIRED"
        ):
            raise ValueError("binary attribution may not claim license clearance")


def _validate_complete_binary_sbom(
    report: dict[str, Any], dependency_graph: dict[str, Any],
    inventory: dict[str, Any],
) -> None:
    """Validate exhaustive, single-owner classification of every sealed PYZ module."""
    expected_fields = {
        "schema_version", "source_sha", "executable_sha256", "evidence_class",
        "python_implementation", "python_version", "stdlib_roots", "modules", "summary",
    }
    if (
        set(report) != expected_fields
        or report["evidence_class"] != _COMPLETE_EVIDENCE_CLASS
        or type(report["source_sha"]) is not str
        or report["source_sha"] != inventory.get("source_sha")
        or report["executable_sha256"] != inventory.get("executable_sha256")
        or report["python_implementation"] != "CPython"
        or type(report["python_version"]) is not str
        or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", report["python_version"]) is None
    ):
        raise ValueError("complete binary SBOM identity/evidence class mismatch")
    validate_embedded_executable_inventory(
        inventory, source_sha=report["source_sha"],
        executable_sha256=report["executable_sha256"],
    )
    if inventory.get("schema_version") != 2:
        raise ValueError("complete binary SBOM requires sealed nested PYZ census")
    graph_names = set(_validated_graph_names(dependency_graph, report["source_sha"]))
    stdlib_roots = report["stdlib_roots"]
    if (
        type(stdlib_roots) is not list or not 1 <= len(stdlib_roots) <= 2048
        or any(type(root) is not str or not 1 <= len(root) <= 128 for root in stdlib_roots)
        or stdlib_roots != sorted(set(stdlib_roots))
    ):
        raise ValueError("complete binary SBOM stdlib authority is invalid")
    entries = report["modules"]
    originals = inventory["pyz_modules"]
    if type(entries) is not list or len(entries) != len(originals) or not 1 <= len(entries) <= 20000:
        raise ValueError("complete binary SBOM misses sealed modules")
    counts = {"distribution": 0, "first_party": 0, "python_stdlib": 0}
    for entry, original in zip(entries, originals):
        if type(entry) is not dict or set(entry) != {
            "module", "owner_kind", "owner", "license_evidence",
        } or entry["module"] != original["name"]:
            raise ValueError("complete binary SBOM module schema/inventory mismatch")
        module = entry["module"]
        root = module.split(".", 1)[0]
        kind, owner, license_evidence = (
            entry["owner_kind"], entry["owner"], entry["license_evidence"]
        )
        if kind == "DISTRIBUTION":
            if owner not in graph_names or license_evidence != "DEPENDENCY_LICENSE_EVIDENCE":
                raise ValueError("complete binary SBOM distribution owner is untrusted")
            counts["distribution"] += 1
        elif kind == "FIRST_PARTY":
            if root != "autosport" or owner != "autosport" or license_evidence != "SOURCE_TREE":
                raise ValueError("complete binary SBOM first-party owner is invalid")
            counts["first_party"] += 1
        elif kind == "PYTHON_STDLIB":
            if root not in stdlib_roots or owner != "python" or license_evidence != "PYTHON_RUNTIME_LICENSE":
                raise ValueError("complete binary SBOM stdlib owner is invalid")
            counts["python_stdlib"] += 1
        else:
            raise ValueError("complete binary SBOM contains an unresolved owner")
    summary = report["summary"]
    expected_summary = {
        "total_modules": len(entries),
        "distribution_modules": counts["distribution"],
        "first_party_modules": counts["first_party"],
        "python_stdlib_modules": counts["python_stdlib"],
        "unresolved_modules": 0,
        "ambiguous_modules": 0,
    }
    if type(summary) is not dict or summary != expected_summary:
        raise ValueError("complete binary SBOM summary is not exact")


def capture_complete_binary_sbom(
    dependency_graph: dict[str, Any], inventory: dict[str, Any], *,
    packages_distributions: dict[str, list[str]] | None = None,
    stdlib_module_names: set[str] | frozenset[str] | None = None,
    python_version: str | None = None,
) -> dict[str, Any]:
    """Classify every sealed PYZ module as distribution, first-party or stdlib.

    Capture fails closed on missing or ambiguous owners.  This is a complete
    module SBOM for the sealed PYZ namespace, not a legal-clearance decision.
    """
    if type(inventory) is not dict or inventory.get("schema_version") != 2:
        raise ValueError("sealed nested PYZ inventory required")
    source_sha = inventory.get("source_sha")
    binary_sha = inventory.get("executable_sha256")
    if (
        type(source_sha) is not str or re.fullmatch(r"[0-9a-f]{40}", source_sha) is None
        or type(binary_sha) is not str or re.fullmatch(r"[0-9a-f]{64}", binary_sha) is None
    ):
        raise ValueError("invalid sealed executable identity")
    validate_embedded_executable_inventory(
        inventory, source_sha=source_sha, executable_sha256=binary_sha,
    )
    ordered_names = _validated_graph_names(dependency_graph, source_sha)
    if packages_distributions is None:
        packages_distributions = metadata.packages_distributions()
    if type(packages_distributions) is not dict:
        raise ValueError("build-host module owners are not a mapping")
    if stdlib_module_names is None:
        stdlib_module_names = sys.stdlib_module_names
    if type(stdlib_module_names) not in (set, frozenset) or not stdlib_module_names:
        raise ValueError("CPython stdlib module authority is unavailable")
    stdlib_roots = sorted(stdlib_module_names)
    if any(type(root) is not str or not 1 <= len(root) <= 128 for root in stdlib_roots):
        raise ValueError("CPython stdlib module authority is malformed")
    version = platform.python_version() if python_version is None else python_version
    if type(version) is not str or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None:
        raise ValueError("CPython version is invalid")
    graph_names = set(ordered_names)
    modules = []
    counts = {"DISTRIBUTION": 0, "FIRST_PARTY": 0, "PYTHON_STDLIB": 0}
    for item in inventory["pyz_modules"]:
        module = item["name"]
        root = module.split(".", 1)[0]
        matches = _candidate_owners(root, packages_distributions, graph_names)
        if len(matches) > 1:
            raise ValueError("complete binary SBOM has ambiguous distribution owner")
        if matches:
            kind, owner, evidence = "DISTRIBUTION", matches[0], "DEPENDENCY_LICENSE_EVIDENCE"
        elif root == "autosport":
            kind, owner, evidence = "FIRST_PARTY", "autosport", "SOURCE_TREE"
        elif root in stdlib_module_names:
            kind, owner, evidence = "PYTHON_STDLIB", "python", "PYTHON_RUNTIME_LICENSE"
        else:
            raise ValueError("complete binary SBOM has an unresolved module owner")
        counts[kind] += 1
        modules.append({
            "module": module, "owner_kind": kind, "owner": owner,
            "license_evidence": evidence,
        })
    result = {
        "schema_version": 2,
        "source_sha": source_sha,
        "executable_sha256": binary_sha,
        "evidence_class": _COMPLETE_EVIDENCE_CLASS,
        "python_implementation": "CPython",
        "python_version": version,
        "stdlib_roots": stdlib_roots,
        "modules": modules,
        "summary": {
            "total_modules": len(modules),
            "distribution_modules": counts["DISTRIBUTION"],
            "first_party_modules": counts["FIRST_PARTY"],
            "python_stdlib_modules": counts["PYTHON_STDLIB"],
            "unresolved_modules": 0,
            "ambiguous_modules": 0,
        },
    }
    validate_binary_module_attribution(result, dependency_graph, inventory)
    return result


def capture_binary_module_attribution(
    dependency_graph: dict[str, Any], inventory: dict[str, Any], *,
    packages_distributions: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Map every sealed PYZ module to non-authorizing build-host owner hints."""
    if type(inventory) is not dict or inventory.get("schema_version") != 2:
        raise ValueError("sealed nested PYZ inventory required")
    source_sha = inventory.get("source_sha")
    binary_sha = inventory.get("executable_sha256")
    if (
        type(source_sha) is not str or re.fullmatch(r"[0-9a-f]{40}", source_sha) is None
        or type(binary_sha) is not str or re.fullmatch(r"[0-9a-f]{64}", binary_sha) is None
    ):
        raise ValueError("invalid sealed executable identity")
    validate_embedded_executable_inventory(
        inventory, source_sha=source_sha, executable_sha256=binary_sha,
    )
    # Validate untrusted host graphs before enumerating installed distributions.
    # The independent release verifier also validates this canonical contract.
    ordered_names = _validated_graph_names(dependency_graph, source_sha)
    if packages_distributions is None:
        packages_distributions = metadata.packages_distributions()
    if type(packages_distributions) is not dict:
        raise ValueError("build-host module owners are not a mapping")
    names = set(ordered_names)
    modules: list[dict[str, Any]] = []
    for item in inventory["pyz_modules"]:
        root = item["name"].split(".", 1)[0]
        matches = _candidate_owners(root, packages_distributions, names)
        modules.append({
            "module": item["name"],
            "host_candidates": matches,
            "review": (
                "HOST_MATCH_REVIEW_REQUIRED" if matches
                else "UNKNOWN_OWNER_REVIEW_REQUIRED"
            ),
        })
    result = {
        "schema_version": 1, "source_sha": source_sha,
        "executable_sha256": binary_sha, "evidence_class": _EVIDENCE_CLASS,
        "modules": modules,
    }
    validate_binary_module_attribution(result, dependency_graph, inventory)
    return result

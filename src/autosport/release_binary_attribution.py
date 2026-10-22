"""Conservative binary-to-build-host distribution hints for Windows release review.

The sealed PyInstaller PYZ inventory is the binary fact. Host package metadata is
merely a candidate mapping: never an embedded-wheel SBOM or license clearance.
"""
from __future__ import annotations

from importlib import metadata
import re
from typing import Any

from .release_sbom import _normalize_name, validate_embedded_executable_inventory

_EVIDENCE_CLASS = "SEALED_PYZ_TO_BUILD_HOST_OWNERSHIP_HINTS_NOT_CLEARANCE"


def validate_binary_module_attribution(
    report: dict[str, Any], dependency_graph: dict[str, Any],
    inventory: dict[str, Any],
) -> None:
    """Reject missing binary modules, forged clearances and unbound host owners."""
    if (
        type(report) is not dict
        or set(report) != {
            "schema_version", "source_sha", "executable_sha256",
            "evidence_class", "modules",
        }
        or type(report["schema_version"]) is not int or report["schema_version"] != 1
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
    names = [
        component["name"]
        for component in components
        if type(component) is dict and "name" in component
    ]
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
            or any(type(name) is not str for name in candidates)
            or candidates != sorted(set(candidates))
            or any(name not in names for name in candidates)
        ):
            raise ValueError("binary attribution host candidates unbounded/untrusted")
        if entry["review"] != (
            "HOST_MATCH_REVIEW_REQUIRED" if candidates
            else "UNKNOWN_OWNER_REVIEW_REQUIRED"
        ):
            raise ValueError("binary attribution may not claim license clearance")


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
    if type(dependency_graph) is not dict or type(dependency_graph.get("components")) is not list:
        raise ValueError("build-host graph is required")
    if packages_distributions is None:
        packages_distributions = metadata.packages_distributions()
    if type(packages_distributions) is not dict:
        raise ValueError("build-host module owners are not a mapping")
    names = {
        component["name"] for component in dependency_graph["components"]
        if type(component) is dict and type(component.get("name")) is str
    }
    modules: list[dict[str, Any]] = []
    for item in inventory["pyz_modules"]:
        root = item["name"].split(".", 1)[0]
        raw = packages_distributions.get(root, ())
        if type(raw) not in (tuple, list) or len(raw) > 32:
            raise ValueError("unbounded build-host ownership candidates")
        candidates = set()
        for value in raw:
            if type(value) is not str:
                raise ValueError("malformed build-host ownership identity")
            normalized = _normalize_name(value)
            if normalized in names:
                candidates.add(normalized)
        matches = sorted(candidates)
        if len(matches) > 16:
            raise ValueError("ambiguous build-host ownership exceeds bound")
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

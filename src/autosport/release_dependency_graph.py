"""Bounded requirement closure of the *build host*, never a bundled-program SBOM.

Only sanitized distribution identities, versions, declared dependency edges and
unverified SPDX metadata are exported. Paths, author metadata, tokens and installed
file contents are not exported. This evidence cannot prove which wheels PyInstaller
actually embedded, license clearance, or physical Windows acceptance.
"""
from __future__ import annotations

from collections import deque
from importlib import metadata
from typing import Any

from .release_sbom import _LICENSE, _VERSION, _normalize_name, _requirements

_MAX_COMPONENTS = 256
# Requirement extras can multiply graph walks without creating new edges.
# Bound both unique contexts and pending work, not only distribution names.
_MAX_EXTRA_CONTEXTS_PER_DISTRIBUTION = 64
_MAX_TOTAL_EXTRA_CONTEXTS = 1024
_MAX_QUEUED_EXTRA_CONTEXTS = 2048
_MAX_EDGES = 8192
_MAX_REQUIREMENTS_PER_DISTRIBUTION = 512
_MAX_REQUIREMENT_LENGTH = 2048
_EVIDENCE_CLASS = "BUILD_HOST_REQUIREMENT_GRAPH_ONLY"


def validate_build_host_dependency_graph(
    payload: dict[str, Any], source_manifest: dict[str, Any],
) -> None:
    roots = _requirements(source_manifest)
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "source_sha", "evidence_class", "components"}
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or payload["source_sha"] != source_manifest.get("source_sha")
        or payload["evidence_class"] != _EVIDENCE_CLASS
    ):
        raise ValueError("build-host graph identity or evidence class is invalid")
    components = payload["components"]
    if not isinstance(components, list) or not 0 < len(components) <= _MAX_COMPONENTS:
        raise ValueError("build-host graph component count is invalid")
    by_name: dict[str, dict[str, Any]] = {}
    total_edges = 0
    for item in components:
        if not isinstance(item, dict) or set(item) != {
            "name", "version", "direct_roles", "requires",
            "license_expression", "license_evidence",
        }:
            raise ValueError("build-host graph component schema is invalid")
        name = item["name"]
        if not isinstance(name, str) or _normalize_name(name) != name or name in by_name:
            raise ValueError("build-host graph component name is ambiguous")
        version = item["version"]
        if not isinstance(version, str) or not _VERSION.fullmatch(version):
            raise ValueError("build-host graph component version is invalid")
        roles = item["direct_roles"]
        expected_roles = sorted(roots[name]["roles"]) if name in roots else []
        if roles != expected_roles:
            raise ValueError("build-host graph direct dependency roles differ")
        if name in roots and version != roots[name]["version"]:
            raise ValueError("build-host graph exact source pin differs")
        required = item["requires"]
        if (
            not isinstance(required, list)
            or any(not isinstance(dep, str) or _normalize_name(dep) != dep for dep in required)
            or required != sorted(set(required))
        ):
            raise ValueError("build-host graph edges are invalid or unsorted")
        total_edges += len(required)
        if total_edges > _MAX_EDGES:
            raise ValueError("build-host graph edge limit exceeded")
        expression, evidence = item["license_expression"], item["license_evidence"]
        if expression is None:
            if evidence != "NOT_PROVIDED":
                raise ValueError("build-host graph claims license clearance without evidence")
        elif (
            not isinstance(expression, str)
            or not 0 < len(expression) <= 200
            or not _LICENSE.fullmatch(expression)
            or evidence != "DISTRIBUTION_METADATA_UNVERIFIED"
        ):
            raise ValueError("build-host graph license evidence is invalid")
        by_name[name] = item
    if list(by_name) != sorted(by_name) or not set(roots).issubset(by_name):
        raise ValueError("build-host graph is unordered or misses direct dependencies")
    for item in components:
        if any(dep not in by_name for dep in item["requires"]):
            raise ValueError("build-host graph references absent dependency")
    # An unrelated installed distribution cannot be smuggled into this scope.
    reachable: set[str] = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        pending.extend(by_name[name]["requires"])
    if reachable != set(by_name):
        raise ValueError("build-host graph contains unrelated components")


def capture_build_host_dependency_graph(
    source_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Resolve declared, marker-active requirements on the exact build interpreter.

    This rejects unresolved/mismatched requirements and never infers that a
    build-host distribution is embedded in Autosport.exe.
    """
    # Packaging is required by the Windows build toolchain, not by the
    # product runtime or consumers that only verify stored evidence.
    from packaging.requirements import InvalidRequirement, Requirement

    roots = _requirements(source_manifest)
    queue = deque((name, "") for name in sorted(roots))
    processed: dict[str, set[str]] = {}
    processed_extra_context_count = 0
    components: dict[str, dict[str, Any]] = {}
    edges = 0
    while queue:
        name, extra = queue.popleft()
        if extra in processed.get(name, set()):
            continue
        contexts = processed.setdefault(name, set())
        if len(contexts) >= _MAX_EXTRA_CONTEXTS_PER_DISTRIBUTION:
            raise ValueError("build-host distribution extra context limit exceeded")
        contexts.add(extra)
        processed_extra_context_count += 1
        if processed_extra_context_count > _MAX_TOTAL_EXTRA_CONTEXTS:
            raise ValueError("build-host graph extra context limit exceeded")
        if len(processed) > _MAX_COMPONENTS:
            raise ValueError("build-host graph component limit exceeded")
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError as exc:
            raise ValueError("build-host dependency is missing") from exc
        actual_name = distribution.metadata.get("Name")
        actual_version = distribution.version
        if (
            not isinstance(actual_name, str)
            or _normalize_name(actual_name) != name
            or not isinstance(actual_version, str)
            or not _VERSION.fullmatch(actual_version)
            or (name in roots and actual_version != roots[name]["version"])
        ):
            raise ValueError("build-host distribution does not match declared identity")
        if name not in components:
            expression = distribution.metadata.get("License-Expression")
            if not (
                isinstance(expression, str)
                and 0 < len(expression) <= 200
                and _LICENSE.fullmatch(expression)
            ):
                expression = None
            components[name] = {
                "name": name,
                "version": actual_version,
                "direct_roles": sorted(roots[name]["roles"]) if name in roots else [],
                "requires": [],
                "license_expression": expression,
                "license_evidence": (
                    "DISTRIBUTION_METADATA_UNVERIFIED" if expression is not None
                    else "NOT_PROVIDED"
                ),
            }
        elif components[name]["version"] != actual_version:
            raise ValueError("build-host distribution changed during graph capture")
        requirements = distribution.requires or ()
        if len(requirements) > _MAX_REQUIREMENTS_PER_DISTRIBUTION:
            raise ValueError("build-host distribution requirement count is unbounded")
        for raw in requirements:
            if not isinstance(raw, str) or len(raw) > _MAX_REQUIREMENT_LENGTH:
                raise ValueError("build-host requirement text is invalid")
            try:
                requirement = Requirement(raw)
                enabled = (
                    requirement.marker is None
                    or requirement.marker.evaluate({"extra": extra})
                )
            except (InvalidRequirement, ValueError) as exc:
                raise ValueError("build-host requirement cannot be interpreted") from exc
            if not enabled:
                continue
            if requirement.url:
                raise ValueError("direct URL build-host requirement cannot be qualified")
            child = _normalize_name(requirement.name)
            try:
                child_dist = metadata.distribution(child)
            except metadata.PackageNotFoundError as exc:
                raise ValueError("build-host transitive requirement is missing") from exc
            if not requirement.specifier.contains(child_dist.version, prereleases=True):
                raise ValueError("build-host transitive version violates requirement")
            if child not in components[name]["requires"]:
                components[name]["requires"].append(child)
                edges += 1
                if edges > _MAX_EDGES:
                    raise ValueError("build-host graph edge limit exceeded")
            # Requirements without an extra marker apply in both the base
            # install and every selected extra; always include the base pass.
            for selected_extra in ["", *sorted(requirement.extras)]:
                if len(queue) >= _MAX_QUEUED_EXTRA_CONTEXTS:
                    raise ValueError("build-host graph pending extra context limit exceeded")
                queue.append((child, selected_extra))
    result = {
        "schema_version": 1,
        "source_sha": source_manifest.get("source_sha"),
        "evidence_class": _EVIDENCE_CLASS,
        "components": [
            {**components[name], "requires": sorted(components[name]["requires"])}
            for name in sorted(components)
        ],
    }
    validate_build_host_dependency_graph(result, source_manifest)
    return result

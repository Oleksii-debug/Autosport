"""Build-host dependency metadata for a source-bound Windows candidate.

This is *not* a complete transitive SBOM, license clearance, or proof of
components bundled in the resulting executable. It records the exact direct
distributions observed by the packaging Python interpreter.
"""
from __future__ import annotations

from importlib import metadata
import re
from typing import Any


_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]*\Z")
_VERSION = re.compile(r"[0-9][A-Za-z0-9_.+-]*\Z")
_LICENSE = re.compile(r"[A-Za-z0-9 .()+_-]+\Z")


def _normalize_name(name: str) -> str:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("invalid direct dependency distribution name")
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirements(source_manifest: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if not isinstance(source_manifest, dict):
        raise ValueError("source declarations are required")
    result: dict[str, dict[str, Any]] = {}
    for role, key in (
        ("runtime", "declared_runtime_dependencies"),
        ("build", "declared_build_dependencies"),
    ):
        declared = source_manifest.get(key)
        if not isinstance(declared, list) or not declared:
            raise ValueError("missing pinned direct dependencies")
        for requirement in declared:
            if not isinstance(requirement, str) or requirement.count("==") != 1:
                raise ValueError("unpinned direct dependency")
            name, version = requirement.split("==", 1)
            canonical = _normalize_name(name)
            if not _VERSION.fullmatch(version):
                raise ValueError("invalid direct dependency version")
            prior = result.setdefault(canonical, {"version": version, "roles": set()})
            if prior["version"] != version or role in prior["roles"]:
                raise ValueError("conflicting or duplicate direct dependency declarations")
            prior["roles"].add(role)
    return result


def validate_direct_dependency_metadata(
    payload: dict[str, Any], source_manifest: dict[str, Any],
) -> None:
    expected = _requirements(source_manifest)
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "source_sha", "evidence_class", "distributions"}
        or type(payload["schema_version"]) is not int
        or payload["schema_version"] != 1
        or payload["source_sha"] != source_manifest.get("source_sha")
        or payload["evidence_class"] != "BUILD_HOST_DIRECT_DEPENDENCIES_ONLY"
    ):
        raise ValueError("direct dependency metadata schema/identity is invalid")
    entries = payload["distributions"]
    if not isinstance(entries, list) or len(entries) != len(expected):
        raise ValueError("direct dependency metadata has an incomplete distribution set")
    names: list[str] = []
    for item in entries:
        if not isinstance(item, dict) or set(item) != {
            "name", "version", "roles", "license_expression", "license_evidence"
        }:
            raise ValueError("direct dependency metadata entry schema is invalid")
        name, version, roles = item["name"], item["version"], item["roles"]
        if not isinstance(name, str) or _normalize_name(name) != name:
            raise ValueError("direct dependency name is not canonical")
        if name not in expected or version != expected[name]["version"]:
            raise ValueError("installed direct dependency does not match the source pin")
        if roles != sorted(expected[name]["roles"]):
            raise ValueError("direct dependency roles are incorrect")
        expression = item["license_expression"]
        evidence = item["license_evidence"]
        if expression is None:
            if evidence != "NOT_PROVIDED":
                raise ValueError("unsupported license evidence claim")
        elif (
            not isinstance(expression, str)
            or not 0 < len(expression) <= 200
            or not _LICENSE.fullmatch(expression)
            or evidence != "DISTRIBUTION_METADATA_UNVERIFIED"
        ):
            raise ValueError("invalid or falsely verified license metadata")
        names.append(name)
    if names != sorted(set(names)):
        raise ValueError("direct dependency metadata order/identity is ambiguous")


def capture_direct_installed_dependencies(
    source_manifest: dict[str, Any],
) -> dict[str, Any]:
    """Snapshot exact pinned direct distributions visible on the build host."""
    expected = _requirements(source_manifest)
    entries: list[dict[str, Any]] = []
    for name, info in sorted(expected.items()):
        try:
            distribution = metadata.distribution(name)
        except metadata.PackageNotFoundError as exc:
            raise ValueError("required build-host dependency is not installed") from exc
        installed_name = distribution.metadata.get("Name")
        installed_version = distribution.version
        if (
            not isinstance(installed_name, str)
            or _normalize_name(installed_name) != name
            or installed_version != info["version"]
        ):
            raise ValueError("build-host distribution differs from exact source declaration")
        expression = distribution.metadata.get("License-Expression")
        if not (
            isinstance(expression, str)
            and 0 < len(expression) <= 200
            and _LICENSE.fullmatch(expression)
        ):
            expression = None
        entries.append({
            "name": name,
            "version": installed_version,
            "roles": sorted(info["roles"]),
            "license_expression": expression,
            "license_evidence": (
                "DISTRIBUTION_METADATA_UNVERIFIED" if expression is not None
                else "NOT_PROVIDED"
            ),
        })
    result = {
        "schema_version": 1,
        "source_sha": source_manifest.get("source_sha"),
        "evidence_class": "BUILD_HOST_DIRECT_DEPENDENCIES_ONLY",
        "distributions": entries,
    }
    validate_direct_dependency_metadata(result, source_manifest)
    return result

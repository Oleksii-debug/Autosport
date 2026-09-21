"""Deterministic static architecture-fitness evidence for the Autosport package.

The collector deliberately parses Python source without importing project modules.
It reports internal dependency strongly connected components and physical source
size. It does not infer runtime latency or execution safety from static source.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_PACKAGE_NAME = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")


class ArchitectureFitnessError(ValueError):
    """Raised when architecture evidence cannot be produced canonically."""


def _require_package_name(value: object) -> str:
    if type(value) is not str or not _PACKAGE_NAME.fullmatch(value):
        raise ArchitectureFitnessError(
            "package_name must be a canonical dotted Python package name"
        )
    return value


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _source_sha256(source: str) -> str:
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _module_name(package_name: str, relative_path: Path) -> str:
    parts = list(relative_path.with_suffix("").parts)
    if not parts:
        raise ArchitectureFitnessError("module path must not be empty")
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join((package_name, *parts)) if parts else package_name


def _current_package(module_name: str, *, is_package: bool) -> str:
    if is_package:
        return module_name
    if "." not in module_name:
        return ""
    return module_name.rsplit(".", 1)[0]


def _resolve_relative_base(
    module_name: str,
    *,
    is_package: bool,
    level: int,
    imported_module: str | None,
) -> str | None:
    package = _current_package(module_name, is_package=is_package)
    if not package:
        return None
    parts = package.split(".")
    if level < 1 or level > len(parts):
        return None
    keep = len(parts) - (level - 1)
    base = parts[:keep]
    if imported_module:
        base.extend(imported_module.split("."))
    return ".".join(base)


def _is_type_checking_test(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Name) and node.id == "TYPE_CHECKING"
    ) or (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "typing"
        and node.attr == "TYPE_CHECKING"
    )


class _ImportCollector(ast.NodeVisitor):
    def __init__(
        self,
        *,
        module_name: str,
        is_package: bool,
        known_modules: frozenset[str],
    ) -> None:
        self.module_name = module_name
        self.is_package = is_package
        self.known_modules = known_modules
        self.dependencies: set[str] = set()
        self._typing_only = 0

    def _add(self, candidate: str | None) -> bool:
        if candidate in self.known_modules and candidate != self.module_name:
            self.dependencies.add(candidate)
            return True
        return False

    def visit_If(self, node: ast.If) -> Any:
        if _is_type_checking_test(node.test):
            self._typing_only += 1
            for child in node.body:
                self.visit(child)
            self._typing_only -= 1
            for child in node.orelse:
                self.visit(child)
            return None
        return self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        if self._typing_only:
            return
        for alias in node.names:
            candidate = alias.name
            if self._add(candidate):
                continue
            parts = candidate.split(".")
            while len(parts) > 1:
                parts.pop()
                if self._add(".".join(parts)):
                    break

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if self._typing_only:
            return
        if node.level:
            base = _resolve_relative_base(
                self.module_name,
                is_package=self.is_package,
                level=node.level,
                imported_module=node.module,
            )
        else:
            base = node.module
        if not base:
            return

        found_specific = False
        for alias in node.names:
            if alias.name == "*":
                continue
            found_specific = self._add(f"{base}.{alias.name}") or found_specific
        if not found_specific:
            self._add(base)


@dataclass(frozen=True, slots=True)
class ModuleArchitectureMetric:
    module: str
    relative_path: str
    source_lines: int
    source_sha256: str
    internal_imports: tuple[str, ...]

    def __post_init__(self) -> None:
        _require_package_name(self.module)
        if (
            type(self.relative_path) is not str
            or not self.relative_path
            or "\\" in self.relative_path
        ):
            raise ArchitectureFitnessError(
                "relative_path must be a non-empty POSIX path"
            )
        if type(self.source_lines) is not int or self.source_lines < 0:
            raise ArchitectureFitnessError(
                "source_lines must be a non-negative integer"
            )
        if (
            type(self.source_sha256) is not str
            or len(self.source_sha256) != 64
            or any(ch not in "0123456789abcdef" for ch in self.source_sha256)
        ):
            raise ArchitectureFitnessError(
                "source_sha256 must be a canonical SHA-256 digest"
            )
        if not isinstance(self.internal_imports, tuple):
            raise ArchitectureFitnessError("internal_imports must be a tuple")
        if self.internal_imports != tuple(sorted(set(self.internal_imports))):
            raise ArchitectureFitnessError(
                "internal_imports must be unique and sorted"
            )
        for dependency in self.internal_imports:
            _require_package_name(dependency)
            if dependency == self.module:
                raise ArchitectureFitnessError(
                    "module cannot depend on itself in canonical graph"
                )

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "module": self.module,
            "relative_path": self.relative_path,
            "source_lines": self.source_lines,
            "source_sha256": self.source_sha256,
            "internal_imports": list(self.internal_imports),
        }


@dataclass(frozen=True, slots=True)
class DependencyCycle:
    modules: tuple[str, ...]

    def __post_init__(self) -> None:
        if len(self.modules) < 2:
            raise ArchitectureFitnessError(
                "dependency cycle must contain at least two modules"
            )
        if self.modules != tuple(sorted(set(self.modules))):
            raise ArchitectureFitnessError(
                "dependency cycle modules must be unique and sorted"
            )
        for module in self.modules:
            _require_package_name(module)

    def canonical_dict(self) -> dict[str, Any]:
        return {"modules": list(self.modules)}


@dataclass(frozen=True, slots=True)
class ArchitectureFitnessSnapshot:
    package_name: str
    modules: tuple[ModuleArchitectureMetric, ...]
    dependency_cycles: tuple[DependencyCycle, ...]
    schema_version: int = 1

    def __post_init__(self) -> None:
        _require_package_name(self.package_name)
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ArchitectureFitnessError("schema_version must be 1")
        if not self.modules:
            raise ArchitectureFitnessError(
                "architecture snapshot requires at least one module"
            )
        names = tuple(item.module for item in self.modules)
        if names != tuple(sorted(set(names))):
            raise ArchitectureFitnessError("modules must be unique and sorted")
        cycles = tuple(item.modules for item in self.dependency_cycles)
        if cycles != tuple(sorted(set(cycles))):
            raise ArchitectureFitnessError(
                "dependency cycles must be unique and sorted"
            )
        known = set(names)
        for metric in self.modules:
            if not set(metric.internal_imports).issubset(known):
                raise ArchitectureFitnessError(
                    "internal import references unknown module"
                )
        for cycle in self.dependency_cycles:
            if not set(cycle.modules).issubset(known):
                raise ArchitectureFitnessError(
                    "cycle references unknown module"
                )

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "package_name": self.package_name,
            "modules": [item.canonical_dict() for item in self.modules],
            "dependency_cycles": [
                item.canonical_dict() for item in self.dependency_cycles
            ],
        }

    @property
    def evidence_sha256(self) -> str:
        return _canonical_sha256(self.canonical_dict())


@dataclass(frozen=True, slots=True)
class ArchitectureFitnessPolicy:
    max_module_lines: int | None = None
    forbid_dependency_cycles: bool = True
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.max_module_lines is not None and (
            type(self.max_module_lines) is not int
            or self.max_module_lines < 1
        ):
            raise ArchitectureFitnessError(
                "max_module_lines must be a positive integer or None"
            )
        if type(self.forbid_dependency_cycles) is not bool:
            raise ArchitectureFitnessError(
                "forbid_dependency_cycles must be a boolean"
            )
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ArchitectureFitnessError("policy schema_version must be 1")

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "max_module_lines": self.max_module_lines,
            "forbid_dependency_cycles": self.forbid_dependency_cycles,
        }

    @property
    def policy_sha256(self) -> str:
        return _canonical_sha256(self.canonical_dict())


@dataclass(frozen=True, slots=True)
class ArchitectureFitnessAssessment:
    snapshot_sha256: str
    policy_sha256: str
    oversized_modules: tuple[str, ...]
    dependency_cycle_count: int
    compliant: bool
    schema_version: int = 1

    def __post_init__(self) -> None:
        for field_name in ("snapshot_sha256", "policy_sha256"):
            value = getattr(self, field_name)
            if (
                type(value) is not str
                or len(value) != 64
                or any(ch not in "0123456789abcdef" for ch in value)
            ):
                raise ArchitectureFitnessError(
                    f"{field_name} must be a canonical SHA-256 digest"
                )
        if self.oversized_modules != tuple(
            sorted(set(self.oversized_modules))
        ):
            raise ArchitectureFitnessError(
                "oversized_modules must be unique and sorted"
            )
        for module in self.oversized_modules:
            _require_package_name(module)
        if (
            type(self.dependency_cycle_count) is not int
            or self.dependency_cycle_count < 0
        ):
            raise ArchitectureFitnessError(
                "dependency_cycle_count must be non-negative"
            )
        if type(self.compliant) is not bool:
            raise ArchitectureFitnessError("compliant must be a boolean")
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ArchitectureFitnessError(
                "assessment schema_version must be 1"
            )

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "snapshot_sha256": self.snapshot_sha256,
            "policy_sha256": self.policy_sha256,
            "oversized_modules": list(self.oversized_modules),
            "dependency_cycle_count": self.dependency_cycle_count,
            "compliant": self.compliant,
        }

    @property
    def evidence_sha256(self) -> str:
        return _canonical_sha256(self.canonical_dict())


def _strongly_connected_components(
    graph: dict[str, tuple[str, ...]],
) -> tuple[tuple[str, ...], ...]:
    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def connect(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

        for dependency in graph[node]:
            if dependency not in indices:
                connect(dependency)
                lowlinks[node] = min(
                    lowlinks[node], lowlinks[dependency]
                )
            elif dependency in on_stack:
                lowlinks[node] = min(
                    lowlinks[node], indices[dependency]
                )

        if lowlinks[node] == indices[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            components.append(tuple(sorted(component)))

    for module in sorted(graph):
        if module not in indices:
            connect(module)
    return tuple(
        sorted(
            component
            for component in components
            if len(component) > 1
        )
    )


def collect_architecture_fitness(
    package_root: str | Path,
    *,
    package_name: str = "autosport",
) -> ArchitectureFitnessSnapshot:
    """Parse a package tree and return deterministic dependency/size evidence."""

    package_name = _require_package_name(package_name)
    root = Path(package_root)
    if not root.exists() or not root.is_dir():
        raise ArchitectureFitnessError(
            "package_root must be an existing directory"
        )

    paths = tuple(
        sorted(
            path
            for path in root.rglob("*.py")
            if "__pycache__" not in path.relative_to(root).parts
        )
    )
    if not paths:
        raise ArchitectureFitnessError(
            "package_root contains no Python modules"
        )

    by_module: dict[str, Path] = {}
    relative_by_module: dict[str, Path] = {}
    for path in paths:
        relative = path.relative_to(root)
        module = _module_name(package_name, relative)
        if module in by_module:
            raise ArchitectureFitnessError(
                f"duplicate module identity: {module}"
            )
        by_module[module] = path
        relative_by_module[module] = relative
    known_modules = frozenset(by_module)

    metrics: list[ModuleArchitectureMetric] = []
    graph: dict[str, tuple[str, ...]] = {}
    for module in sorted(by_module):
        path = by_module[module]
        relative = relative_by_module[module]
        try:
            source = path.read_text(
                encoding="utf-8", errors="strict"
            )
        except (OSError, UnicodeError) as exc:
            raise ArchitectureFitnessError(
                f"cannot read module source: {relative.as_posix()}"
            ) from exc
        try:
            tree = ast.parse(
                source, filename=relative.as_posix()
            )
        except (SyntaxError, ValueError) as exc:
            raise ArchitectureFitnessError(
                f"cannot parse module source: {relative.as_posix()}"
            ) from exc
        collector = _ImportCollector(
            module_name=module,
            is_package=relative.name == "__init__.py",
            known_modules=known_modules,
        )
        collector.visit(tree)
        dependencies = tuple(sorted(collector.dependencies))
        graph[module] = dependencies
        metrics.append(
            ModuleArchitectureMetric(
                module=module,
                relative_path=relative.as_posix(),
                source_lines=len(source.splitlines()),
                source_sha256=_source_sha256(source),
                internal_imports=dependencies,
            )
        )

    cycles = tuple(
        DependencyCycle(component)
        for component in _strongly_connected_components(graph)
    )
    return ArchitectureFitnessSnapshot(
        package_name=package_name,
        modules=tuple(metrics),
        dependency_cycles=cycles,
    )


def assess_architecture_fitness(
    snapshot: ArchitectureFitnessSnapshot,
    policy: ArchitectureFitnessPolicy,
) -> ArchitectureFitnessAssessment:
    """Evaluate explicit policy without inventing hidden thresholds."""

    if not isinstance(snapshot, ArchitectureFitnessSnapshot):
        raise ArchitectureFitnessError(
            "snapshot must be ArchitectureFitnessSnapshot"
        )
    if not isinstance(policy, ArchitectureFitnessPolicy):
        raise ArchitectureFitnessError(
            "policy must be ArchitectureFitnessPolicy"
        )
    oversized: tuple[str, ...] = ()
    if policy.max_module_lines is not None:
        oversized = tuple(
            metric.module
            for metric in snapshot.modules
            if metric.source_lines > policy.max_module_lines
        )
    cycles_violate = (
        policy.forbid_dependency_cycles
        and bool(snapshot.dependency_cycles)
    )
    return ArchitectureFitnessAssessment(
        snapshot_sha256=snapshot.evidence_sha256,
        policy_sha256=policy.policy_sha256,
        oversized_modules=tuple(sorted(oversized)),
        dependency_cycle_count=len(snapshot.dependency_cycles),
        compliant=not oversized and not cycles_violate,
    )

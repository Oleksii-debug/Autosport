"""Deterministic static architecture-fitness metrics for the Autosport package.

The report is diagnostic only.  It does not authorize release, execution, or
readiness and deliberately avoids arbitrary pass/fail thresholds.
"""

from __future__ import annotations

import ast
import hashlib
import json
import keyword
from dataclasses import dataclass
from pathlib import Path
from typing import Final


SCHEMA: Final = "autosport.architecture_fitness"
SCHEMA_VERSION: Final = 1
_AUTHORITY_NAMES: Final = ("AUTHORITY_FAMILY", "_AUTHORITY_FAMILY")


class ArchitectureFitnessError(ValueError):
    """Raised when source cannot be measured deterministically."""


@dataclass(frozen=True, slots=True, order=True)
class ModuleMetric:
    module: str
    line_count: int
    internal_imports: tuple[str, ...]
    authority_families: tuple[str, ...]
    unresolved_authority_declarations: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "module": self.module,
            "line_count": self.line_count,
            "internal_imports": list(self.internal_imports),
            "authority_families": list(self.authority_families),
            "unresolved_authority_declarations": list(
                self.unresolved_authority_declarations
            ),
        }


@dataclass(frozen=True, slots=True)
class ArchitectureFitnessReport:
    package: str
    modules: tuple[ModuleMetric, ...]
    dependency_cycles: tuple[tuple[str, ...], ...]
    duplicate_authority_families: tuple[tuple[str, tuple[str, ...]], ...]
    release_authority: bool = False
    execution_authority: bool = False
    readiness_authority: bool = False
    whole_product_complete: bool = False

    @property
    def module_count(self) -> int:
        return len(self.modules)

    @property
    def internal_edge_count(self) -> int:
        return sum(len(item.internal_imports) for item in self.modules)

    @property
    def modules_by_size(self) -> tuple[tuple[str, int], ...]:
        return tuple(
            (item.module, item.line_count)
            for item in sorted(
                self.modules,
                key=lambda item: (-item.line_count, item.module),
            )
        )

    def _payload(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "schema_version": SCHEMA_VERSION,
            "package": self.package,
            "module_count": self.module_count,
            "internal_edge_count": self.internal_edge_count,
            "modules": [item.to_dict() for item in self.modules],
            "modules_by_size": [list(item) for item in self.modules_by_size],
            "dependency_cycles": [list(item) for item in self.dependency_cycles],
            "duplicate_authority_families": [
                {"authority_family": authority, "modules": list(modules)}
                for authority, modules in self.duplicate_authority_families
            ],
            "release_authority": False,
            "execution_authority": False,
            "readiness_authority": False,
            "whole_product_complete": False,
        }

    @property
    def report_sha256(self) -> str:
        encoded = json.dumps(
            self._payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {**self._payload(), "report_sha256": self.report_sha256}


def _module_name(package_dir: Path, path: Path, package: str) -> str:
    relative = path.relative_to(package_dir)
    parts = list(relative.parts)
    filename = parts.pop()
    stem = filename[:-3]
    suffix = parts
    if stem != "__init__":
        suffix.append(stem)
    return ".".join((package, *suffix))


def _line_count(source: str) -> int:
    if not source:
        return 0
    return source.count("\n") + (0 if source.endswith("\n") else 1)


def _module_level_authorities(
    tree: ast.Module,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    values: set[str] = set()
    unresolved: set[str] = set()
    for node in tree.body:
        name: str | None = None
        value: ast.expr | None = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                name = target.id
                value = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
            value = node.value
        if name is None or value is None:
            continue
        if not (name == _AUTHORITY_NAMES[0] or name.endswith(_AUTHORITY_NAMES[1])):
            continue
        if isinstance(value, ast.Constant) and type(value.value) is str:
            authority = value.value
            if authority and authority == authority.strip() and "\x00" not in authority:
                values.add(authority)
                continue
        unresolved.add(name)
    return tuple(sorted(values)), tuple(sorted(unresolved))


def _relative_base(module: str, is_package: bool, level: int) -> tuple[str, ...]:
    current = module.split(".") if is_package else module.split(".")[:-1]
    if level <= 0:
        return tuple(current)
    remove = level - 1
    if remove > len(current):
        return ()
    return tuple(current[: len(current) - remove])


def _import_candidates(
    tree: ast.Module,
    module: str,
    *,
    is_package: bool,
    package: str,
) -> tuple[str, ...]:
    candidates: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == package or alias.name.startswith(package + "."):
                    candidates.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = _relative_base(module, is_package, node.level)
                if not base or base[0] != package:
                    continue
                if node.module:
                    imported = (*base, *node.module.split("."))
                    for alias in node.names:
                        if alias.name == "*":
                            candidates.add(".".join(imported))
                        else:
                            candidates.add(".".join((*imported, alias.name)))
                else:
                    for alias in node.names:
                        if alias.name != "*":
                            candidates.add(".".join((*base, alias.name)))
            elif node.module and (
                node.module == package or node.module.startswith(package + ".")
            ):
                for alias in node.names:
                    if alias.name == "*":
                        candidates.add(node.module)
                    else:
                        candidates.add(f"{node.module}.{alias.name}")
    return tuple(sorted(candidates))


def _resolve_candidate(candidate: str, modules: set[str]) -> str | None:
    parts = candidate.split(".")
    while parts:
        joined = ".".join(parts)
        if joined in modules:
            return joined
        parts.pop()
    return None


def _cycles(graph: dict[str, tuple[str, ...]]) -> tuple[tuple[str, ...], ...]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indexes: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        nonlocal index
        indexes[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)
        for target in graph[node]:
            if target not in indexes:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indexes[target])
        if lowlinks[node] != indexes[node]:
            return
        component: list[str] = []
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == node:
                break
        ordered = tuple(sorted(component))
        if len(ordered) > 1 or (len(ordered) == 1 and node in graph[node]):
            components.append(ordered)

    for node in sorted(graph):
        if node not in indexes:
            visit(node)
    return tuple(sorted(components))


def analyze_package(
    package_dir: str | Path,
    *,
    package_name: str | None = None,
) -> ArchitectureFitnessReport:
    """Measure one Python package without importing or executing its modules."""

    root = Path(package_dir)
    if root.is_symlink():
        raise ArchitectureFitnessError("package_dir symlink is not canonical")
    if not root.is_dir():
        raise ArchitectureFitnessError("package_dir must be an existing directory")
    package = package_name or root.name
    if not package or not package.isidentifier() or keyword.iskeyword(package):
        raise ArchitectureFitnessError("package_name must be a canonical Python identifier")

    try:
        entries = tuple(sorted(root.rglob("*")))
    except OSError as exc:
        raise ArchitectureFitnessError(
            f"cannot enumerate package tree: {type(exc).__name__}"
        ) from exc
    for entry in entries:
        if entry.is_symlink():
            relative = entry.relative_to(root).as_posix()
            raise ArchitectureFitnessError(
                f"symlinked package entry is not canonical: {relative}"
            )

    paths = tuple(
        path
        for path in entries
        if path.suffix == ".py" and "__pycache__" not in path.parts
    )
    if not paths:
        raise ArchitectureFitnessError("package contains no Python modules")

    parsed: dict[str, tuple[Path, str, ast.Module, bool]] = {}
    for path in paths:
        module = _module_name(root, path, package)
        try:
            source = path.read_text(encoding="utf-8", errors="strict")
            tree = ast.parse(source, filename=str(path))
        except (OSError, UnicodeError, SyntaxError) as exc:
            raise ArchitectureFitnessError(f"cannot parse {module}: {type(exc).__name__}") from exc
        if module in parsed:
            raise ArchitectureFitnessError(f"duplicate module identity: {module}")
        parsed[module] = (path, source, tree, path.name == "__init__.py")

    module_names = set(parsed)
    metrics: list[ModuleMetric] = []
    graph: dict[str, tuple[str, ...]] = {}
    authority_owners: dict[str, set[str]] = {}
    for module in sorted(parsed):
        _path, source, tree, is_package = parsed[module]
        resolved = {
            target
            for candidate in _import_candidates(
                tree,
                module,
                is_package=is_package,
                package=package,
            )
            if (target := _resolve_candidate(candidate, module_names)) is not None
            and target != module
        }
        imports = tuple(sorted(resolved))
        graph[module] = imports
        authorities, unresolved_authorities = _module_level_authorities(tree)
        for authority in authorities:
            authority_owners.setdefault(authority, set()).add(module)
        metrics.append(
            ModuleMetric(
                module=module,
                line_count=_line_count(source),
                internal_imports=imports,
                authority_families=authorities,
                unresolved_authority_declarations=unresolved_authorities,
            )
        )

    duplicates = tuple(
        sorted(
            (authority, tuple(sorted(owners)))
            for authority, owners in authority_owners.items()
            if len(owners) > 1
        )
    )
    return ArchitectureFitnessReport(
        package=package,
        modules=tuple(metrics),
        dependency_cycles=_cycles(graph),
        duplicate_authority_families=duplicates,
    )


__all__ = [
    "ArchitectureFitnessError",
    "ArchitectureFitnessReport",
    "ModuleMetric",
    "SCHEMA",
    "SCHEMA_VERSION",
    "analyze_package",
]

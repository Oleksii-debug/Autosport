"""Deterministic structural architecture measurements for the Autosport package.

The scanner is intentionally static: it parses Python source with :mod:`ast` and
never imports or executes product modules.  It reports internal import cycles and
module sizes, while leaving policy thresholds explicit at the call site so an
unmeasured budget cannot silently become release truth.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Iterable, Sequence


class ArchitectureFitnessError(RuntimeError):
    """Static architecture evidence could not be produced safely."""


@dataclass(frozen=True, slots=True)
class ModuleMetric:
    module: str
    path: str
    byte_size: int
    line_count: int
    internal_imports: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "module": self.module,
            "path": self.path,
            "byte_size": self.byte_size,
            "line_count": self.line_count,
            "internal_imports": list(self.internal_imports),
        }


@dataclass(frozen=True, slots=True)
class ArchitectureFitnessReport:
    package_name: str
    modules: tuple[ModuleMetric, ...]
    internal_edges: tuple[tuple[str, str], ...]
    import_cycles: tuple[tuple[str, ...], ...]
    max_module_bytes: int | None
    max_cycle_count: int | None
    oversized_modules: tuple[str, ...]
    budget_violations: tuple[str, ...]

    @property
    def budget_passed(self) -> bool:
        return not self.budget_violations

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "autosport.architecture_fitness",
            "schema_version": 1,
            "package_name": self.package_name,
            "module_count": len(self.modules),
            "internal_edge_count": len(self.internal_edges),
            "import_cycle_count": len(self.import_cycles),
            "max_module_bytes": self.max_module_bytes,
            "max_cycle_count": self.max_cycle_count,
            "budget_passed": self.budget_passed,
            "oversized_modules": list(self.oversized_modules),
            "budget_violations": list(self.budget_violations),
            "import_cycles": [list(cycle) for cycle in self.import_cycles],
            "modules": [metric.to_dict() for metric in self.modules],
        }


def _positive_or_zero(value: int | None, name: str, *, allow_zero: bool) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ArchitectureFitnessError(f"{name} must be an integer")
    minimum = 0 if allow_zero else 1
    if value < minimum:
        comparator = ">= 0" if allow_zero else ">= 1"
        raise ArchitectureFitnessError(f"{name} must be {comparator}")
    return value


def _module_name(package_root: Path, source: Path, package_name: str) -> str:
    relative = source.relative_to(package_root)
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join((package_name, *parts)) if parts else package_name


def _line_count(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _package_for_module(module: str, is_package_init: bool) -> str:
    if is_package_init:
        return module
    return module.rpartition(".")[0]


def _relative_base(
    *,
    current_module: str,
    current_is_package_init: bool,
    level: int,
    imported_module: str | None,
) -> str | None:
    current_package = _package_for_module(current_module, current_is_package_init)
    package_parts = current_package.split(".") if current_package else []
    if level <= 0 or level > len(package_parts):
        return None
    base_parts = package_parts[: len(package_parts) - level + 1]
    if imported_module:
        base_parts.extend(imported_module.split("."))
    return ".".join(base_parts)


def _longest_known_prefix(name: str, known_modules: set[str]) -> str | None:
    candidate = name
    while candidate:
        if candidate in known_modules:
            return candidate
        candidate = candidate.rpartition(".")[0]
    return None


def _internal_imports(
    tree: ast.AST,
    *,
    current_module: str,
    current_is_package_init: bool,
    package_name: str,
    known_modules: set[str],
) -> tuple[str, ...]:
    targets: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == package_name or alias.name.startswith(package_name + "."):
                    target = _longest_known_prefix(alias.name, known_modules)
                    if target is not None:
                        targets.add(target)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue

        if node.level:
            base = _relative_base(
                current_module=current_module,
                current_is_package_init=current_is_package_init,
                level=node.level,
                imported_module=node.module,
            )
        else:
            base = node.module

        if not base or not (base == package_name or base.startswith(package_name + ".")):
            continue

        base_target = _longest_known_prefix(base, known_modules)
        if base_target is not None:
            targets.add(base_target)
        for alias in node.names:
            if alias.name == "*":
                continue
            candidate = f"{base}.{alias.name}"
            target = _longest_known_prefix(candidate, known_modules)
            if target is not None and target != base_target:
                targets.add(target)

    return tuple(sorted(targets))


def _strongly_connected_components(
    nodes: Iterable[str],
    edges: Iterable[tuple[str, str]],
) -> tuple[tuple[str, ...], ...]:
    adjacency: dict[str, list[str]] = {node: [] for node in sorted(nodes)}
    edge_set = set(edges)
    for source, target in sorted(edge_set):
        if source in adjacency and target in adjacency:
            adjacency[source].append(target)

    index = 0
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    components: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

        for target in adjacency[node]:
            if target not in indices:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])

        if lowlinks[node] != indices[node]:
            return
        component: list[str] = []
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.append(member)
            if member == node:
                break
        ordered = tuple(sorted(component))
        if len(ordered) > 1 or (ordered[0], ordered[0]) in edge_set:
            components.append(ordered)

    for node in sorted(adjacency):
        if node not in indices:
            visit(node)

    return tuple(sorted(components))


def measure_architecture_fitness(
    package_root: str | Path,
    *,
    package_name: str = "autosport",
    max_module_bytes: int | None = None,
    max_cycle_count: int | None = None,
) -> ArchitectureFitnessReport:
    """Measure static package structure without importing product modules.

    Budgets are opt-in.  Omitting a budget records evidence but cannot manufacture a
    release pass/fail threshold.  ``max_cycle_count=0`` is valid when a caller has
    explicitly adopted a no-cycle policy.
    """

    root = Path(package_root).resolve()
    if not root.is_dir():
        raise ArchitectureFitnessError("package_root must be an existing directory")
    if type(package_name) is not str or not package_name or package_name != package_name.strip():
        raise ArchitectureFitnessError("package_name must be non-empty canonical text")
    if any(not part.isidentifier() for part in package_name.split(".")):
        raise ArchitectureFitnessError("package_name must be a dotted Python identifier")
    module_budget = _positive_or_zero(max_module_bytes, "max_module_bytes", allow_zero=False)
    cycle_budget = _positive_or_zero(max_cycle_count, "max_cycle_count", allow_zero=True)

    source_files = tuple(sorted(root.rglob("*.py"), key=lambda path: path.as_posix()))
    if not source_files:
        raise ArchitectureFitnessError("package_root contains no Python source files")

    module_by_path = {
        source: _module_name(root, source, package_name)
        for source in source_files
    }
    if len(set(module_by_path.values())) != len(module_by_path):
        raise ArchitectureFitnessError("package source paths do not map to unique module names")
    known_modules = set(module_by_path.values())

    parsed: dict[Path, tuple[str, ast.AST, bytes]] = {}
    for source in source_files:
        try:
            raw = source.read_bytes()
            text = raw.decode("utf-8")
            tree = ast.parse(text, filename=source.name)
        except (OSError, UnicodeDecodeError, SyntaxError) as exc:
            relative = source.relative_to(root).as_posix()
            raise ArchitectureFitnessError(
                f"cannot parse package source: {relative}: {type(exc).__name__}"
            ) from None
        parsed[source] = (text, tree, raw)

    metrics: list[ModuleMetric] = []
    edges: set[tuple[str, str]] = set()
    for source in source_files:
        module = module_by_path[source]
        text, tree, raw = parsed[source]
        imports = _internal_imports(
            tree,
            current_module=module,
            current_is_package_init=source.name == "__init__.py",
            package_name=package_name,
            known_modules=known_modules,
        )
        for target in imports:
            edges.add((module, target))
        metrics.append(
            ModuleMetric(
                module=module,
                path=source.relative_to(root).as_posix(),
                byte_size=len(raw),
                line_count=_line_count(text),
                internal_imports=imports,
            )
        )

    ordered_metrics = tuple(sorted(metrics, key=lambda metric: metric.module))
    ordered_edges = tuple(sorted(edges))
    cycles = _strongly_connected_components(known_modules, ordered_edges)
    oversized = tuple(
        metric.module
        for metric in ordered_metrics
        if module_budget is not None and metric.byte_size > module_budget
    )

    violations: list[str] = []
    if module_budget is not None and oversized:
        violations.append(
            f"{len(oversized)} module(s) exceed max_module_bytes={module_budget}"
        )
    if cycle_budget is not None and len(cycles) > cycle_budget:
        violations.append(
            f"{len(cycles)} import cycle(s) exceed max_cycle_count={cycle_budget}"
        )

    return ArchitectureFitnessReport(
        package_name=package_name,
        modules=ordered_metrics,
        internal_edges=ordered_edges,
        import_cycles=cycles,
        max_module_bytes=module_budget,
        max_cycle_count=cycle_budget,
        oversized_modules=oversized,
        budget_violations=tuple(violations),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Measure Autosport Python architecture statically; budgets are explicit and opt-in."
        )
    )
    parser.add_argument(
        "--package-root",
        default=str(Path(__file__).resolve().parent),
        help="Package directory to scan (default: installed autosport package).",
    )
    parser.add_argument(
        "--package-name",
        default="autosport",
        help="Import package name represented by --package-root.",
    )
    parser.add_argument(
        "--max-module-bytes",
        type=int,
        default=None,
        help="Optional explicit per-module byte budget; omitted means measure only.",
    )
    parser.add_argument(
        "--max-cycle-count",
        type=int,
        default=None,
        help="Optional explicit internal import-cycle budget; 0 means no cycles allowed.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = measure_architecture_fitness(
            args.package_root,
            package_name=args.package_name,
            max_module_bytes=args.max_module_bytes,
            max_cycle_count=args.max_cycle_count,
        )
    except ArchitectureFitnessError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(report.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if report.budget_passed else 3


if __name__ == "__main__":  # pragma: no cover - exercised through main().
    raise SystemExit(main())

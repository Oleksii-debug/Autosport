from __future__ import annotations

import argparse
import ast
import importlib.util
import io
import json
import token
import tokenize
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

_SCHEMA_VERSION = 1
_DEFAULT_MAX_SOURCE_LINES = 1_000


class ArchitectureFitnessError(ValueError):
    """Raised when architecture evidence cannot be produced deterministically."""


@dataclass(frozen=True, slots=True)
class ModuleMetric:
    module: str
    path: str
    physical_lines: int
    source_lines: int

    def to_payload(self) -> dict[str, object]:
        return {
            "module": self.module,
            "path": self.path,
            "physical_lines": self.physical_lines,
            "source_lines": self.source_lines,
        }


@dataclass(frozen=True, slots=True)
class ArchitectureFitnessReport:
    package: str
    package_root: str
    max_source_lines: int
    modules: tuple[ModuleMetric, ...]
    edges: tuple[tuple[str, str], ...]
    cycles: tuple[tuple[str, ...], ...]

    @property
    def oversized_modules(self) -> tuple[ModuleMetric, ...]:
        return tuple(
            metric
            for metric in self.modules
            if metric.source_lines > self.max_source_lines
        )

    @property
    def healthy(self) -> bool:
        return not self.cycles and not self.oversized_modules

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "kind": "autosport-architecture-fitness-v1",
            "package": self.package,
            "package_root": self.package_root,
            "thresholds": {"max_source_lines": self.max_source_lines},
            "summary": {
                "module_count": len(self.modules),
                "local_import_edge_count": len(self.edges),
                "cycle_count": len(self.cycles),
                "oversized_module_count": len(self.oversized_modules),
                "healthy": self.healthy,
            },
            "cycles": [list(cycle) for cycle in self.cycles],
            "oversized_modules": [
                metric.to_payload() for metric in self.oversized_modules
            ],
            "modules": [metric.to_payload() for metric in self.modules],
            "local_import_edges": [list(edge) for edge in self.edges],
            "truth": {
                "runtime_modules_executed": False,
                "latency_measured": False,
                "release_readiness_claim": False,
            },
        }


def _positive_int(name: str, value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ArchitectureFitnessError(f"{name} must be a positive integer")
    return value


def _module_name(path: Path, root: Path, package: str) -> str:
    relative = path.relative_to(root)
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    suffix = ".".join(parts)
    return package if not suffix else f"{package}.{suffix}"


def _relative_path(path: Path, root: Path) -> str:
    return path.relative_to(root.parent).as_posix()


def _source_line_count(source: str) -> int:
    meaningful_lines: set[int] = set()
    ignored = {
        token.ENDMARKER,
        token.ENCODING,
        token.INDENT,
        token.DEDENT,
        token.NEWLINE,
        tokenize.NL,
        tokenize.COMMENT,
    }
    try:
        stream = tokenize.generate_tokens(io.StringIO(source).readline)
        for item in stream:
            if item.type not in ignored and item.string.strip():
                meaningful_lines.add(item.start[0])
    except (IndentationError, tokenize.TokenError) as exc:
        raise ArchitectureFitnessError(f"tokenization failed: {exc}") from exc
    return len(meaningful_lines)


def _read_module(path: Path) -> tuple[ast.Module, int, int]:
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ArchitectureFitnessError(f"cannot read {path}: {exc}") from exc
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as exc:
        raise ArchitectureFitnessError(
            f"cannot parse {path}:{exc.lineno}: {exc.msg}"
        ) from exc
    physical = len(source.splitlines())
    return tree, physical, _source_line_count(source)


def _containing_package(module: str, *, is_package: bool) -> str:
    if is_package:
        return module
    return module.rpartition(".")[0]


def _existing_module_prefix(name: str, known: set[str], package: str) -> str | None:
    if name == package and name in known:
        return name
    if not name.startswith(f"{package}."):
        return None
    candidate = name
    while candidate.startswith(package):
        if candidate in known:
            return candidate
        if "." not in candidate:
            break
        candidate = candidate.rpartition(".")[0]
    return None


def _resolve_from_base(
    node: ast.ImportFrom,
    current_module: str,
    *,
    is_package: bool,
) -> str | None:
    if node.level == 0:
        return node.module
    package_context = _containing_package(current_module, is_package=is_package)
    if not package_context:
        return None
    relative_name = "." * node.level + (node.module or "")
    try:
        return importlib.util.resolve_name(relative_name, package_context)
    except (ImportError, ValueError):
        return None


def _local_dependencies(
    tree: ast.Module,
    current_module: str,
    *,
    is_package: bool,
    known: set[str],
    packages: set[str],
    package: str,
) -> set[str]:
    dependencies: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = _existing_module_prefix(alias.name, known, package)
                if target is not None and target != current_module:
                    dependencies.add(target)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from_base(
                node,
                current_module,
                is_package=is_package,
            )
            if base is None:
                continue
            if node.module is not None:
                target = _existing_module_prefix(base, known, package)
                if target is not None and target != current_module:
                    dependencies.add(target)
                if base in packages:
                    for alias in node.names:
                        if alias.name == "*":
                            continue
                        alias_target = f"{base}.{alias.name}"
                        if alias_target in known and alias_target != current_module:
                            dependencies.add(alias_target)
            if node.module is None:
                for alias in node.names:
                    if alias.name == "*":
                        continue
                    alias_target = _existing_module_prefix(
                        f"{base}.{alias.name}", known, package
                    )
                    if alias_target is not None and alias_target != current_module:
                        dependencies.add(alias_target)
    return dependencies


def _strongly_connected_components(
    graph: dict[str, set[str]],
) -> tuple[tuple[str, ...], ...]:
    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    components: list[tuple[str, ...]] = []

    def visit(node: str) -> None:
        nonlocal index
        indices[node] = index
        lowlinks[node] = index
        index += 1
        stack.append(node)
        on_stack.add(node)

        for target in sorted(graph[node]):
            if target not in indices:
                visit(target)
                lowlinks[node] = min(lowlinks[node], lowlinks[target])
            elif target in on_stack:
                lowlinks[node] = min(lowlinks[node], indices[target])

        if lowlinks[node] == indices[node]:
            component: list[str] = []
            while True:
                member = stack.pop()
                on_stack.remove(member)
                component.append(member)
                if member == node:
                    break
            ordered = tuple(sorted(component))
            if len(ordered) > 1 or node in graph[node]:
                components.append(ordered)

    for node in sorted(graph):
        if node not in indices:
            visit(node)
    return tuple(sorted(components))


def analyze_package(
    package_root: str | Path,
    *,
    package: str = "autosport",
    max_source_lines: int = _DEFAULT_MAX_SOURCE_LINES,
) -> ArchitectureFitnessReport:
    root = Path(package_root)
    if not root.is_dir():
        raise ArchitectureFitnessError("package_root must be an existing directory")
    if not package or package.strip() != package or "." in package:
        raise ArchitectureFitnessError("package must be one canonical top-level name")
    threshold = _positive_int("max_source_lines", max_source_lines)

    paths = tuple(sorted(root.rglob("*.py")))
    if not paths:
        raise ArchitectureFitnessError("package_root contains no Python modules")

    module_paths: dict[str, tuple[Path, bool]] = {}
    for path in paths:
        module = _module_name(path, root, package)
        if module in module_paths:
            raise ArchitectureFitnessError(f"duplicate module identity: {module}")
        module_paths[module] = (path, path.name == "__init__.py")

    known = set(module_paths)
    packages = {
        module for module, (_, is_package) in module_paths.items() if is_package
    }
    graph: dict[str, set[str]] = {module: set() for module in known}
    metrics: list[ModuleMetric] = []
    for module in sorted(module_paths):
        path, is_package = module_paths[module]
        tree, physical_lines, source_lines = _read_module(path)
        graph[module] = _local_dependencies(
            tree,
            module,
            is_package=is_package,
            known=known,
            packages=packages,
            package=package,
        )
        metrics.append(
            ModuleMetric(
                module=module,
                path=_relative_path(path, root),
                physical_lines=physical_lines,
                source_lines=source_lines,
            )
        )

    edges = tuple(
        (source, target)
        for source in sorted(graph)
        for target in sorted(graph[source])
    )
    return ArchitectureFitnessReport(
        package=package,
        package_root=root.name,
        max_source_lines=threshold,
        modules=tuple(metrics),
        edges=edges,
        cycles=_strongly_connected_components(graph),
    )


def _parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure deterministic static architecture fitness for Autosport."
    )
    parser.add_argument("--package-root", default="src/autosport")
    parser.add_argument("--package", default="autosport")
    parser.add_argument(
        "--max-source-lines", type=int, default=_DEFAULT_MAX_SOURCE_LINES
    )
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero when import cycles or oversized modules are present",
    )
    return parser.parse_args(argv)


def main(argv: Iterable[str] | None = None) -> int:
    args = _parse_args(argv)
    report = analyze_package(
        args.package_root,
        package=args.package,
        max_source_lines=args.max_source_lines,
    )
    payload = report.to_payload()
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    else:
        summary = payload["summary"]
        print(
            "scope=static_architecture_fitness "
            "runtime_modules_executed=false latency_measured=false "
            f"modules={summary['module_count']} edges={summary['local_import_edge_count']} "
            f"cycles={summary['cycle_count']} oversized={summary['oversized_module_count']} "
            f"max_source_lines={args.max_source_lines} healthy={str(report.healthy).lower()}"
        )
        for cycle in report.cycles:
            print("cycle=" + " -> ".join(cycle))
        for metric in report.oversized_modules:
            print(
                f"oversized={metric.module} source_lines={metric.source_lines} "
                f"physical_lines={metric.physical_lines} path={metric.path}"
            )
    return 1 if args.check and not report.healthy else 0


if __name__ == "__main__":
    raise SystemExit(main())

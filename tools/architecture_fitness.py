from __future__ import annotations

import argparse
import ast
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


SCHEMA_VERSION = 1


class ArchitectureFitnessError(RuntimeError):
    """Raised when a deterministic architecture report cannot be produced."""


@dataclass(frozen=True)
class _ParsedModule:
    name: str
    path: str
    is_package: bool
    line_count: int
    ast_node_count: int
    sha256: str
    tree: ast.AST


def _canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _module_name(relative_path: Path) -> tuple[str, bool]:
    parts = list(relative_path.parts)
    filename = parts[-1]
    is_package = filename == "__init__.py"
    if is_package:
        parts = parts[:-1]
    else:
        parts[-1] = relative_path.stem

    if not parts:
        return "__root__", is_package
    return ".".join(parts), is_package


def _read_modules(source_root: Path) -> tuple[_ParsedModule, ...]:
    source_root = source_root.resolve()
    if not source_root.is_dir():
        raise ArchitectureFitnessError(f"source root is not a directory: {source_root}")

    modules: list[_ParsedModule] = []
    for path in sorted(source_root.rglob("*.py"), key=lambda item: item.relative_to(source_root).as_posix()):
        relative = path.relative_to(source_root)
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ArchitectureFitnessError(
                f"non-UTF-8 Python source: {relative.as_posix()}"
            ) from exc

        try:
            tree = ast.parse(text, filename=relative.as_posix())
        except SyntaxError as exc:
            location = f"{relative.as_posix()}:{exc.lineno or 0}:{exc.offset or 0}"
            raise ArchitectureFitnessError(f"Python syntax error at {location}") from exc

        name, is_package = _module_name(relative)
        modules.append(
            _ParsedModule(
                name=name,
                path=relative.as_posix(),
                is_package=is_package,
                line_count=len(text.splitlines()),
                ast_node_count=sum(1 for _ in ast.walk(tree)),
                sha256=_sha256_bytes(raw),
                tree=tree,
            )
        )

    if not modules:
        raise ArchitectureFitnessError("source root contains no Python modules")

    names = [module.name for module in modules]
    if len(names) != len(set(names)):
        raise ArchitectureFitnessError("source root maps multiple files to the same module name")
    return tuple(modules)


def _relative_base(module: _ParsedModule, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module or ""

    package_parts = module.name.split(".") if module.is_package else module.name.split(".")[:-1]
    ascend = node.level - 1
    if ascend > len(package_parts):
        return None

    kept = package_parts[: len(package_parts) - ascend]
    if node.module:
        kept.extend(node.module.split("."))
    return ".".join(part for part in kept if part)


def _resolve_imports(module: _ParsedModule, module_names: frozenset[str]) -> tuple[str, ...]:
    imports: set[str] = set()

    for node in ast.walk(module.tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in module_names and alias.name != module.name:
                    imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            base = _relative_base(module, node)
            if base is None:
                continue

            resolved_alias = False
            for alias in node.names:
                if alias.name == "*":
                    continue
                candidate = f"{base}.{alias.name}" if base else alias.name
                if candidate in module_names and candidate != module.name:
                    imports.add(candidate)
                    resolved_alias = True

            if not resolved_alias and base in module_names and base != module.name:
                imports.add(base)

    return tuple(sorted(imports))


def _cycle_components(graph: dict[str, tuple[str, ...]]) -> tuple[tuple[str, ...], ...]:
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

        for target in graph[node]:
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

        component.sort()
        if len(component) > 1 or node in graph[node]:
            components.append(tuple(component))

    for node in sorted(graph):
        if node not in indices:
            visit(node)

    return tuple(sorted(components))


def _rank(
    modules: Iterable[_ParsedModule],
    metric: dict[str, int],
    *,
    top_n: int,
) -> list[dict[str, object]]:
    by_name = {module.name: module for module in modules}
    ranked = sorted(
        metric,
        key=lambda name: (-metric[name], by_name[name].path),
    )[:top_n]
    return [
        {
            "module": name,
            "path": by_name[name].path,
            "value": metric[name],
        }
        for name in ranked
    ]


def measure_architecture(source_root: Path | str, *, top_n: int = 20) -> dict[str, object]:
    """Return deterministic static architecture measurements for a Python source tree.

    The report is descriptive only. It deliberately carries no pass/fail threshold and
    grants no runtime, release, scientific, economic, execution, or provider authority.
    """

    if isinstance(top_n, bool) or not isinstance(top_n, int) or top_n <= 0:
        raise ValueError("top_n must be a positive integer")

    root = Path(source_root)
    modules = _read_modules(root)
    module_names = frozenset(module.name for module in modules)

    graph = {
        module.name: _resolve_imports(module, module_names)
        for module in modules
    }
    fan_in = {name: 0 for name in module_names}
    for targets in graph.values():
        for target in targets:
            fan_in[target] += 1
    fan_out = {name: len(graph[name]) for name in module_names}

    cycles = _cycle_components(graph)
    module_records = [
        {
            "module": module.name,
            "path": module.path,
            "line_count": module.line_count,
            "ast_node_count": module.ast_node_count,
            "sha256": module.sha256,
            "internal_imports": list(graph[module.name]),
            "fan_in": fan_in[module.name],
            "fan_out": fan_out[module.name],
        }
        for module in sorted(modules, key=lambda item: item.path)
    ]

    source_fingerprint_rows = [
        [record["path"], record["sha256"]]
        for record in module_records
    ]
    source_tree_sha256 = _sha256_bytes(_canonical_bytes(source_fingerprint_rows))

    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "measurement_kind": "STATIC_PYTHON_ARCHITECTURE_FITNESS",
        "source_root": root.name,
        "source_tree_sha256": source_tree_sha256,
        "summary": {
            "module_count": len(modules),
            "total_line_count": sum(module.line_count for module in modules),
            "total_ast_node_count": sum(module.ast_node_count for module in modules),
            "internal_import_edge_count": sum(len(targets) for targets in graph.values()),
            "dependency_cycle_count": len(cycles),
        },
        "dependency_cycles": [list(component) for component in cycles],
        "top_modules_by_lines": [
            {
                "module": module.name,
                "path": module.path,
                "value": module.line_count,
            }
            for module in sorted(
                modules,
                key=lambda item: (-item.line_count, item.path),
            )[:top_n]
        ],
        "top_modules_by_ast_nodes": [
            {
                "module": module.name,
                "path": module.path,
                "value": module.ast_node_count,
            }
            for module in sorted(
                modules,
                key=lambda item: (-item.ast_node_count, item.path),
            )[:top_n]
        ],
        "top_fan_in": _rank(modules, fan_in, top_n=top_n),
        "top_fan_out": _rank(modules, fan_out, top_n=top_n),
        "modules": module_records,
        "non_claims": [
            "NO_PASS_FAIL_THRESHOLD",
            "NO_RUNTIME_AUTHORITY",
            "NO_RELEASE_AUTHORITY",
            "NO_ECONOMIC_AUTHORITY",
            "NO_EXECUTION_AUTHORITY",
            "NO_PROVIDER_AUTHORITY",
        ],
    }
    payload["report_sha256"] = _sha256_bytes(_canonical_bytes(payload))
    return payload


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Measure deterministic Python module size/import/cycle architecture fitness "
            "without imposing policy thresholds."
        )
    )
    parser.add_argument(
        "source_root",
        nargs="?",
        default="src",
        help="Python source tree to inspect (default: src)",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=20,
        help="Number of ranked modules to include per metric (default: 20)",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Emit canonical compact JSON instead of indented JSON",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    report = measure_architecture(args.source_root, top_n=args.top_n)
    if args.compact:
        print(_canonical_bytes(report).decode("utf-8"))
    else:
        print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

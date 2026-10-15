from __future__ import annotations

import ast
from pathlib import Path
from typing import Mapping


AUTOSPORT_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "autosport"
PRODUCT_SHELL_MODULES = frozenset(
    {
        "product_runtime",
        "product_entrypoint",
        "gui",
        "windows_gui",
    }
)


def _module_imports(module_name: str) -> frozenset[str]:
    path = AUTOSPORT_PACKAGE / f"{module_name}.py"
    assert path.is_file(), f"canonical product shell module is missing: {path.name}"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            candidates: tuple[str, ...] = ()
            if node.level == 1:
                candidates = (
                    (node.module.split(".", 1)[0],)
                    if node.module
                    else tuple(alias.name.split(".", 1)[0] for alias in node.names)
                )
            elif node.level == 0 and node.module and node.module.startswith("autosport."):
                candidates = (node.module.removeprefix("autosport.").split(".", 1)[0],)
            for candidate in candidates:
                if candidate in PRODUCT_SHELL_MODULES:
                    imports.add(candidate)
            continue

        if isinstance(node, ast.Import):
            for alias in node.names:
                if not alias.name.startswith("autosport."):
                    continue
                candidate = alias.name.removeprefix("autosport.").split(".", 1)[0]
                if candidate in PRODUCT_SHELL_MODULES:
                    imports.add(candidate)

    return frozenset(imports)


def _import_graph() -> dict[str, frozenset[str]]:
    return {
        module_name: _module_imports(module_name)
        for module_name in sorted(PRODUCT_SHELL_MODULES)
    }


def _find_cycle(graph: Mapping[str, frozenset[str]]) -> tuple[str, ...] | None:
    visited: set[str] = set()
    active: list[str] = []
    active_index: dict[str, int] = {}

    def visit(module_name: str) -> tuple[str, ...] | None:
        if module_name in active_index:
            start = active_index[module_name]
            return tuple(active[start:] + [module_name])
        if module_name in visited:
            return None

        active_index[module_name] = len(active)
        active.append(module_name)
        for dependency in sorted(graph.get(module_name, frozenset())):
            cycle = visit(dependency)
            if cycle is not None:
                return cycle
        active.pop()
        active_index.pop(module_name)
        visited.add(module_name)
        return None

    for module_name in sorted(graph):
        cycle = visit(module_name)
        if cycle is not None:
            return cycle
    return None


def test_product_shell_import_graph_is_acyclic() -> None:
    graph = _import_graph()
    cycle = _find_cycle(graph)
    assert cycle is None, (
        "canonical product shell imports must remain acyclic; "
        f"detected {' -> '.join(cycle or ())}; graph={graph!r}"
    )


def test_import_cycle_detector_is_non_vacuous() -> None:
    synthetic = {
        "product_runtime": frozenset({"product_entrypoint"}),
        "product_entrypoint": frozenset({"gui"}),
        "gui": frozenset({"product_runtime"}),
        "windows_gui": frozenset({"gui"}),
    }
    assert _find_cycle(synthetic) == (
        "gui",
        "product_runtime",
        "product_entrypoint",
        "gui",
    )

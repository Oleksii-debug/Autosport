from __future__ import annotations

import ast
from pathlib import Path

import autosport
from autosport.localization import catalog, require_keys


_PRODUCT_GUI_SOURCE = Path(autosport.__file__).with_name("product_windows_gui.py")


def _product_gui_tree() -> ast.Module:
    return ast.parse(_PRODUCT_GUI_SOURCE.read_text(encoding="utf-8"))


def _runtime_keys(tree: ast.AST) -> frozenset[str]:
    return frozenset(
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith("ui.product_runtime.")
    )


def test_packaged_product_gui_does_not_import_parallel_runtime_renderer() -> None:
    tree = _product_gui_tree()
    parallel_imports = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.level == 1
        and node.module == "localization_product_runtime"
    ]

    assert parallel_imports == []


def test_all_packaged_runtime_keys_are_owned_by_canonical_catalog() -> None:
    tree = _product_gui_tree()
    keys = _runtime_keys(tree)

    assert keys
    require_keys(keys)
    messages = catalog()
    assert all(key in messages for key in keys)
    assert all(type(messages[key]) is str and messages[key] for key in keys)


def test_packaged_runtime_surface_uses_canonical_localization_import() -> None:
    tree = _product_gui_tree()
    canonical_text_imports = [
        alias
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.level == 1
        and node.module == "localization"
        for alias in node.names
        if alias.name == "text"
    ]

    assert canonical_text_imports

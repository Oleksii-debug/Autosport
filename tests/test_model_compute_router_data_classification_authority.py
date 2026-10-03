"""Regression fence for product-owned cloud data-classification authority.

This is deliberately structural: the positive CLOUD path must bind the
classification assertion to the already-canonical VOC decision context rather
than trusting ComputeRouteRequest.data_classification by itself.
"""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ROUTER = ROOT / "src" / "autosport" / "model_compute_router.py"
VOC = ROOT / "src" / "autosport" / "voc_evaluation.py"


def _module(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"missing function: {name}")


def _string_keys(mapping: ast.Dict) -> set[str]:
    return {
        key.value
        for key in mapping.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


def test_canonical_voc_context_schema_carries_data_classification() -> None:
    tree = _module(VOC)
    assignments = {
        target.id: value
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
        for value in (node.value,)
    }

    matching_names = [
        name
        for name in assignments
        if name.startswith("_VOC_CURRENT_CONTEXT_FIELDS")
    ]
    assert matching_names, "canonical VOC current-context field authority is missing"

    literal_fields: set[str] = set()
    for name in matching_names:
        node = assignments[name]
        if isinstance(node, (ast.Set, ast.List, ast.Tuple)):
            literal_fields.update(
                element.value
                for element in node.elts
                if isinstance(element, ast.Constant)
                and isinstance(element.value, str)
            )

    assert "data_classification" in literal_fields, (
        "canonical VOC_ROUTE_CONTEXT must bind product-owned data_classification "
        "before CLOUD routing can rely on the classification"
    )


def test_cloud_route_matches_request_to_canonical_data_classification() -> None:
    tree = _module(ROUTER)
    route = _function(tree, "route_compute")

    expected_context_has_classification = False
    canonical_context_is_read = False

    for node in ast.walk(route):
        if isinstance(node, ast.Assign):
            if any(
                isinstance(target, ast.Name)
                and target.id == "expected_current_context"
                for target in node.targets
            ) and isinstance(node.value, ast.Dict):
                expected_context_has_classification = (
                    "data_classification" in _string_keys(node.value)
                )

        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (
                isinstance(node.func.value, ast.Name)
                and node.func.value.id == "current_context"
                and node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "data_classification"
            ):
                canonical_context_is_read = True

        if isinstance(node, ast.Subscript):
            if (
                isinstance(node.value, ast.Name)
                and node.value.id == "current_context"
                and isinstance(node.slice, ast.Constant)
                and node.slice.value == "data_classification"
            ):
                canonical_context_is_read = True

    assert expected_context_has_classification, (
        "positive CLOUD routing must bind caller classification into the "
        "canonical current VOC context identity"
    )
    assert canonical_context_is_read, (
        "positive CLOUD routing must re-resolve data_classification from the "
        "canonical current VOC context instead of trusting the request assertion"
    )

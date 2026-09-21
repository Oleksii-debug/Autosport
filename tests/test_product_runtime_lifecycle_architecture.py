from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_GUI_PATTERNS = (
    "src/autosport/*gui*.py",
    "src/autosport/windows_entry.py",
)
_RAW_RUNTIME_TYPE = "AutonomousProductRuntime"
_LIFECYCLE_METHODS = frozenset({"start", "tick", "stop", "close"})
_LEXICAL_BOUNDARIES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)


def _annotation_mentions(node: ast.expr | None, name: str) -> bool:
    if node is None:
        return False
    return any(
        (isinstance(item, ast.Name) and item.id == name)
        or (isinstance(item, ast.Attribute) and item.attr == name)
        for item in ast.walk(node)
    )


def _receiver_key(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _receiver_key(node.value)
        if parent is None:
            return None
        return f"{parent}.{node.attr}"
    return None


def _walk_lexical(scope: ast.AST) -> Iterator[ast.AST]:
    """Yield descendants owned by one lexical scope, excluding nested scopes."""
    stack = list(reversed(list(ast.iter_child_nodes(scope))))
    while stack:
        node = stack.pop()
        if isinstance(node, _LEXICAL_BOUNDARIES):
            continue
        yield node
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _typed_parameter_targets(
    scope: ast.FunctionDef | ast.AsyncFunctionDef,
) -> set[str]:
    args = [
        *scope.args.posonlyargs,
        *scope.args.args,
        *scope.args.kwonlyargs,
    ]
    if scope.args.vararg is not None:
        args.append(scope.args.vararg)
    if scope.args.kwarg is not None:
        args.append(scope.args.kwarg)
    return {
        arg.arg
        for arg in args
        if _annotation_mentions(arg.annotation, _RAW_RUNTIME_TYPE)
    }


def _typed_local_targets(
    scope: ast.FunctionDef | ast.AsyncFunctionDef,
) -> set[str]:
    targets: set[str] = set()
    for node in _walk_lexical(scope):
        if not isinstance(node, ast.AnnAssign):
            continue
        if not _annotation_mentions(node.annotation, _RAW_RUNTIME_TYPE):
            continue
        key = _receiver_key(node.target)
        if key is not None:
            targets.add(key)
    return targets


def _class_runtime_attributes(scope: ast.ClassDef) -> set[str]:
    """Collect typed raw-runtime attributes shared by methods of one class."""
    attributes: set[str] = set()
    for statement in scope.body:
        if isinstance(statement, ast.AnnAssign) and _annotation_mentions(
            statement.annotation, _RAW_RUNTIME_TYPE
        ):
            key = _receiver_key(statement.target)
            if key is not None and "." not in key:
                attributes.add(key)

        if not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for node in _walk_lexical(statement):
            if not isinstance(node, ast.AnnAssign):
                continue
            if not _annotation_mentions(node.annotation, _RAW_RUNTIME_TYPE):
                continue
            key = _receiver_key(node.target)
            if key is None:
                continue
            if key.startswith("self."):
                attributes.add(key.removeprefix("self."))
            elif key.startswith("cls."):
                attributes.add(key.removeprefix("cls."))
    return attributes


def _function_scopes(
    node: ast.AST,
    *,
    owner_class: ast.ClassDef | None = None,
) -> Iterator[
    tuple[ast.FunctionDef | ast.AsyncFunctionDef, ast.ClassDef | None]
]:
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.ClassDef):
            yield from _function_scopes(child, owner_class=child)
            continue
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            method_owner = owner_class
            yield child, method_owner
            # Nested functions are separate lexical scopes. They must not inherit
            # sibling/local type declarations from the containing method.
            yield from _function_scopes(child, owner_class=None)
            continue
        yield from _function_scopes(child, owner_class=owner_class)


def _raw_runtime_lifecycle_violations(source: str) -> tuple[tuple[int, str], ...]:
    tree = ast.parse(source)
    violations: list[tuple[int, str]] = []
    class_targets = {
        id(node): _class_runtime_attributes(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
    }

    for scope, owner_class in _function_scopes(tree):
        raw_targets = _typed_parameter_targets(scope) | _typed_local_targets(scope)
        if owner_class is not None:
            for attribute in class_targets[id(owner_class)]:
                raw_targets.add(f"self.{attribute}")
                raw_targets.add(f"cls.{attribute}")
        if not raw_targets:
            continue

        for node in _walk_lexical(scope):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in _LIFECYCLE_METHODS:
                continue
            receiver = _receiver_key(node.func.value)
            if receiver in raw_targets:
                violations.append((node.lineno, f"{receiver}.{node.func.attr}"))

    return tuple(sorted(set(violations)))


def _gui_sources() -> tuple[Path, ...]:
    found: set[Path] = set()
    for pattern in _GUI_PATTERNS:
        found.update(_REPO_ROOT.glob(pattern))
    return tuple(sorted(found))


def test_detector_rejects_direct_typed_raw_runtime_lifecycle_calls() -> None:
    direct = """
def run() -> None:
    runtime: AutonomousProductRuntime | None = None
    runtime = build_runtime()
    runtime.start()
    runtime.tick()
    runtime.stop("operator_stop")
    runtime.close()
"""
    assert _raw_runtime_lifecycle_violations(direct) == (
        (5, "runtime.start"),
        (6, "runtime.tick"),
        (7, "runtime.stop"),
        (8, "runtime.close"),
    )

    controller_owned = """
def run() -> None:
    runtime: AutonomousProductRuntime | None = None
    runtime = build_runtime()
    controller = ProductOperatorController(runtime)
    controller.start()
    controller.tick()
    controller.stop("operator_stop")
    controller.close()
"""
    assert _raw_runtime_lifecycle_violations(controller_owned) == ()


def test_detector_rejects_typed_parameters() -> None:
    typed_parameter = """
def run(runtime: AutonomousProductRuntime) -> None:
    runtime.start()
    runtime.tick()
"""
    assert _raw_runtime_lifecycle_violations(typed_parameter) == (
        (3, "runtime.start"),
        (4, "runtime.tick"),
    )


def test_detector_carries_typed_runtime_attributes_across_class_methods() -> None:
    cross_method = """
class Worker:
    def __init__(self) -> None:
        self.runtime: AutonomousProductRuntime | None = None

    def run(self) -> None:
        self.runtime.start()
        self.runtime.close()
"""
    assert _raw_runtime_lifecycle_violations(cross_method) == (
        (7, "self.runtime.start"),
        (8, "self.runtime.close"),
    )

    class_attribute = """
class Worker:
    runtime: AutonomousProductRuntime | None

    @classmethod
    def stop(cls) -> None:
        cls.runtime.stop("operator_stop")
"""
    assert _raw_runtime_lifecycle_violations(class_attribute) == (
        (7, "cls.runtime.stop"),
    )


def test_detector_keeps_sibling_and_nested_lexical_types_isolated() -> None:
    sibling_reuse = """
def typed_only() -> None:
    runtime: AutonomousProductRuntime | None = None

def unrelated() -> None:
    runtime = other()
    runtime.start()
"""
    assert _raw_runtime_lifecycle_violations(sibling_reuse) == ()

    nested_reuse = """
def outer() -> None:
    runtime = unrelated()
    runtime.start()

    def nested() -> None:
        runtime: AutonomousProductRuntime | None = None
        runtime.start()
"""
    assert _raw_runtime_lifecycle_violations(nested_reuse) == (
        (8, "runtime.start"),
    )


def test_gui_layers_do_not_own_typed_raw_product_runtime_lifecycle() -> None:
    violations: list[str] = []
    for path in _gui_sources():
        source = path.read_text(encoding="utf-8")
        for lineno, call in _raw_runtime_lifecycle_violations(source):
            relative = path.relative_to(_REPO_ROOT).as_posix()
            violations.append(f"{relative}:{lineno}: direct raw lifecycle call {call}")

    assert not violations, (
        "GUI/Windows layers must not directly drive a typed AutonomousProductRuntime. "
        "Keep thread/poll/message ownership in the GUI worker, but delegate synchronous "
        "start/tick/stop/close semantics through the canonical product operator "
        "controller.\n"
        + "\n".join(violations)
    )

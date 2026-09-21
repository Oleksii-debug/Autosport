from __future__ import annotations

import ast
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_GUI_PATTERNS = (
    "src/autosport/*gui*.py",
    "src/autosport/windows_entry.py",
)
_RAW_RUNTIME_TYPE = "AutonomousProductRuntime"
_LIFECYCLE_METHODS = frozenset({"start", "tick", "stop", "close"})


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


def _typed_raw_runtime_targets(scope: ast.AST) -> set[str]:
    targets: set[str] = set()
    for node in ast.walk(scope):
        if not isinstance(node, ast.AnnAssign):
            continue
        if not _annotation_mentions(node.annotation, _RAW_RUNTIME_TYPE):
            continue
        key = _receiver_key(node.target)
        if key is not None:
            targets.add(key)
    return targets


def _raw_runtime_lifecycle_violations(source: str) -> tuple[tuple[int, str], ...]:
    tree = ast.parse(source)
    violations: list[tuple[int, str]] = []
    scopes = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for scope in scopes:
        raw_targets = _typed_raw_runtime_targets(scope)
        if not raw_targets:
            continue
        for node in ast.walk(scope):
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

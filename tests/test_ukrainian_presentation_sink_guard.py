from __future__ import annotations

import ast
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_TARGETS = (
    Path("src/autosport/gui.py"),
    Path("src/autosport/windows_gui.py"),
)

# These call sites are direct user/NVDA presentation boundaries. Product-owned
# copy at these sinks must come from the canonical localization catalog rather
# than a source literal. Empty strings are allowed for deliberately blank values.
_TWO_POSITIONAL_PRESENTATION_ARGS = {
    "showerror": (0, 1),
    "showwarning": (0, 1),
    "showinfo": (0, 1),
    "askokcancel": (0, 1),
    "askyesno": (0, 1),
}
_SECOND_POSITIONAL_PRESENTATION_ARGS = {
    "set_acc_name",
    "set_acc_description",
    "insert",
}
_FIRST_POSITIONAL_PRESENTATION_ARGS = {
    "title",
    "set",
}
_PRESENTATION_KEYWORDS = {
    "text",
    "title",
}


def _call_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _direct_literal(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if node.value.strip():
            return repr(node.value)
        return None
    if isinstance(node, ast.JoinedStr):
        return "f-string"
    return None


def _presentation_nodes(call: ast.Call) -> list[tuple[str, ast.AST]]:
    name = _call_name(call)
    found: list[tuple[str, ast.AST]] = []

    if name in _TWO_POSITIONAL_PRESENTATION_ARGS:
        for index in _TWO_POSITIONAL_PRESENTATION_ARGS[name]:
            if len(call.args) > index:
                found.append((f"{name} arg#{index + 1}", call.args[index]))

    if name in _SECOND_POSITIONAL_PRESENTATION_ARGS and len(call.args) > 1:
        found.append((f"{name} arg#2", call.args[1]))

    if name in _FIRST_POSITIONAL_PRESENTATION_ARGS and call.args:
        found.append((f"{name} arg#1", call.args[0]))

    for keyword in call.keywords:
        if keyword.arg in _PRESENTATION_KEYWORDS:
            found.append((f"{name or '<call>'} {keyword.arg}=", keyword.value))

    return found


def _direct_presentation_literals(path: Path) -> list[str]:
    source = (_REPO_ROOT / path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for sink, value in _presentation_nodes(node):
            literal = _direct_literal(value)
            if literal is not None:
                violations.append(f"{path}:{node.lineno}: {sink} -> {literal}")

    return violations


def test_critical_gui_presentation_sinks_do_not_bypass_localization_catalog() -> None:
    violations = [
        violation
        for path in _TARGETS
        for violation in _direct_presentation_literals(path)
    ]

    assert violations == [], (
        "Critical GUI/UIA presentation must come through autosport.localization.text(); "
        "direct string/f-string sinks can silently reintroduce English or bypass the "
        "uk-UA catalog. Raw domain/provider identifiers and internal diagnostics remain "
        "outside this presentation-only fence.\n"
        + "\n".join(violations)
    )

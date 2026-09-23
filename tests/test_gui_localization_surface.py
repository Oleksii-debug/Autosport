from __future__ import annotations

import ast
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[1]
_GUI_PATH = _REPO_ROOT / "src" / "autosport" / "gui.py"

_MESSAGEBOX_CALLS = {
    "messagebox.showerror",
    "messagebox.showinfo",
    "messagebox.showwarning",
    "messagebox.askokcancel",
    "messagebox.askquestion",
    "messagebox.askretrycancel",
    "messagebox.askyesno",
    "messagebox.askyesnocancel",
}
_FILEDIALOG_CALLS = {
    "filedialog.askdirectory",
    "filedialog.askopenfilename",
    "filedialog.askopenfilenames",
    "filedialog.asksaveasfilename",
}
_TEXT_WIDGET_CALLS = {
    "tk.Button",
    "tk.Checkbutton",
    "tk.Label",
    "tk.LabelFrame",
    "tk.Radiobutton",
    "ttk.Button",
    "ttk.Checkbutton",
    "ttk.Label",
    "ttk.LabelFrame",
    "ttk.Radiobutton",
}
_VISIBLE_STRINGVARS = {
    "bank",
    "dataset_text",
    "live_status",
    "log_value",
    "research_plan_text",
    "status",
    "strategy_status",
}
_VISIBLE_METHODS = {
    "_append_log": 0,
    "_set_evaluation_lines": 0,
    "_set_live_lines": 0,
}
_VISIBLE_INSERT_WIDGETS = {
    "evaluation",
    "live_quotes",
    "log",
    "tickets",
}


def _call_name(node: ast.expr) -> str | None:
    parts: list[str] = []
    current: ast.expr = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


def _is_text_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "text"
    )


def _contains_text_call(node: ast.AST) -> bool:
    return any(_is_text_call(candidate) for candidate in ast.walk(node))


def _contains_nonempty_string_literal(node: ast.AST) -> bool:
    return any(
        isinstance(candidate, ast.Constant)
        and isinstance(candidate.value, str)
        and bool(candidate.value.strip())
        for candidate in ast.walk(node)
    )


def _self_attribute_name(node: ast.AST) -> str | None:
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    ):
        return node.attr
    return None


def _keyword(call: ast.Call, name: str) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _presentation_arguments(call: ast.Call) -> list[tuple[str, ast.expr]]:
    call_name = _call_name(call.func)
    arguments: list[tuple[str, ast.expr]] = []

    if call_name in _TEXT_WIDGET_CALLS:
        value = _keyword(call, "text")
        if value is not None:
            arguments.append(("widget text", value))

    if call_name in _MESSAGEBOX_CALLS:
        if len(call.args) >= 1:
            arguments.append(("dialog title", call.args[0]))
        if len(call.args) >= 2:
            arguments.append(("dialog message", call.args[1]))
        for keyword_name in ("title", "message"):
            value = _keyword(call, keyword_name)
            if value is not None:
                arguments.append((f"dialog {keyword_name}", value))

    if call_name in _FILEDIALOG_CALLS:
        value = _keyword(call, "title")
        if value is not None:
            arguments.append(("file dialog title", value))

    if call_name == "tk.StringVar":
        value = _keyword(call, "value")
        if value is not None:
            arguments.append(("StringVar initial value", value))

    if (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "title"
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "self"
        and call.args
    ):
        arguments.append(("window title", call.args[0]))

    if isinstance(call.func, ast.Attribute) and call.func.attr == "set" and call.args:
        variable_name = _self_attribute_name(call.func.value)
        if variable_name in _VISIBLE_STRINGVARS:
            arguments.append((f"{variable_name}.set", call.args[0]))

    if (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "self"
        and call.func.attr in _VISIBLE_METHODS
    ):
        index = _VISIBLE_METHODS[call.func.attr]
        if len(call.args) > index:
            arguments.append((call.func.attr, call.args[index]))

    if (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "insert"
        and len(call.args) >= 2
    ):
        widget_name = _self_attribute_name(call.func.value)
        if widget_name in _VISIBLE_INSERT_WIDGETS:
            arguments.append((f"{widget_name}.insert", call.args[1]))

    return arguments


def _format_violation(node: ast.AST, sink: str) -> str:
    return f"line {getattr(node, 'lineno', '?')}: {sink}"


def test_gui_presentation_sinks_do_not_hardcode_nonempty_visible_text() -> None:
    """Visible Windows/Tk strings must flow through localization, not new literals."""

    tree = ast.parse(_GUI_PATH.read_text(encoding="utf-8"), filename=str(_GUI_PATH))
    violations: list[str] = []

    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        for sink, argument in _presentation_arguments(call):
            if _contains_nonempty_string_literal(argument) and not _contains_text_call(argument):
                violations.append(_format_violation(argument, sink))

    assert violations == [], (
        "User-visible GUI text bypasses autosport.localization.text(...): "
        + "; ".join(sorted(violations))
    )


def test_uia_names_and_descriptions_are_sourced_from_localization_catalog() -> None:
    """The accessibility tuple is itself a presentation boundary, not raw metadata."""

    tree = ast.parse(_GUI_PATH.read_text(encoding="utf-8"), filename=str(_GUI_PATH))
    autosport_app = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "AutosportApp"
    )
    configure = next(
        node
        for node in autosport_app.body
        if isinstance(node, ast.FunctionDef) and node.name == "_configure_accessibility"
    )

    controls_value: ast.expr | None = None
    for statement in configure.body:
        if not isinstance(statement, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == "controls" for target in statement.targets):
            controls_value = statement.value
            break

    assert isinstance(controls_value, (ast.Tuple, ast.List))
    assert controls_value.elts, "Accessibility controls tuple must not be empty."

    violations: list[str] = []
    for index, row in enumerate(controls_value.elts):
        if not isinstance(row, (ast.Tuple, ast.List)) or len(row.elts) < 4:
            violations.append(f"row {index}: malformed accessibility tuple")
            continue
        name_expression = row.elts[1]
        description_expression = row.elts[2]
        if not _is_text_call(name_expression):
            violations.append(f"row {index}: accessible name is not text(...)")
        if not _is_text_call(description_expression):
            violations.append(f"row {index}: accessible description is not text(...)")

    assert violations == [], "; ".join(violations)

    call_names = {
        _call_name(call.func)
        for call in ast.walk(configure)
        if isinstance(call, ast.Call)
    }
    assert "tk_uia.set_acc_name" in call_names
    assert "tk_uia.set_acc_description" in call_names

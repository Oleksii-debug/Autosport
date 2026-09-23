from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PRESENTATION_SOURCES = (
    REPO_ROOT / "src" / "autosport" / "gui.py",
    REPO_ROOT / "src" / "autosport" / "windows_gui.py",
)

_WIDGET_TEXT_CONSTRUCTORS = {
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
_MESSAGEBOX_CALLS = {
    "messagebox.askokcancel",
    "messagebox.askquestion",
    "messagebox.askretrycancel",
    "messagebox.askyesno",
    "messagebox.askyesnocancel",
    "messagebox.showerror",
    "messagebox.showinfo",
    "messagebox.showwarning",
}
_FILEDIALOG_CALLS = {
    "filedialog.askdirectory",
    "filedialog.askopenfile",
    "filedialog.askopenfilename",
    "filedialog.askopenfilenames",
    "filedialog.asksaveasfile",
    "filedialog.asksaveasfilename",
}
_UIA_TEXT_CALLS = {
    "tk_uia.set_acc_description",
    "tk_uia.set_acc_name",
}
_MENU_CALLS = {
    "add_cascade",
    "add_checkbutton",
    "add_command",
    "add_radiobutton",
}
_PRESENTATION_SET_CALLS = {
    "self.bank.set",
    "self.dataset_text.set",
    "self.live_mode_text.set",
    "self.live_status.set",
    "self.log_value.set",
    "self.research_plan_text.set",
    "self.speed_text.set",
    "self.status.set",
    "self.strategy_status.set",
    "self.strategy_text.set",
}
_PRESENTATION_INSERT_CALLS = {
    "self.evaluation.insert",
    "self.live_quotes.insert",
    "self.log.insert",
    "self.tickets.insert",
}
_PRESENTATION_LINE_CALLS = {
    "self._set_evaluation_lines",
    "self._set_live_lines",
}


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        owner = _call_name(node.value)
        return f"{owner}.{node.attr}" if owner else node.attr
    return ""


def _literal_user_text(node: ast.AST | None) -> str | None:
    """Return a stable description only for direct presentation text literals.

    Empty strings and punctuation-only literals are intentionally allowed. They are
    layout/placeholder values, not translatable resources. Dynamic values are also
    allowed because this gate is specifically about bypassing the localization
    authority with hard-coded human-language presentation text.

    Common Python string composition forms are inspected recursively because a
    literal prefix/suffix remains a direct presentation resource when combined with
    dynamic content. Arbitrary calls remain opaque boundaries, so catalog-backed
    ``text(...)`` values are not mistaken for localization-key string arguments.
    """

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        value = node.value
        if any(character.isalpha() for character in value):
            return repr(value)
        return None
    if isinstance(node, ast.JoinedStr):
        literal_parts = "".join(
            value.value
            for value in node.values
            if isinstance(value, ast.Constant) and isinstance(value.value, str)
        )
        if any(character.isalpha() for character in literal_parts):
            return "f-string"
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        if _literal_user_text(node.left) is not None or _literal_user_text(node.right) is not None:
            return "string-concatenation"
        return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
        if _literal_user_text(node.left) is not None:
            return "percent-format"
        return None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if (
            node.func.attr in {"format", "format_map"}
            and _literal_user_text(node.func.value) is not None
        ):
            return f"string-{node.func.attr}"
        return None
    if isinstance(node, ast.IfExp):
        if _literal_user_text(node.body) is not None or _literal_user_text(node.orelse) is not None:
            return "conditional-string"
    return None


def _keyword(call: ast.Call, name: str) -> ast.AST | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _sequence_items(node: ast.AST | None) -> tuple[ast.AST, ...]:
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return tuple(node.elts)
    return ()


def _presentation_candidates(call: ast.Call) -> list[tuple[str, ast.AST | None]]:
    qualified = _call_name(call.func)
    method = qualified.rsplit(".", 1)[-1]
    candidates: list[tuple[str, ast.AST | None]] = []

    if qualified in _WIDGET_TEXT_CONSTRUCTORS:
        candidates.append(("widget.text", _keyword(call, "text")))

    if qualified == "ttk.Combobox":
        for value in _sequence_items(_keyword(call, "values")):
            candidates.append(("combobox.value", value))

    if qualified in {"tk.StringVar", "StringVar"}:
        candidates.append(("stringvar.value", _keyword(call, "value")))

    if method in {"config", "configure"}:
        candidates.append(("configure.text", _keyword(call, "text")))

    if method == "title" and call.args:
        candidates.append(("window.title", call.args[0]))

    if qualified in _MESSAGEBOX_CALLS:
        if call.args:
            candidates.append(("messagebox.title", call.args[0]))
        if len(call.args) > 1:
            candidates.append(("messagebox.message", call.args[1]))
        candidates.append(("messagebox.title", _keyword(call, "title")))
        candidates.append(("messagebox.message", _keyword(call, "message")))
        candidates.append(("messagebox.detail", _keyword(call, "detail")))

    if qualified in _FILEDIALOG_CALLS:
        candidates.append(("filedialog.title", _keyword(call, "title")))
        for filetype in _sequence_items(_keyword(call, "filetypes")):
            filetype_parts = _sequence_items(filetype)
            if filetype_parts:
                candidates.append(("filedialog.filetype_label", filetype_parts[0]))

    if qualified in _UIA_TEXT_CALLS and len(call.args) > 1:
        candidates.append((qualified, call.args[1]))

    if method in _MENU_CALLS:
        candidates.append(("menu.label", _keyword(call, "label")))

    if qualified in _PRESENTATION_SET_CALLS and call.args:
        candidates.append((qualified, call.args[0]))

    if qualified in _PRESENTATION_INSERT_CALLS and len(call.args) > 1:
        candidates.append((qualified, call.args[1]))

    if qualified == "self._append_log" and call.args:
        candidates.append(("self._append_log", call.args[0]))

    if qualified in _PRESENTATION_LINE_CALLS and call.args:
        for line in _sequence_items(call.args[0]):
            candidates.append((qualified, line))

    return candidates


def _find_presentation_literal_leaks(source: str, *, filename: str) -> list[str]:
    tree = ast.parse(source, filename=filename)
    leaks: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for sink, candidate in _presentation_candidates(node):
            literal = _literal_user_text(candidate)
            if literal is not None:
                leaks.append(f"{filename}:{node.lineno}: {sink} -> {literal}")
    return leaks


def test_scanner_detects_direct_user_facing_literals_without_flagging_catalog_calls() -> None:
    synthetic = '''
from tkinter import filedialog, messagebox, ttk
import tk_uia


def bad(self, root, control, detail, values, ok):
    ttk.Button(root, text="Run")
    messagebox.showerror("Error", "Operation failed", detail="Try again")
    filedialog.askopenfilename(
        title="Open dataset",
        filetypes=(("JSON files", "*.json"),),
    )
    tk_uia.set_acc_name(control, "Start action")
    self.status.set("Ready")
    self.tickets.insert("end", "Hard-coded status")
    self.log.insert("end", "Hard-coded log")
    self._append_log("Replay started")
    self._set_live_lines(["No live quotes"])
    self.status.set("Hard-coded status: " + detail)
    ttk.Label(root, text="Hard-coded prefix " + detail)
    messagebox.showerror(text("ui.error.title"), "Hard-coded error: " + detail)
    self.status.set("Hard-coded status: %s" % detail)
    self.status.set("Hard-coded status: {}".format(detail))
    self.status.set("Hard-coded status: {detail}".format_map(values))
    self.status.set("Ready" if ok else "Failed")
    ttk.Combobox(root, values=("English choice",), state="readonly")


def good(self, root, control, detail, values, ok, choices):
    ttk.Button(root, text=text("ui.button.run"))
    messagebox.showerror(text("ui.error.title"), text("ui.error.body"))
    filedialog.askopenfilename(
        title=text("ui.dialog.dataset.choose_title"),
        filetypes=((text("ui.filetype.json"), "*.json"),),
    )
    tk_uia.set_acc_name(control, text("ui.accessibility.start.name"))
    self.status.set(text("ui.status.ready"))
    self.tickets.insert("end", text("ui.ticket.status"))
    self.log.insert("end", text("ui.log.entry"))
    self._append_log(text("ui.log.replay.started"))
    self._set_live_lines([text("ui.status.live_quotes.empty")])
    self.status.set(text("ui.status.detail", detail=detail))
    ttk.Label(root, text=text("ui.label.detail", detail=detail))
    messagebox.showerror(text("ui.error.title"), text("ui.error.detail", detail=detail))
    self.status.set(text("ui.status.percent", detail=detail))
    self.status.set(text("ui.status.format", detail=detail))
    self.status.set(text("ui.status.map", detail=detail))
    self.status.set(text("ui.status.ready") if ok else text("ui.status.failed"))
    ttk.Combobox(root, values=(text("ui.choice"),), state="readonly")
    ttk.Combobox(root, values=choices, state="readonly")
'''

    leaks = _find_presentation_literal_leaks(synthetic, filename="synthetic.py")

    assert len(leaks) == 20
    assert any("widget.text -> 'Run'" in leak for leak in leaks)
    assert any("messagebox.title -> 'Error'" in leak for leak in leaks)
    assert any("messagebox.message -> 'Operation failed'" in leak for leak in leaks)
    assert any("messagebox.detail -> 'Try again'" in leak for leak in leaks)
    assert any("filedialog.title -> 'Open dataset'" in leak for leak in leaks)
    assert any("filedialog.filetype_label -> 'JSON files'" in leak for leak in leaks)
    assert any("tk_uia.set_acc_name -> 'Start action'" in leak for leak in leaks)
    assert any("self.status.set -> 'Ready'" in leak for leak in leaks)
    assert any("self.tickets.insert -> 'Hard-coded status'" in leak for leak in leaks)
    assert any("self.log.insert -> 'Hard-coded log'" in leak for leak in leaks)
    assert any("self._append_log -> 'Replay started'" in leak for leak in leaks)
    assert any("self._set_live_lines -> 'No live quotes'" in leak for leak in leaks)
    assert any("self.status.set -> string-concatenation" in leak for leak in leaks)
    assert any("widget.text -> string-concatenation" in leak for leak in leaks)
    assert any("messagebox.message -> string-concatenation" in leak for leak in leaks)
    assert any("self.status.set -> percent-format" in leak for leak in leaks)
    assert any("self.status.set -> string-format" in leak for leak in leaks)
    assert any("self.status.set -> string-format_map" in leak for leak in leaks)
    assert any("self.status.set -> conditional-string" in leak for leak in leaks)
    assert any("combobox.value -> 'English choice'" in leak for leak in leaks)


def test_desktop_presentation_text_is_owned_by_localization_resources() -> None:
    """Prevent Tk/UIA presentation strings from escaping the locale catalog.

    This deliberately scans presentation sinks rather than every source string.
    Provider/domain identifiers, internal exception diagnostics and protocol values
    are language-neutral or developer-facing and therefore stay outside this
    boundary. The localization catalog itself is the authority and is not scanned.
    """

    leaks: list[str] = []
    for path in PRESENTATION_SOURCES:
        leaks.extend(
            _find_presentation_literal_leaks(
                path.read_text(encoding="utf-8"),
                filename=str(path.relative_to(REPO_ROOT)),
            )
        )

    assert not leaks, (
        "Hard-coded user-facing text bypasses autosport.localization resources:\n"
        + "\n".join(leaks)
    )

def _uia_controls_tuple_resource_violations(
    source: str,
    *,
    filename: str,
) -> list[str]:
    """Require tuple-carried UIA names/descriptions to stay catalog-owned."""

    tree = ast.parse(source, filename=filename)
    violations: list[str] = []
    controls_assignment_found = False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "_configure_accessibility":
            continue
        for statement in node.body:
            if not isinstance(statement, ast.Assign):
                continue
            if not any(
                isinstance(target, ast.Name) and target.id == "controls"
                for target in statement.targets
            ):
                continue
            controls_assignment_found = True
            if not isinstance(statement.value, ast.Tuple):
                violations.append(
                    f"{filename}:{statement.lineno}: controls must be a tuple"
                )
                continue
            for row_index, row in enumerate(statement.value.elts):
                if not isinstance(row, ast.Tuple) or len(row.elts) != 4:
                    violations.append(
                        f"{filename}:{statement.lineno}: "
                        f"controls[{row_index}] must contain 4 fields"
                    )
                    continue
                for field_index, field_name in ((1, "name"), (2, "description")):
                    value = row.elts[field_index]
                    if not (
                        isinstance(value, ast.Call)
                        and _call_name(value.func) == "text"
                    ):
                        violations.append(
                            f"{filename}:{getattr(value, 'lineno', statement.lineno)}: "
                            f"controls[{row_index}].{field_name} must use text(...)"
                        )
    if not controls_assignment_found:
        violations.append(
            f"{filename}: _configure_accessibility controls tuple is missing"
        )
    return violations


def test_uia_controls_tuple_gate_rejects_non_catalog_name_or_description() -> None:
    synthetic_bad = """
def _configure_accessibility(self):
    controls = (
        (self.run_button, "Run replay", text("ui.run.description"), 102),
        (self.log, text("ui.log.name"), hardcoded_description, 202),
    )
"""
    violations = _uia_controls_tuple_resource_violations(
        synthetic_bad,
        filename="synthetic_bad.py",
    )

    assert len(violations) == 2
    assert any("controls[0].name must use text(...)" in item for item in violations)
    assert any(
        "controls[1].description must use text(...)" in item
        for item in violations
    )


def test_uia_controls_tuple_names_and_descriptions_are_catalog_owned() -> None:
    path = REPO_ROOT / "src" / "autosport" / "gui.py"
    violations = _uia_controls_tuple_resource_violations(
        path.read_text(encoding="utf-8"),
        filename=str(path.relative_to(REPO_ROOT)),
    )

    assert not violations, (
        "UIA controls tuple bypasses autosport.localization resources:\n"
        + "\n".join(violations)
    )


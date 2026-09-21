from __future__ import annotations

import ast
from pathlib import Path


_MESSAGEBOX_METHODS = {
    "showerror",
    "showwarning",
    "showinfo",
    "askyesno",
    "askokcancel",
    "askretrycancel",
    "askquestion",
    "askyesnocancel",
}

_GUI_SOURCES = (
    Path("src/autosport/gui.py"),
    Path("src/autosport/windows_gui.py"),
)


def _messagebox_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id == "messagebox"
            and function.attr in _MESSAGEBOX_METHODS
        ):
            continue
        calls.append(node)
    return calls


def test_canonical_gui_messageboxes_are_owned_by_the_app_window() -> None:
    total = 0
    for path in _GUI_SOURCES:
        calls = _messagebox_calls(path)
        assert calls, f"{path}: expected canonical messagebox coverage"
        total += len(calls)
        for call in calls:
            parent_values = [
                keyword.value
                for keyword in call.keywords
                if keyword.arg == "parent"
            ]
            assert len(parent_values) == 1, (
                f"{path}:{call.lineno}: messagebox must bind exactly one parent"
            )
            parent = parent_values[0]
            assert isinstance(parent, ast.Name) and parent.id == "self", (
                f"{path}:{call.lineno}: messagebox parent must be the owning app window"
            )

    assert total >= 2

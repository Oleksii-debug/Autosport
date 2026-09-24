from __future__ import annotations

import ast
from pathlib import Path


_FILE_DIALOG_METHODS = {
    "askdirectory",
    "askopenfile",
    "askopenfilename",
    "askopenfilenames",
    "askopenfiles",
    "asksaveasfile",
    "asksaveasfilename",
}

_GUI_SOURCE = Path("src/autosport/gui.py")


def _file_dialog_calls(path: Path) -> list[ast.Call]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id == "filedialog"
            and function.attr in _FILE_DIALOG_METHODS
        ):
            continue
        calls.append(node)
    return calls


def test_canonical_file_dialogs_are_owned_by_the_app_window() -> None:
    calls = _file_dialog_calls(_GUI_SOURCE)
    assert len(calls) >= 3, "expected canonical file-dialog coverage"

    for call in calls:
        parent_values = [
            keyword.value
            for keyword in call.keywords
            if keyword.arg == "parent"
        ]
        assert len(parent_values) == 1, (
            f"{_GUI_SOURCE}:{call.lineno}: file dialog must bind exactly one parent"
        )
        parent = parent_values[0]
        assert isinstance(parent, ast.Name) and parent.id == "self", (
            f"{_GUI_SOURCE}:{call.lineno}: file-dialog parent must be the owning app window"
        )

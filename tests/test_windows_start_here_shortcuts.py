from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
KEYBOARD_AUDIT = ROOT / "src" / "autosport" / "keyboard_audit.py"
WINDOWS_GUIDE = ROOT / "WINDOWS_START_HERE.txt"


def _action_bindings() -> dict[str, str]:
    tree = ast.parse(KEYBOARD_AUDIT.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "_ACTION_BINDINGS" for target in node.targets):
            continue
        value = ast.literal_eval(node.value)
        assert isinstance(value, dict)
        return value
    raise AssertionError("keyboard_audit._ACTION_BINDINGS not found")


def test_windows_start_here_documents_gui_recovery_shortcut() -> None:
    bindings = _action_bindings()
    assert bindings.get("<Control-Shift-R>") == "repair_workspace"

    guide = WINDOWS_GUIDE.read_text(encoding="utf-8")
    assert "Ctrl+Shift+R" in guide
    assert "Ctrl+Shift+R — запустити fail-closed відновлення поточного workspace через GUI" in guide

from __future__ import annotations

import inspect
from pathlib import Path

import autosport.keyboard_audit as base_keyboard_audit
import autosport.windows_keyboard_audit as packaged_keyboard_audit
from autosport.windows_accessible_gui import (
    OPERATIONAL_STATUS_FOCUS_SHORTCUT,
    AccessibleWindowsAutosportApp,
)


def test_operational_status_has_unambiguous_f5_focus_binding() -> None:
    assert OPERATIONAL_STATUS_FOCUS_SHORTCUT == "<F5>"
    configure_source = inspect.getsource(
        AccessibleWindowsAutosportApp._configure_accessibility
    )
    focus_source = inspect.getsource(
        AccessibleWindowsAutosportApp._focus_operational_status
    )
    assert "OPERATIONAL_STATUS_FOCUS_SHORTCUT" in configure_source
    assert "self._focus_operational_status" in configure_source
    assert "self.operational_status.focus_set()" in focus_source


def test_packaged_keyboard_gate_temporarily_requires_f5_shortcut(
    monkeypatch,
    tmp_path: Path,
) -> None:
    shortcut = OPERATIONAL_STATUS_FOCUS_SHORTCUT
    previous_target = base_keyboard_audit._FOCUS_BINDINGS.get(shortcut)
    had_binding = shortcut in base_keyboard_audit._FOCUS_BINDINGS
    observed: dict[str, object] = {}

    def fake_run(output_path: str | Path) -> int:
        observed["path"] = Path(output_path)
        observed["target"] = base_keyboard_audit._FOCUS_BINDINGS.get(shortcut)
        return 0

    monkeypatch.setattr(base_keyboard_audit, "run_keyboard_audit", fake_run)
    destination = tmp_path / "keyboard.json"
    assert packaged_keyboard_audit.run_keyboard_audit(destination) == 0
    assert observed == {"path": destination, "target": "operational_status"}

    assert (shortcut in base_keyboard_audit._FOCUS_BINDINGS) is had_binding
    if had_binding:
        assert base_keyboard_audit._FOCUS_BINDINGS[shortcut] == previous_target

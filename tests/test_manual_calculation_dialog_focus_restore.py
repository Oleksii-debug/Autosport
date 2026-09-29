from __future__ import annotations

import ast
from collections.abc import Callable
import inspect
import textwrap

import autosport.windows_manual_calculation as workbench


class _FocusButton:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def focus_set(self) -> None:
        self.events.append("focus")


class _App:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.manual_calculation_button = _FocusButton(events)
        self.idle_callbacks: list[Callable[[], None]] = []

    def after_idle(self, callback: Callable[[], None]) -> None:
        self.events.append("schedule")
        self.idle_callbacks.append(callback)


class _Dialog:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.protocols: dict[str, Callable[[], None]] = {}

    def destroy(self) -> None:
        self.events.append("destroy")

    def protocol(self, name: str, callback: Callable[[], None]) -> None:
        self.protocols[name] = callback


def test_manual_workbench_close_restores_focus_after_dialog_destruction() -> None:
    events: list[str] = []
    app = _App(events)
    dialog = _Dialog(events)

    close = workbench._manual_calculation_dialog_close_handler(app, dialog)
    close()

    assert events == ["destroy", "schedule"]
    assert len(app.idle_callbacks) == 1

    app.idle_callbacks.pop()()
    assert events == ["destroy", "schedule", "focus"]


def test_manual_workbench_window_manager_close_uses_same_focus_path() -> None:
    events: list[str] = []
    app = _App(events)
    dialog = _Dialog(events)

    close = workbench._manual_calculation_dialog_close_handler(app, dialog)

    assert dialog.protocols["WM_DELETE_WINDOW"] is close
    dialog.protocols["WM_DELETE_WINDOW"]()
    assert events == ["destroy", "schedule"]

    app.idle_callbacks.pop()()
    assert events == ["destroy", "schedule", "focus"]


def test_manual_workbench_focus_restore_has_headless_scheduler_fallback() -> None:
    events: list[str] = []

    class _MinimalApp:
        manual_calculation_button = _FocusButton(events)

    dialog = _Dialog(events)
    close = workbench._manual_calculation_dialog_close_handler(_MinimalApp(), dialog)

    close()

    assert events == ["destroy", "focus"]


def test_workbench_close_button_uses_focus_restoring_handler() -> None:
    source = textwrap.dedent(inspect.getsource(workbench.show_manual_calculation_workbench))
    tree = ast.parse(source)
    close_commands = 0
    direct_destroy_commands = 0

    for node in ast.walk(tree):
        if not isinstance(node, ast.keyword) or node.arg != "command":
            continue
        value = node.value
        if isinstance(value, ast.Name) and value.id == "close_dialog":
            close_commands += 1
        if (
            isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == "dialog"
            and value.attr == "destroy"
        ):
            direct_destroy_commands += 1

    assert close_commands == 1
    assert direct_destroy_commands == 0

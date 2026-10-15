from __future__ import annotations

import ast
from collections.abc import Callable
import inspect
import textwrap

import autosport.windows_layout as layout


class _FocusButton:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    def focus_set(self) -> None:
        self.events.append("focus")


class _App:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.owner_economic_authority_button = _FocusButton(events)
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


def test_owner_dialog_close_restores_focus_after_dialog_destruction() -> None:
    events: list[str] = []
    app = _App(events)
    dialog = _Dialog(events)

    close = layout._owner_economic_dialog_close_handler(app, dialog)
    close()

    assert events == ["destroy", "schedule"]
    assert len(app.idle_callbacks) == 1

    callback = app.idle_callbacks.pop()
    callback()

    assert events == ["destroy", "schedule", "focus"]


def test_window_manager_close_uses_the_same_focus_restoration_path() -> None:
    events: list[str] = []
    app = _App(events)
    dialog = _Dialog(events)

    close = layout._owner_economic_dialog_close_handler(app, dialog)

    assert dialog.protocols["WM_DELETE_WINDOW"] is close
    dialog.protocols["WM_DELETE_WINDOW"]()
    assert events == ["destroy", "schedule"]

    app.idle_callbacks.pop()()
    assert events == ["destroy", "schedule", "focus"]


def test_focus_restoration_has_a_headless_scheduler_fallback() -> None:
    events: list[str] = []

    class _MinimalApp:
        owner_economic_authority_button = _FocusButton(events)

    dialog = _Dialog(events)
    close = layout._owner_economic_dialog_close_handler(_MinimalApp(), dialog)

    close()

    assert events == ["destroy", "focus"]


def test_both_owner_dialog_close_buttons_use_the_focus_restoring_handler() -> None:
    source = textwrap.dedent(inspect.getsource(layout._show_owner_economic_dialog))
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

    assert close_commands == 2
    assert direct_destroy_commands == 0
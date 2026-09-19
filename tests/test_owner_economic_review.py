from __future__ import annotations

from decimal import Decimal

import autosport.windows_layout as layout
from autosport.economic_goal_store import EconomicGoalStore
from autosport.localization import text
from autosport.owner_economic_authority import (
    INITIAL_OWNER_FORM_DEFAULTS,
    OwnerEconomicReviewSnapshot,
)


def test_review_snapshot_preserves_the_exact_checked_form_without_writing(tmp_path):
    values = dict(INITIAL_OWNER_FORM_DEFAULTS)
    review = OwnerEconomicReviewSnapshot.from_form(values, emergency_stop=True)

    assert any("аварійна STOP: так" in line for line in review.lines_uk)
    assert review.still_matches(values, emergency_stop=True)
    assert not review.still_matches(values, emergency_stop=False)
    values["max_stake_fraction"] = "0.01"
    assert not review.still_matches(values, emergency_stop=True)
    assert review.form_values()["max_stake_fraction"] == "0.02"
    assert not (tmp_path / EconomicGoalStore.FILE_NAME).exists()


def _fake_dialog(monkeypatch, tmp_path):
    widgets = []
    variables = []
    errors = []

    class Variable:
        def __init__(self, value=None):
            self.value = value
            self.callbacks = []
            variables.append(self)

        def get(self):
            return self.value

        def set(self, value):
            self.value = value
            for callback in self.callbacks:
                callback("", "", "write")

        def trace_add(self, _mode, callback):
            self.callbacks.append(callback)

    class Widget:
        def __init__(self, _parent=None, **kwargs):
            self.options = kwargs
            self.values = []
            self.focused = False
            self.destroyed = False
            widgets.append(self)

        def pack(self, **_kwargs):
            pass

        def grid(self, **_kwargs):
            pass

        def title(self, _title):
            pass

        def transient(self, _parent):
            pass

        def geometry(self, _value):
            pass

        def minsize(self, *_size):
            pass

        def bind(self, *_args):
            pass

        def configure(self, **kwargs):
            self.options.update(kwargs)

        def insert(self, _where, value):
            self.values.append(value)

        def delete(self, *_args):
            self.values.clear()

        def selection_set(self, _index):
            pass

        def activate(self, _index):
            pass

        def focus_set(self):
            self.focused = True

        def destroy(self):
            self.destroyed = True

        def invoke(self):
            self.options["command"]()

    for name in ("Toplevel", "Listbox"):
        monkeypatch.setattr(layout.tk, name, Widget)
    for name in ("Frame", "LabelFrame", "Label", "Entry", "Checkbutton", "Button"):
        monkeypatch.setattr(layout.ttk, name, Widget)
    monkeypatch.setattr(layout.tk, "StringVar", Variable)
    monkeypatch.setattr(layout.tk, "BooleanVar", Variable)
    for name in ("set_acc_name", "set_acc_description", "set_automation_id"):
        monkeypatch.setattr(layout.tk_uia, name, lambda *_args: None)
    monkeypatch.setattr(layout.messagebox, "askokcancel", lambda *_args, **_kwargs: 1 / 0)
    monkeypatch.setattr(layout.messagebox, "showerror", lambda _title, message, **_kwargs: errors.append(message))
    workspace = [tmp_path]
    monkeypatch.setattr(layout, "_owner_economic_workspace", lambda _app: (workspace[0], None))
    monkeypatch.setattr(layout, "_owner_economic_write_blocker", lambda *_args: None)
    monkeypatch.setattr(layout, "refresh_owner_economic_authority_surface", lambda _app: None)
    layout._show_owner_economic_dialog(object())
    button = next(widget for widget in widgets if widget.options.get("text") == text("ui.windows.owner_authority.button.review"))
    readback = next(widget for widget in widgets if widget.options.get("height") == 18)
    return widgets, variables, workspace, errors, button, readback


def test_keyboard_review_is_readable_before_distinct_confirm_and_edit_requires_rereview(
    tmp_path, monkeypatch
):
    widgets, variables, _workspace, errors, button, readback = _fake_dialog(monkeypatch, tmp_path)
    path = tmp_path / EconomicGoalStore.FILE_NAME

    button.invoke()
    assert not path.exists()
    assert readback.focused
    assert readback.values[0] == text("ui.windows.owner_authority.review.prompt")
    assert any("Максимальна частка однієї ставки: 0.02" in line for line in readback.values)
    assert button.options["text"] == text("ui.windows.owner_authority.button.confirm")

    variables[3].set("0.01")
    assert button.options["text"] == text("ui.windows.owner_authority.button.review")
    assert not path.exists()
    button.invoke()
    assert any("Максимальна частка однієї ставки: 0.01" in line for line in readback.values)
    assert not path.exists()

    button.invoke()
    assert EconomicGoalStore(tmp_path).load().max_stake_fraction == Decimal("0.01")
    assert button.options["state"] == "disabled"
    assert not errors
    assert widgets[0].destroyed is False


def test_cancellation_and_workspace_switch_after_review_never_write(tmp_path, monkeypatch):
    widgets, _variables, workspace, errors, button, _readback = _fake_dialog(monkeypatch, tmp_path)
    button.invoke()
    workspace[0] = tmp_path / "other"
    button.invoke()
    assert errors
    assert not (tmp_path / EconomicGoalStore.FILE_NAME).exists()
    assert not (workspace[0] / EconomicGoalStore.FILE_NAME).exists()
    close = next(widget for widget in widgets if widget.options.get("text") == text("ui.windows.owner_authority.button.close"))
    close.invoke()
    assert widgets[0].destroyed
    assert not (tmp_path / EconomicGoalStore.FILE_NAME).exists()

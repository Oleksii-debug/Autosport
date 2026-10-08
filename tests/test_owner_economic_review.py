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
            self.bindings = {}
            self.acc_name = None
            self.acc_description = None
            self.automation_id = None
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

        def bind(self, sequence, callback, *_args):
            self.bindings[sequence] = callback

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
    monkeypatch.setattr(
        layout.tk_uia,
        "set_acc_name",
        lambda widget, value: setattr(widget, "acc_name", value),
    )
    monkeypatch.setattr(
        layout.tk_uia,
        "set_acc_description",
        lambda widget, value: setattr(widget, "acc_description", value),
    )
    monkeypatch.setattr(
        layout.tk_uia,
        "set_automation_id",
        lambda widget, value: setattr(widget, "automation_id", value),
    )
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


def test_conditional_owner_form_has_static_keyboard_and_accessibility_contract(
    tmp_path, monkeypatch
):
    """Machine-check static annotations/focus only; this is not NVDA or external UIA proof."""

    widgets, _variables, _workspace, errors, button, readback = _fake_dialog(
        monkeypatch, tmp_path
    )
    path = tmp_path / EconomicGoalStore.FILE_NAME

    expected_ids = {
        key: automation_id
        for key, automation_id in layout.OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS.items()
        if 309 <= automation_id <= 328
    }
    assert sorted(expected_ids.values()) == list(range(309, 329))

    conditional_widgets = [
        widget for widget in widgets if widget.automation_id in expected_ids.values()
    ]
    observed_ids = [widget.automation_id for widget in conditional_widgets]
    assert len(observed_ids) == len(expected_ids)
    assert len(set(observed_ids)) == len(expected_ids)

    by_id = {widget.automation_id: widget for widget in conditional_widgets}
    for field in layout.OWNER_ECONOMIC_FORM_FIELDS:
        widget = by_id[layout.OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS[field]]
        assert widget.options.get("takefocus") is True
        assert widget.acc_name == text(f"ui.windows.owner_authority.field.{field}")
        assert widget.acc_name.strip()

    emergency_stop = by_id[
        layout.OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS["emergency_stop"]
    ]
    assert emergency_stop.options.get("takefocus") is True
    assert emergency_stop.acc_name == text(
        "ui.windows.owner_authority.field.emergency_stop"
    )

    create = by_id[layout.OWNER_ECONOMIC_DIALOG_AUTOMATION_IDS["create"]]
    assert create is button
    assert create.options.get("takefocus") is True
    assert create.acc_name == text("ui.windows.owner_authority.button.review")
    assert create.acc_description == text(
        "ui.windows.owner_authority.accessibility.create.description"
    )
    assert not path.exists()

    dialog = widgets[0]
    assert "<Control-Return>" in dialog.bindings

    button.invoke()
    assert not path.exists()
    assert readback.focused
    assert create.acc_name == text("ui.windows.owner_authority.button.confirm")
    assert create.acc_description == text(
        "ui.windows.owner_authority.accessibility.confirm.description"
    )

    create.focused = False
    dialog.bindings["<Control-Return>"](None)
    assert create.focused
    assert not errors
    assert not path.exists()


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


def test_switch_away_and_back_requires_fresh_review_before_creation(tmp_path, monkeypatch):
    _widgets, _variables, workspace, errors, button, readback = _fake_dialog(monkeypatch, tmp_path)
    path = tmp_path / EconomicGoalStore.FILE_NAME

    button.invoke()  # Review workspace A, without a write.
    workspace[0] = tmp_path / "other"
    button.invoke()  # Rejected confirm in B consumes the A review.
    assert errors
    assert button.options["text"] == text("ui.windows.owner_authority.button.review")
    workspace[0] = tmp_path
    button.invoke()  # This must review A again, not persist the stale approval.
    assert not path.exists()
    assert button.options["text"] == text("ui.windows.owner_authority.button.confirm")
    assert readback.focused
    button.invoke()
    assert path.exists()


def test_temporary_recovery_blocker_requires_fresh_review_after_clear(tmp_path, monkeypatch):
    _widgets, _variables, _workspace, errors, button, _readback = _fake_dialog(monkeypatch, tmp_path)
    path = tmp_path / EconomicGoalStore.FILE_NAME
    blocker = [None]
    monkeypatch.setattr(layout, "_owner_economic_write_blocker", lambda *_args: blocker[0])

    button.invoke()
    blocker[0] = "Recovery required"
    button.invoke()
    assert errors == ["Recovery required"]
    assert button.options["text"] == text("ui.windows.owner_authority.button.review")
    blocker[0] = None
    button.invoke()
    assert not path.exists()
    button.invoke()
    assert path.exists()


def test_persistence_rejection_consumes_review_before_retry(tmp_path, monkeypatch):
    _widgets, _variables, _workspace, errors, button, _readback = _fake_dialog(monkeypatch, tmp_path)
    path = tmp_path / EconomicGoalStore.FILE_NAME
    original = layout._persist_initial_owner_economic_contract
    rejected = [False]

    def reject_once(*args, **kwargs):
        if not rejected[0]:
            rejected[0] = True
            raise layout.OwnerEconomicAuthorityError("Concurrent recovery")
        return original(*args, **kwargs)

    monkeypatch.setattr(layout, "_persist_initial_owner_economic_contract", reject_once)
    button.invoke()
    button.invoke()
    assert errors == ["Concurrent recovery"]
    assert button.options["text"] == text("ui.windows.owner_authority.button.review")
    button.invoke()
    assert not path.exists()
    button.invoke()
    assert path.exists()

"""Keyboard-first Windows manual calculation workbench.

The workbench owns presentation and input collection only. Formula, arithmetic,
exact/approximate classification, assumptions, warnings and evidence hashing stay
in ManualCalculationService/CalculationEngine. No result is persisted here.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any

import tk_uia

from .localization import text
from .calculation_manual import ManualCalculationService


WORKBENCH_AUTOMATION_IDS = {
    "open": 330,
    "operation": 331,
    "input": 332,
    "calculate": 333,
    "result": 334,
    "clear": 335,
    "close": 336,
}

WORKBENCH_OPERATIONS = (
    ("odds_conversion", "Перетворення коефіцієнта"),
    ("implied_probability", "Ймовірність із коефіцієнта"),
    ("multiplicative_devig", "Мультиплікативне зняття маржі"),
    ("expected_return", "Очікуваний результат"),
    ("paper_payout", "Паперова виплата"),
    ("fractional_kelly", "Обмежений дробовий Kelly"),
    ("maximum_drawdown", "Максимальна просадка"),
)

_OPERATION_LABEL_KEYS = {
    "odds_conversion": "ui.windows.manual_calculation.operation.odds_conversion",
    "implied_probability": "ui.windows.manual_calculation.operation.implied_probability",
    "multiplicative_devig": "ui.windows.manual_calculation.operation.multiplicative_devig",
    "expected_return": "ui.windows.manual_calculation.operation.expected_return",
    "paper_payout": "ui.windows.manual_calculation.operation.paper_payout",
    "fractional_kelly": "ui.windows.manual_calculation.operation.fractional_kelly",
    "maximum_drawdown": "ui.windows.manual_calculation.operation.maximum_drawdown",
}
_OPERATION_HINT_KEYS = {
    "odds_conversion": "ui.windows.manual_calculation.hint.odds_conversion",
    "implied_probability": "ui.windows.manual_calculation.hint.implied_probability",
    "multiplicative_devig": "ui.windows.manual_calculation.hint.multiplicative_devig",
    "expected_return": "ui.windows.manual_calculation.hint.expected_return",
    "paper_payout": "ui.windows.manual_calculation.hint.paper_payout",
    "fractional_kelly": "ui.windows.manual_calculation.hint.fractional_kelly",
    "maximum_drawdown": "ui.windows.manual_calculation.hint.maximum_drawdown",
}
WORKBENCH_OPERATIONS = tuple(
    (key, text(label_key)) for key, label_key in _OPERATION_LABEL_KEYS.items()
)
_OPERATION_HINTS = {key: text(hint_key) for key, hint_key in _OPERATION_HINT_KEYS.items()}


def _read_lines(widget: tk.Text, *, minimum: int = 1) -> list[str]:
    raw = widget.get("1.0", "end-1c")
    lines = [line.strip() for line in raw.replace(",", "\n").splitlines() if line.strip()]
    if len(lines) < minimum:
        raise ValueError(text("ui.windows.manual_calculation.error.nonempty"))
    return lines


def _selection_odds(widget: tk.Text) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in _read_lines(widget):
        if "=" not in line:
            raise ValueError(text("ui.windows.manual_calculation.error.selection_format"))
        selection, odds = (part.strip() for part in line.split("=", 1))
        if not selection or not odds:
            raise ValueError(text("ui.windows.manual_calculation.error.selection_empty"))
        if selection in values:
            raise ValueError(text("ui.windows.manual_calculation.error.duplicate", selection=selection))
        values[selection] = odds
    return values


def _calculation_call(
    service: ManualCalculationService,
    operation: str,
    widget: tk.Text,
):
    if operation in {"odds_conversion", "implied_probability"}:
        value = _read_lines(widget)
        if len(value) != 1:
            raise ValueError(text("ui.windows.manual_calculation.error.single"))
        return getattr(service, operation)(value[0])

    if operation == "multiplicative_devig":
        return service.multiplicative_devig(_selection_odds(widget))

    values = _read_lines(widget)
    if operation == "expected_return":
        if len(values) != 3:
            raise ValueError(text("ui.windows.manual_calculation.error.expected_return"))
        return service.expected_return(values[0], values[1], values[2])
    if operation == "paper_payout":
        if len(values) != 2:
            raise ValueError(text("ui.windows.manual_calculation.error.paper_payout"))
        return service.paper_payout(values[0], values[1])
    if operation == "fractional_kelly":
        if len(values) != 4:
            raise ValueError(text("ui.windows.manual_calculation.error.kelly"))
        return service.fractional_kelly(values[0], values[1], fraction=values[2], cap=values[3])
    if operation == "maximum_drawdown":
        return service.maximum_drawdown(values)
    raise ValueError(text("ui.windows.manual_calculation.error.unknown"))


def _set_uia(widget: Any, *, name: str, description: str, automation_id: int) -> None:
    tk_uia.set_acc_name(widget, name)
    tk_uia.set_acc_description(widget, description)
    tk_uia.set_automation_id(widget, automation_id)


def show_manual_calculation_workbench(app: Any) -> tk.Toplevel:
    """Open a non-persistent, keyboard-first manual calculation dialog."""

    dialog = tk.Toplevel(app)
    dialog.title(text("ui.windows.manual_calculation.dialog.title"))
    dialog.transient(app)
    dialog.geometry("1080x780")
    dialog.minsize(820, 640)

    body = ttk.Frame(dialog, padding=12)
    body.pack(fill="both", expand=True)

    ttk.Label(
        body,
        text=text("ui.windows.manual_calculation.dialog.title"),
    ).pack(anchor="w")
    ttk.Label(
        body,
        text=text("ui.windows.manual_calculation.dialog.description"),
        wraplength=1000,
    ).pack(anchor="w", pady=(2, 8))

    row = ttk.Frame(body)
    row.pack(fill="x")
    ttk.Label(row, text=text("ui.windows.manual_calculation.operation.label")).pack(side="left", padx=(0, 6))
    operation_var = tk.StringVar(value=WORKBENCH_OPERATIONS[0][1])
    operation_values = {label: key for key, label in WORKBENCH_OPERATIONS}
    operation = ttk.Combobox(
        row,
        textvariable=operation_var,
        values=[label for _, label in WORKBENCH_OPERATIONS],
        state="readonly",
        width=52,
        takefocus=True,
    )
    operation.pack(side="left", fill="x", expand=True)

    hint_var = tk.StringVar(value=_OPERATION_HINTS[WORKBENCH_OPERATIONS[0][0]])
    ttk.Label(body, textvariable=hint_var, wraplength=1000).pack(anchor="w", pady=(4, 4))

    ttk.Label(body, text=text("ui.windows.manual_calculation.input.label")).pack(anchor="w")
    input_box = tk.Text(body, height=10, wrap="word", takefocus=True)
    input_box.pack(fill="x", pady=(2, 8))

    result_box = tk.Text(body, height=16, wrap="word", takefocus=True)
    result_box.configure(state="disabled")
    result_box.pack(fill="both", expand=True)

    status_var = tk.StringVar(value=text("ui.windows.manual_calculation.status.ready"))
    ttk.Label(body, textvariable=status_var, wraplength=1000).pack(anchor="w", pady=(4, 4))

    buttons = ttk.Frame(body)
    buttons.pack(fill="x", pady=(4, 0))

    def selected_operation() -> str:
        try:
            return operation_values[operation_var.get()]
        except KeyError as exc:
            raise ValueError(text("ui.windows.manual_calculation.error.operation_empty")) from exc

    def set_result(value: str | None) -> None:
        result_box.configure(state="normal")
        result_box.delete("1.0", "end")
        if value is not None:
            result_box.insert("1.0", value)
        result_box.configure(state="disabled")

    def on_operation_changed(_event: object | None = None) -> None:
        set_result(None)
        key = selected_operation()
        hint_var.set(_OPERATION_HINTS[key])
        status_var.set(text("ui.windows.manual_calculation.status.ready"))

    operation.bind("<<ComboboxSelected>>", on_operation_changed)

    service = ManualCalculationService()

    def calculate() -> None:
        set_result(None)
        try:
            evidence = _calculation_call(service, selected_operation(), input_box)
            rendered = evidence.to_text()
        except (TypeError, ValueError, ArithmeticError) as exc:
            status_var.set(text("ui.windows.manual_calculation.status.error"))
            messagebox.showerror(
                text("ui.windows.manual_calculation.dialog.title"),
                f"Розрахунок не виконано: {exc}",
                parent=dialog,
            )
            input_box.focus_set()
            return
        set_result(rendered)
        status_var.set(text("ui.windows.manual_calculation.status.success"))
        result_box.focus_set()

    def clear() -> None:
        input_box.delete("1.0", "end")
        set_result(None)
        status_var.set(text("ui.windows.manual_calculation.status.cleared"))
        input_box.focus_set()

    calculate_button = ttk.Button(
        buttons,
        text=text("ui.windows.manual_calculation.calculate"),
        command=calculate,
        takefocus=True,
    )
    calculate_button.pack(side="left", padx=(0, 6))
    clear_button = ttk.Button(
        buttons,
        text=text("ui.windows.manual_calculation.clear"),
        command=clear,
        takefocus=True,
    )
    clear_button.pack(side="left", padx=(0, 6))
    close_button = ttk.Button(
        buttons,
        text=text("ui.windows.manual_calculation.close"),
        command=dialog.destroy,
        takefocus=True,
    )
    close_button.pack(side="right")

    _set_uia(
        operation,
        name=text("ui.windows.manual_calculation.uia.operation.name"),
        description=text("ui.windows.manual_calculation.uia.operation.description"),
        automation_id=WORKBENCH_AUTOMATION_IDS["operation"],
    )
    _set_uia(
        input_box,
        name=text("ui.windows.manual_calculation.uia.input.name"),
        description=text("ui.windows.manual_calculation.uia.input.description"),
        automation_id=WORKBENCH_AUTOMATION_IDS["input"],
    )
    _set_uia(
        calculate_button,
        name=text("ui.windows.manual_calculation.uia.calculate.name"),
        description=text("ui.windows.manual_calculation.uia.calculate.description"),
        automation_id=WORKBENCH_AUTOMATION_IDS["calculate"],
    )
    _set_uia(
        clear_button,
        name=text("ui.windows.manual_calculation.uia.clear.name"),
        description=text("ui.windows.manual_calculation.uia.clear.description"),
        automation_id=WORKBENCH_AUTOMATION_IDS["clear"],
    )
    _set_uia(
        close_button,
        name=text("ui.windows.manual_calculation.uia.close.name"),
        description=text("ui.windows.manual_calculation.uia.close.description"),
        automation_id=WORKBENCH_AUTOMATION_IDS["close"],
    )
    _set_uia(
        result_box,
        name=text("ui.windows.manual_calculation.uia.result.name"),
        description=text("ui.windows.manual_calculation.uia.result.description"),
        automation_id=WORKBENCH_AUTOMATION_IDS["result"],
    )

    dialog._autosport_workbench_controls = {
        "operation": operation,
        "input": input_box,
        "calculate": calculate_button,
        "result": result_box,
        "clear": clear_button,
        "close": close_button,
    }

    # The dialog is deliberately keyboard-first: operation -> input -> Calculate ->
    # Clear -> Close, with the result as a read-only focus target after success.
    operation.focus_set()
    return dialog

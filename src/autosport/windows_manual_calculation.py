"""Keyboard-first Windows manual calculation workbench.

The workbench owns presentation and input collection only. Formula, arithmetic,
exact/approximate classification, assumptions, warnings and evidence hashing stay
in ManualCalculationService/CalculationEngine. No result is persisted here.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Any, Callable

import tk_uia

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
    ("odds_conversion", "Перетворення десяткового коефіцієнта"),
    ("implied_probability", "Ймовірність із десяткового коефіцієнта"),
    ("multiplicative_devig", "Мультиплікативне зняття маржі"),
    ("expected_return", "Очікуваний результат"),
    ("paper_payout", "Паперова виплата"),
    ("fractional_kelly", "Обмежений дробовий Kelly"),
    ("maximum_drawdown", "Максимальна просадка"),
)

_OPERATION_HINTS = {
    "odds_conversion": "Введіть один скінченний десятковий коефіцієнт.",
    "implied_probability": "Введіть один скінченний десятковий коефіцієнт.",
    "multiplicative_devig": "Кожен рядок: selection_id=decimal_odds; ідентифікатори не повторюються.",
    "expected_return": "Введіть: ймовірність, десятковий коефіцієнт, ставка — по одному значенню в рядку.",
    "paper_payout": "Введіть: ставка, десятковий коефіцієнт — по одному значенню в рядку.",
    "fractional_kelly": "Введіть: ймовірність, коефіцієнт, fraction, cap — по одному значенню в рядку.",
    "maximum_drawdown": "Введіть послідовність балансів, по одному значенню в рядку.",
}


def _read_lines(widget: tk.Text, *, minimum: int = 1) -> list[str]:
    raw = widget.get("1.0", "end-1c")
    lines = [line.strip() for line in raw.replace(",", "\n").splitlines() if line.strip()]
    if len(lines) < minimum:
        raise ValueError("Потрібно ввести всі обов'язкові значення.")
    return lines


def _selection_odds(widget: tk.Text) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in _read_lines(widget):
        if "=" not in line:
            raise ValueError("Для зняття маржі кожен рядок має бути у форматі selection_id=коефіцієнт.")
        selection, odds = (part.strip() for part in line.split("=", 1))
        if not selection or not odds:
            raise ValueError("Ідентифікатор вибору та коефіцієнт не можуть бути порожніми.")
        if selection in values:
            raise ValueError(f"Повторний ідентифікатор вибору: {selection!r}.")
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
            raise ValueError("Ця операція приймає рівно одне числове значення.")
        return getattr(service, operation)(value[0])

    if operation == "multiplicative_devig":
        return service.multiplicative_devig(_selection_odds(widget))

    values = _read_lines(widget)
    if operation == "expected_return":
        if len(values) != 3:
            raise ValueError("Очікуваний результат потребує 3 значення: ймовірність, коефіцієнт, ставка.")
        return service.expected_return(values[0], values[1], values[2])
    if operation == "paper_payout":
        if len(values) != 2:
            raise ValueError("Паперова виплата потребує 2 значення: ставка, коефіцієнт.")
        return service.paper_payout(values[0], values[1])
    if operation == "fractional_kelly":
        if len(values) != 4:
            raise ValueError("Kelly потребує 4 значення: ймовірність, коефіцієнт, fraction, cap.")
        return service.fractional_kelly(values[0], values[1], fraction=values[2], cap=values[3])
    if operation == "maximum_drawdown":
        return service.maximum_drawdown(values)
    raise ValueError("Невідома ручна операція.")


def _set_uia(widget: Any, *, name: str, description: str, automation_id: int) -> None:
    tk_uia.set_acc_name(widget, name)
    tk_uia.set_acc_description(widget, description)
    tk_uia.set_automation_id(widget, automation_id)


def show_manual_calculation_workbench(app: Any) -> None:
    """Open a non-persistent, keyboard-first manual calculation dialog."""

    dialog = tk.Toplevel(app)
    dialog.title("Автоспорт — ручні розрахунки")
    dialog.transient(app)
    dialog.geometry("1080x780")
    dialog.minsize(820, 640)

    body = ttk.Frame(dialog, padding=12)
    body.pack(fill="both", expand=True)

    ttk.Label(
        body,
        text="Ручні розрахунки — лише дослідження / паперовий режим",
    ).pack(anchor="w")
    ttk.Label(
        body,
        text=(
            "Введіть значення вручну. Формули та хеші належать канонічному сервісу; "
            "результат не записується на диск і не створює реальну ставку."
        ),
        wraplength=1000,
    ).pack(anchor="w", pady=(2, 8))

    row = ttk.Frame(body)
    row.pack(fill="x")
    ttk.Label(row, text="Операція:").pack(side="left", padx=(0, 6))
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

    ttk.Label(body, text="Вхідні значення:").pack(anchor="w")
    input_box = tk.Text(body, height=10, wrap="word", takefocus=True)
    input_box.pack(fill="x", pady=(2, 8))

    result_box = tk.Text(body, height=16, wrap="word", takefocus=True)
    result_box.configure(state="disabled")
    result_box.pack(fill="both", expand=True)

    status_var = tk.StringVar(value="Готово. Жодні дані ще не обчислено.")
    ttk.Label(body, textvariable=status_var, wraplength=1000).pack(anchor="w", pady=(4, 4))

    buttons = ttk.Frame(body)
    buttons.pack(fill="x", pady=(4, 0))

    def selected_operation() -> str:
        try:
            return operation_values[operation_var.get()]
        except KeyError as exc:
            raise ValueError("Операція не вибрана.") from exc

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
        status_var.set("Готово. Введіть нові значення для обраної операції.")

    operation.bind("<<ComboboxSelected>>", on_operation_changed)

    service = ManualCalculationService()

    def calculate() -> None:
        set_result(None)
        try:
            evidence = _calculation_call(service, selected_operation(), input_box)
            rendered = evidence.to_text()
        except (TypeError, ValueError, ArithmeticError) as exc:
            status_var.set("Розрахунок відхилено; частковий результат не показується.")
            messagebox.showerror(
                "Автоспорт — ручні розрахунки",
                f"Розрахунок не виконано: {exc}",
                parent=dialog,
            )
            input_box.focus_set()
            return
        set_result(rendered)
        status_var.set(
            "Готово: канонічний результат і evidence доступні лише для читання; "
            "real_money_execution=false."
        )
        result_box.focus_set()

    def clear() -> None:
        input_box.delete("1.0", "end")
        set_result(None)
        status_var.set("Очищено. Скасування/очищення нічого не записує.")
        input_box.focus_set()

    calculate_button = ttk.Button(
        buttons,
        text="Обчислити",
        command=calculate,
        takefocus=True,
    )
    calculate_button.pack(side="left", padx=(0, 6))
    clear_button = ttk.Button(
        buttons,
        text="Очистити",
        command=clear,
        takefocus=True,
    )
    clear_button.pack(side="left", padx=(0, 6))
    close_button = ttk.Button(
        buttons,
        text="Закрити",
        command=dialog.destroy,
        takefocus=True,
    )
    close_button.pack(side="right")

    _set_uia(
        operation,
        name="Операція ручного розрахунку",
        description="Вибір канонічної ручної операції; формула залишається в сервісі.",
        automation_id=WORKBENCH_AUTOMATION_IDS["operation"],
    )
    _set_uia(
        input_box,
        name="Вхідні значення ручного розрахунку",
        description="Лише ручний текстовий ввід. Некоректні, нечислові та дубльовані значення відхиляються до обчислення.",
        automation_id=WORKBENCH_AUTOMATION_IDS["input"],
    )
    _set_uia(
        calculate_button,
        name="Обчислити ручний результат",
        description="Запускає лише канонічний ManualCalculationService; реальні ставки не створюються.",
        automation_id=WORKBENCH_AUTOMATION_IDS["calculate"],
    )
    _set_uia(
        clear_button,
        name="Очистити ручні значення",
        description="Очищає локальні поля без запису на диск.",
        automation_id=WORKBENCH_AUTOMATION_IDS["clear"],
    )
    _set_uia(
        close_button,
        name="Закрити ручні розрахунки",
        description="Закриває робочу поверхню без запису результату.",
        automation_id=WORKBENCH_AUTOMATION_IDS["close"],
    )
    _set_uia(
        result_box,
        name="Результат і evidence ручного розрахунку",
        description="Лише для читання: канонічний JSON результату, метод, одиниці, припущення, попередження та evidence hash.",
        automation_id=WORKBENCH_AUTOMATION_IDS["result"],
    )

    # The dialog is deliberately keyboard-first: operation -> input -> Calculate ->
    # Clear -> Close, with the result as a read-only focus target after success.
    operation.focus_set()

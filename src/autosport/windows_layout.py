from __future__ import annotations

import tkinter as tk
from typing import Any

from tkinter import ttk

import tk_uia

from .windows_surface_contract import (
    SURFACE_BY_KEY,
    SURFACES,
    load_surface_selection,
    save_surface_selection,
    surface_detail_lines,
)

# Keep every critical surface mapped inside the canonical 1080x860 Windows
# window. Listboxes remain scrollable, so reducing visible rows does not remove
# content or keyboard access.
_SURFACE_HEIGHTS = {
    "live_quotes": 4,
    "tickets": 5,
    "evaluation": 4,
    "log": 5,
}

WINDOWS_SHELL_AUTOMATION_IDS = {
    "navigation": 301,
    "state": 302,
    "open": 303,
    "details": 304,
}


def compact_surface_heights(app: Any) -> None:
    """Apply the Windows V1 vertical budget without weakening UIA gates."""
    for name, height in _SURFACE_HEIGHTS.items():
        getattr(app, name).configure(height=height)


def _focus_surface_target(app: Any, surface_key: str) -> None:
    surface = SURFACE_BY_KEY[surface_key]
    if surface.target_widget is None:
        app.shell_details.focus_set()
        return
    target = getattr(app, surface.target_widget, None)
    if target is None and surface.target_widget == "bank_summary":
        target = getattr(app, "tickets", None)
    if target is None:
        app.shell_details.focus_set()
        return
    target.focus_set()


def _render_shell_surface(app: Any, surface_key: str, *, persist: bool) -> None:
    surface = SURFACE_BY_KEY[surface_key]
    app.shell_surface_key.set(surface.key)
    app.shell_surface_display.set(surface.title_uk)
    app.shell_surface_state.set(
        {
            "v1-active": "Активна V1-поверхня",
            "visible-disabled": f"Вимкнено: {surface.blocked_reason_uk}",
            "presentation-only": "Лише інформація — без доменної дії",
        }[surface.phase]
    )
    app.shell_details.delete(0, "end")
    for line in surface_detail_lines(surface):
        app.shell_details.insert("end", line)
    app.shell_open_button.configure(
        state=("normal" if surface.target_widget is not None else "disabled")
    )
    if persist:
        save_surface_selection(app.workspace, surface.key)


def _on_shell_selected(app: Any, _event: object | None = None) -> None:
    title = app.shell_surface_display.get()
    surface = next((item for item in SURFACES if item.title_uk == title), SURFACES[0])
    _render_shell_surface(app, surface.key, persist=True)


def _cycle_shell_surface(app: Any, delta: int) -> None:
    current = app.shell_surface_key.get()
    keys = [surface.key for surface in SURFACES]
    try:
        index = keys.index(current)
    except ValueError:
        index = 0
    surface = SURFACE_BY_KEY[keys[(index + delta) % len(keys)]]
    _render_shell_surface(app, surface.key, persist=True)
    app.shell_navigation.focus_set()


def install_windows_product_shell(app: Any) -> None:
    """Install the truthful keyboard-first product navigator in the existing Tk shell."""
    children = app.winfo_children()
    frame = children[0] if children else None
    if frame is None:
        raise RuntimeError("Autosport root frame is missing")

    shell = ttk.LabelFrame(frame, text="Навігація продукту", padding=(8, 6))
    first = frame.winfo_children()[0] if frame.winfo_children() else None
    if first is None:
        shell.pack(fill="x", pady=(0, 8))
    else:
        shell.pack(fill="x", pady=(0, 8), before=first)

    app.shell_surface_key = tk.StringVar()
    app.shell_surface_display = tk.StringVar()
    app.shell_surface_state = tk.StringVar()

    nav_row = ttk.Frame(shell)
    nav_row.pack(fill="x")
    ttk.Label(nav_row, text="Екран:").pack(side="left", padx=(0, 4))
    app.shell_navigation = ttk.Combobox(
        nav_row,
        textvariable=app.shell_surface_display,
        values=[surface.title_uk for surface in SURFACES],
        state="readonly",
        width=38,
        takefocus=True,
    )
    app.shell_navigation.pack(side="left", fill="x", expand=True, padx=(0, 8))
    app.shell_navigation.bind("<<ComboboxSelected>>", lambda event: _on_shell_selected(app, event))

    app.shell_open_button = ttk.Button(
        nav_row,
        text="Перейти до робочої поверхні",
        command=lambda: _focus_surface_target(app, app.shell_surface_key.get()),
        takefocus=True,
    )
    app.shell_open_button.pack(side="left")

    app.shell_state = ttk.Entry(
        shell,
        textvariable=app.shell_surface_state,
        state="readonly",
        takefocus=True,
    )
    app.shell_state.pack(fill="x", pady=(6, 4))

    app.shell_details = tk.Listbox(shell, height=4, takefocus=True)
    app.shell_details.pack(fill="x")

    app.bind("<F2>", lambda _event: app.shell_navigation.focus_set())
    app.bind("<Control-Alt-Left>", lambda _event: _cycle_shell_surface(app, -1))
    app.bind("<Control-Alt-Right>", lambda _event: _cycle_shell_surface(app, 1))

    _render_shell_surface(app, load_surface_selection(app.workspace), persist=False)


def configure_windows_product_shell_accessibility(app: Any) -> None:
    """Attach stable Windows UIA metadata after the canonical Tk/UIA bridge is enabled."""
    tk_uia.set_acc_name(app.shell_navigation, "Навігація екранами Автоспорт")
    tk_uia.set_acc_description(
        app.shell_navigation,
        "Виберіть один із канонічних екранів. F2 переводить фокус сюди; Control+Alt+Left/Right рухає між екранами.",
    )
    tk_uia.set_automation_id(app.shell_navigation, WINDOWS_SHELL_AUTOMATION_IDS["navigation"])
    tk_uia.set_acc_name(app.shell_state, "Стан вибраної поверхні")
    tk_uia.set_acc_description(
        app.shell_state,
        "Тільки для читання: активна, інформаційна або видима, але вимкнена capability.",
    )
    tk_uia.set_automation_id(app.shell_state, WINDOWS_SHELL_AUTOMATION_IDS["state"])
    tk_uia.set_acc_name(app.shell_open_button, "Перейти до робочої поверхні")
    tk_uia.set_acc_description(
        app.shell_open_button,
        "Переводить фокус до вже реалізованого робочого контролу. Для неактивних capability кнопка вимкнена.",
    )
    tk_uia.set_automation_id(app.shell_open_button, WINDOWS_SHELL_AUTOMATION_IDS["open"])
    tk_uia.set_acc_name(app.shell_details, "Контракт вибраного екрана")
    tk_uia.set_acc_description(
        app.shell_details,
        "Read-only опис задачі, клавіатури, станів, persistence та меж доменної істини вибраного екрана.",
    )
    tk_uia.set_automation_id(app.shell_details, WINDOWS_SHELL_AUTOMATION_IDS["details"])


def install_compact_windows_layout() -> None:
    """Install the Windows build wrapper before any packaged AutosportApp exists.

    The packaged entrypoint calls this before normal GUI startup and before the
    accessibility/keyboard audit entrypoints. The same layout is therefore
    audited that Windows users actually receive; this is not an audit-only
    resize.
    """
    from .gui import AutosportApp

    if getattr(AutosportApp, "_compact_windows_layout_installed", False):
        return

    original_build = AutosportApp._build
    original_configure_accessibility = AutosportApp._configure_accessibility

    def build_with_windows_budget(self) -> None:
        original_build(self)
        compact_surface_heights(self)
        install_windows_product_shell(self)

    def configure_with_windows_shell(self) -> None:
        original_configure_accessibility(self)
        configure_windows_product_shell_accessibility(self)

    AutosportApp._build = build_with_windows_budget
    AutosportApp._configure_accessibility = configure_with_windows_shell
    AutosportApp._compact_windows_layout_installed = True

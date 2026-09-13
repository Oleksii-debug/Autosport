from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import tk_uia

from .dataset import load_dataset
from .live_observation import OneShotObservationWorker, observe_workspace_once
from .parlayapi_provider import ParlayApiTableTennisProvider
from .paths import default_workspace
from .session import AutosportSession
from .ui_model import (
    observation_quote_lines,
    observation_summary,
    result_summary,
    ticket_lines,
)


_SPEEDS = {
    "Подієвий — максимально швидко": 0.0,
    "1× реальний час": 1.0,
    "10×": 10.0,
    "100×": 100.0,
    "1000×": 1000.0,
}
_LIVE_MODES = {
    "Public preview — без ключа": True,
    "API key з environment": False,
}

AUTOMATION_IDS = {
    "choose_dataset": 101,
    "run_replay": 102,
    "replay_speed": 103,
    "live_mode": 104,
    "live_refresh": 105,
    "tickets": 201,
    "log": 202,
    "live_quotes": 203,
}


class AutosportApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Автоспорт — V1 Windows Paper Lab")
        self.geometry("960x800")
        self.minsize(760, 620)
        self.dataset_path: Path | None = None
        self.session = AutosportSession(default_workspace(), "10000")
        self.live_worker = OneShotObservationWorker()
        self._closing = False
        self.status = tk.StringVar(value="Готово. Виберіть папку replay dataset або оновіть live snapshot.")
        self.bank = tk.StringVar(value=self._bank_text())
        self.dataset_text = tk.StringVar(value="Dataset не вибраний.")
        self.speed_text = tk.StringVar(value="Подієвий — максимально швидко")
        self.live_mode_text = tk.StringVar(value="Public preview — без ключа")
        self.live_status = tk.StringVar(value="Live snapshot ще не завантажувався.")
        self._build()
        self.update_idletasks()
        self._configure_accessibility()
        self.protocol("WM_DELETE_WINDOW", self.close_app)

    def _build(self) -> None:
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Автоспорт — V1 Windows Paper Lab", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(frame, textvariable=self.bank, wraplength=900).pack(anchor="w", pady=(12, 4))
        ttk.Label(frame, textvariable=self.dataset_text, wraplength=900).pack(anchor="w", pady=(0, 4))
        ttk.Label(frame, textvariable=self.status, wraplength=900).pack(anchor="w", pady=(0, 12))

        controls = ttk.Frame(frame)
        controls.pack(fill="x")
        self.choose_button = ttk.Button(controls, text="Вибрати dataset", command=self.choose_dataset)
        self.choose_button.pack(side="left", padx=(0, 8))
        self.run_button = ttk.Button(controls, text="Запустити paper replay", command=self.run_dataset)
        self.run_button.pack(side="left", padx=(0, 8))
        self.speed_label = ttk.Label(controls, text="Швидкість:")
        self.speed_label.pack(side="left", padx=(8, 4))
        self.speed = ttk.Combobox(
            controls,
            textvariable=self.speed_text,
            values=list(_SPEEDS),
            state="readonly",
            width=28,
            takefocus=True,
        )
        self.speed.pack(side="left")

        live_controls = ttk.Frame(frame)
        live_controls.pack(fill="x", pady=(14, 0))
        self.live_mode_label = ttk.Label(live_controls, text="Live режим:")
        self.live_mode_label.pack(side="left", padx=(0, 4))
        self.live_mode = ttk.Combobox(
            live_controls,
            textvariable=self.live_mode_text,
            values=list(_LIVE_MODES),
            state="readonly",
            width=29,
            takefocus=True,
        )
        self.live_mode.pack(side="left", padx=(0, 8))
        self.live_refresh_button = ttk.Button(
            live_controls,
            text="Оновити live snapshot",
            command=self.refresh_live_snapshot,
        )
        self.live_refresh_button.pack(side="left")
        ttk.Label(frame, textvariable=self.live_status, wraplength=900).pack(anchor="w", pady=(6, 4))
        self.live_quotes_label = ttk.Label(frame, text="Live quotes")
        self.live_quotes_label.pack(anchor="w", pady=(4, 4))
        self.live_quotes = tk.Listbox(frame, height=6, takefocus=True)
        self.live_quotes.pack(fill="x")
        self._set_live_lines(["Live quotes ще відсутні."])

        self.tickets_label = ttk.Label(frame, text="Paper tickets і результати")
        self.tickets_label.pack(anchor="w", pady=(14, 4))
        self.tickets = tk.Listbox(frame, height=7, takefocus=True)
        self.tickets.pack(fill="x")
        self.log_label = ttk.Label(frame, text="Журнал")
        self.log_label.pack(anchor="w", pady=(14, 4))
        self.log = tk.Text(frame, height=9, wrap="word", takefocus=True)
        self.log.pack(fill="both", expand=True)

        self.bind("<Control-o>", lambda _event: self.choose_dataset())
        self.bind("<Control-r>", lambda _event: self.run_dataset())
        self.bind("<Control-l>", lambda _event: self.refresh_live_snapshot())
        self.bind("<F6>", lambda _event: self.tickets.focus_set())
        self.bind("<F7>", lambda _event: self.live_quotes.focus_set())
        self._refresh_tickets()

    def _configure_accessibility(self) -> None:
        self.accessibility_strategy = tk_uia.enable(self)
        controls = (
            (self.choose_button, "Вибрати replay dataset", "Відкриває вибір папки replay dataset. Гаряча клавіша Control+O.", AUTOMATION_IDS["choose_dataset"]),
            (self.run_button, "Запустити paper replay", "Запускає causal paper replay для вибраного dataset. Гаряча клавіша Control+R.", AUTOMATION_IDS["run_replay"]),
            (self.speed, "Швидкість replay", "Вибір подієвого, 1×, 10×, 100× або 1000× режиму replay.", AUTOMATION_IDS["replay_speed"]),
            (self.live_mode, "Режим live observation", "Public preview без ключа або authenticated API key з environment.", AUTOMATION_IDS["live_mode"]),
            (self.live_refresh_button, "Оновити live snapshot", "Запускає один read-only table-tennis snapshot у worker thread. Гаряча клавіша Control+L.", AUTOMATION_IDS["live_refresh"]),
            (self.live_quotes, "Live quotes", "Поточні read-only quotes останнього snapshot. F7 переводить сюди фокус.", AUTOMATION_IDS["live_quotes"]),
            (self.tickets, "Paper tickets і результати", "Список віртуальних tickets та їх поточних результатів. F6 переводить сюди фокус.", AUTOMATION_IDS["tickets"]),
            (self.log, "Журнал виконання", "Текстовий журнал replay, observation, settlement та evaluation.", AUTOMATION_IDS["log"]),
        )
        for widget, name, description, automation_id in controls:
            tk_uia.set_acc_name(widget, name)
            tk_uia.set_acc_description(widget, description)
            tk_uia.set_automation_id(widget, automation_id)

    def _bank_text(self) -> str:
        return (
            f"Віртуальний банк: {self.session.book.balance}; "
            f"committed: {self.session.book.committed_stake}; "
            f"workspace: {self.session.workspace}"
        )

    def choose_dataset(self) -> None:
        selected = filedialog.askdirectory(title="Вибрати папку Autosport replay dataset")
        if not selected:
            return
        try:
            dataset = load_dataset(selected)
        except Exception as exc:
            messagebox.showerror("Автоспорт", f"Dataset відхилено: {exc}")
            return
        self.dataset_path = Path(selected)
        self.dataset_text.set(
            f"Dataset: {dataset.name}; sport={dataset.sport}; market SHA={dataset.market_sha256[:12]}; sealed results SHA={dataset.results_sha256[:12]}"
        )
        self.status.set("Dataset перевірено. Можна запускати replay.")

    def refresh_live_snapshot(self) -> None:
        if self._closing:
            return
        mode = self.live_mode_text.get()
        public_preview = _LIVE_MODES.get(mode)
        if public_preview is None:
            self.live_status.set("Невідомий live режим; snapshot не запущено.")
            return
        workspace = Path(self.session.workspace)

        def task():
            api_key = None if public_preview else os.environ.get("AUTOSPORT_PARLAYAPI_KEY")
            if not public_preview and not api_key:
                raise RuntimeError(
                    "AUTOSPORT_PARLAYAPI_KEY не задано в environment; виберіть Public preview або задайте ключ до запуску програми"
                )
            provider = ParlayApiTableTennisProvider(api_key, public_preview=public_preview)
            return observe_workspace_once(workspace, provider, max_items=250)

        if not self.live_worker.start(task):
            self.live_status.set("Live snapshot уже виконується; дочекайтеся завершення поточного запиту.")
            return
        self.live_refresh_button.state(["disabled"])
        self.live_status.set("Live snapshot виконується у фоновому worker; інтерфейс залишається доступним.")
        self.status.set("Виконується read-only live observation. PaperBook не змінюється.")
        self.after(100, self._poll_live_worker)

    def _poll_live_worker(self) -> None:
        if self._closing:
            return
        message = self.live_worker.poll()
        if message is None:
            self.after(100, self._poll_live_worker)
            return
        self.live_refresh_button.state(["!disabled"])
        if message.error is not None:
            text = f"Live snapshot помилка: {message.error}"
            self.live_status.set(text)
            self.status.set("Live observation не оновлено; replay/paper state не змінено.")
            self._append_log(text)
            return
        result = message.result
        if result is None:
            self.live_status.set("Live worker завершився без результату.")
            return
        summary = observation_summary(result)
        self.live_status.set(summary)
        self.status.set("Read-only live snapshot оновлено.")
        self._set_live_lines(observation_quote_lines(result))
        self._append_log(summary)

    def _set_live_lines(self, lines: list[str]) -> None:
        self.live_quotes.delete(0, "end")
        for line in lines:
            self.live_quotes.insert("end", line)

    def _append_log(self, text: str) -> None:
        self.log.insert("end", text + "\n")
        self.log.see("end")

    def run_dataset(self) -> None:
        if not self.dataset_path:
            messagebox.showinfo("Автоспорт", "Спочатку виберіть dataset.")
            return
        try:
            dataset = load_dataset(self.dataset_path)
            speed = _SPEEDS[self.speed_text.get()]
            self.status.set("Replay виконується. Strategy agents не мають доступу до sealed results.")
            self.update_idletasks()
            result = self.session.run_dataset(dataset, speed=speed)
            summary = result_summary(result)
            self.status.set(summary)
            self._append_log(summary)
            self.bank.set(self._bank_text())
            self._refresh_tickets()
        except Exception as exc:
            messagebox.showerror("Автоспорт", str(exc))
            self.status.set("Replay завершився помилкою; стан збережено fail-safe настільки, наскільки дозволив завершений transaction boundary.")

    def _refresh_tickets(self) -> None:
        self.tickets.delete(0, "end")
        for line in ticket_lines(self.session):
            self.tickets.insert("end", line)

    def close_app(self) -> None:
        self._closing = True
        try:
            self.session.close()
        finally:
            self.destroy()


def main() -> int:
    AutosportApp().mainloop()
    return 0

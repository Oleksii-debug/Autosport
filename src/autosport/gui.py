from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import tk_uia

from .dataset import load_dataset
from .paths import default_workspace
from .session import AutosportSession
from .ui_model import result_summary, ticket_lines


_SPEEDS = {"Подієвий — максимально швидко": 0.0, "1× реальний час": 1.0, "10×": 10.0, "100×": 100.0}

AUTOMATION_IDS = {
    "choose_dataset": 101,
    "run_replay": 102,
    "replay_speed": 103,
    "tickets": 201,
    "log": 202,
}


class AutosportApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Автоспорт — V1 Windows Paper Lab")
        self.geometry("900x640")
        self.minsize(700, 500)
        self.dataset_path: Path | None = None
        self.session = AutosportSession(default_workspace(), "10000")
        self.status = tk.StringVar(value="Готово. Виберіть папку replay dataset.")
        self.bank = tk.StringVar(value=self._bank_text())
        self.dataset_text = tk.StringVar(value="Dataset не вибраний.")
        self.speed_text = tk.StringVar(value="Подієвий — максимально швидко")
        self._build()
        self.update_idletasks()
        self._configure_accessibility()
        self.protocol("WM_DELETE_WINDOW", self.close_app)

    def _build(self) -> None:
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Автоспорт — V1 Windows Paper Lab", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(frame, textvariable=self.bank, wraplength=840).pack(anchor="w", pady=(12, 4))
        ttk.Label(frame, textvariable=self.dataset_text, wraplength=840).pack(anchor="w", pady=(0, 4))
        ttk.Label(frame, textvariable=self.status, wraplength=840).pack(anchor="w", pady=(0, 12))

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

        self.tickets_label = ttk.Label(frame, text="Paper tickets і результати")
        self.tickets_label.pack(anchor="w", pady=(16, 4))
        self.tickets = tk.Listbox(frame, height=9, takefocus=True)
        self.tickets.pack(fill="x")
        self.log_label = ttk.Label(frame, text="Журнал")
        self.log_label.pack(anchor="w", pady=(16, 4))
        self.log = tk.Text(frame, height=13, wrap="word", takefocus=True)
        self.log.pack(fill="both", expand=True)

        self.bind("<Control-o>", lambda _event: self.choose_dataset())
        self.bind("<Control-r>", lambda _event: self.run_dataset())
        self.bind("<F6>", lambda _event: self.tickets.focus_set())
        self._refresh_tickets()

    def _configure_accessibility(self) -> None:
        self.accessibility_strategy = tk_uia.enable(self)
        controls = (
            (self.choose_button, "Вибрати replay dataset", "Відкриває вибір папки replay dataset. Гаряча клавіша Control+O.", AUTOMATION_IDS["choose_dataset"]),
            (self.run_button, "Запустити paper replay", "Запускає causal paper replay для вибраного dataset. Гаряча клавіша Control+R.", AUTOMATION_IDS["run_replay"]),
            (self.speed, "Швидкість replay", "Вибір подієвого, 1×, 10× або 100× режиму replay.", AUTOMATION_IDS["replay_speed"]),
            (self.tickets, "Paper tickets і результати", "Список віртуальних tickets та їх поточних результатів. F6 переводить сюди фокус.", AUTOMATION_IDS["tickets"]),
            (self.log, "Журнал виконання", "Текстовий журнал replay, settlement та evaluation.", AUTOMATION_IDS["log"]),
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
            self.log.insert("end", summary + "\n")
            self.log.see("end")
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
        try:
            self.session.close()
        finally:
            self.destroy()


def main() -> int:
    AutosportApp().mainloop()
    return 0

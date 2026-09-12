from __future__ import annotations

import tkinter as tk
from decimal import Decimal
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .agents import AgentContext, AgentOrchestrator, MarketMirrorAgent, PaperBaselineAgent
from .paper import PaperBook
from .replay import ReplayEngine


class AutosportApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Автоспорт — V1 Paper Lab")
        self.geometry("760x520")
        self.book = PaperBook(Decimal("10000"))
        self.context = AgentContext(self.book)
        self.orchestrator = AgentOrchestrator([MarketMirrorAgent(), PaperBaselineAgent()], self.context)
        self.replay_path: Path | None = None
        self.status = tk.StringVar(value="Готово. Завантажте історичний JSONL replay.")
        self.bank = tk.StringVar(value=self._bank_text())
        self._build()

    def _build(self) -> None:
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Автоспорт — V1 Paper Lab", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(frame, textvariable=self.bank).pack(anchor="w", pady=(12, 6))
        ttk.Label(frame, textvariable=self.status, wraplength=700).pack(anchor="w", pady=(0, 12))
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="Завантажити replay JSONL", command=self.load_replay).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Запустити replay", command=self.run_replay).pack(side="left", padx=(0, 8))
        ttk.Button(buttons, text="Очистити журнал", command=lambda: self.log.delete("1.0", "end")).pack(side="left")
        ttk.Label(frame, text="Журнал подій").pack(anchor="w", pady=(16, 4))
        self.log = tk.Text(frame, height=18, wrap="word", takefocus=True)
        self.log.pack(fill="both", expand=True)
        self.bind("<Control-o>", lambda _event: self.load_replay())
        self.bind("<Control-r>", lambda _event: self.run_replay())

    def _bank_text(self) -> str:
        open_count = sum(t.status.value == "open" for t in self.book.tickets.values())
        return f"Віртуальний банк: {self.book.balance} | Відкрито tickets: {open_count}"

    def load_replay(self) -> None:
        selected = filedialog.askopenfilename(title="Вибрати replay JSONL", filetypes=[("JSON Lines", "*.jsonl"), ("All files", "*.*")])
        if selected:
            self.replay_path = Path(selected)
            self.status.set(f"Завантажено: {self.replay_path.name}")

    def run_replay(self) -> None:
        if not self.replay_path:
            messagebox.showinfo("Автоспорт", "Спочатку виберіть replay JSONL.")
            return
        try:
            engine = ReplayEngine.from_jsonl(self.replay_path)
            run = engine.run(self._on_event, speed=0)
            self.context.replay_run_id = run.run_id
            self.status.set(
                f"Replay завершено: {run.event_count} events; dataset {run.dataset_hash[:12]}. Майбутній результат був sealed до завершення."
            )
            self.bank.set(self._bank_text())
        except Exception as exc:
            messagebox.showerror("Автоспорт", str(exc))

    def _on_event(self, event) -> None:
        self.orchestrator.on_market_event(event)
        self.log.insert("end", f"{event.observed_ts} | {event.event_id} | {event.market_id} | {event.selection_id} | {event.decimal_odds}\n")
        self.log.see("end")
        self.update_idletasks()


def main() -> int:
    AutosportApp().mainloop()
    return 0

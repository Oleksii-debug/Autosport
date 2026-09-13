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
from .replay_worker import (
    OneShotReplayWorker,
    run_workspace_dataset_once,
    workspace_for_strategy,
)
from .research_strategy import ResearchStrategyPlan
from .session import AutosportSession
from .strategies import available_strategies, strategy_spec
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
_STRATEGY_IDS = tuple(spec.strategy_id for spec in available_strategies())

AUTOMATION_IDS = {
    "choose_dataset": 101,
    "run_replay": 102,
    "replay_speed": 103,
    "live_mode": 104,
    "live_refresh": 105,
    "strategy": 106,
    "research_plan": 107,
    "tickets": 201,
    "log": 202,
    "live_quotes": 203,
}


class AutosportApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Автоспорт — V1 Windows Paper Lab")
        self.geometry("960x840")
        self.minsize(760, 660)
        self.dataset_path: Path | None = None
        self.workspace_root = default_workspace()
        # Backward-compatible name for the shared live-observation workspace root.
        self.workspace = self.workspace_root
        self.active_workspace = self.workspace_root
        self.active_strategy_id = "baseline-v1"
        self.active_research_plan: ResearchStrategyPlan | None = None
        self.research_plan_path: Path | None = None
        self.research_plan: ResearchStrategyPlan | None = None
        self.session: AutosportSession | None = AutosportSession(
            self.active_workspace,
            "10000",
            strategy_id=self.active_strategy_id,
        )
        self.replay_worker = OneShotReplayWorker()
        self.live_worker = OneShotObservationWorker()
        self._closing = False
        self.status = tk.StringVar(value="Готово. Виберіть папку replay dataset або оновіть live snapshot.")
        self.bank = tk.StringVar(value=self._bank_text())
        self.dataset_text = tk.StringVar(value="Dataset не вибраний.")
        self.speed_text = tk.StringVar(value="Подієвий — максимально швидко")
        self.strategy_text = tk.StringVar(value="baseline-v1")
        self.research_plan_text = tk.StringVar(value="Research plan: не потрібен для baseline-v1.")
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

        strategy_controls = ttk.Frame(frame)
        strategy_controls.pack(fill="x", pady=(12, 0))
        self.strategy_label = ttk.Label(strategy_controls, text="Strategy:")
        self.strategy_label.pack(side="left", padx=(0, 4))
        self.strategy = ttk.Combobox(
            strategy_controls,
            textvariable=self.strategy_text,
            values=list(_STRATEGY_IDS),
            state="readonly",
            width=24,
            takefocus=True,
        )
        self.strategy.pack(side="left", padx=(0, 8))
        self.strategy.bind("<<ComboboxSelected>>", self._on_strategy_changed)
        self.research_plan_button = ttk.Button(
            strategy_controls,
            text="Вибрати research plan",
            command=self.choose_research_plan,
        )
        self.research_plan_button.pack(side="left")
        ttk.Label(frame, textvariable=self.research_plan_text, wraplength=900).pack(anchor="w", pady=(5, 0))

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
            (self.strategy, "Strategy replay", "Вибір canonical deterministic strategy: baseline, observe-only або typed research replay.", AUTOMATION_IDS["strategy"]),
            (self.research_plan_button, "Вибрати research plan", "Вибирає typed causal JSON plan для strategy research-replay-v1. План прив'язується до replay за SHA-256.", AUTOMATION_IDS["research_plan"]),
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
        if self.session is None:
            return f"Віртуальний банк: economic state тимчасово недоступний; workspace: {self.active_workspace}"
        return (
            f"Віртуальний банк: {self.session.book.balance}; "
            f"committed: {self.session.book.committed_stake}; "
            f"strategy: {self.active_strategy_id}; workspace: {self.session.workspace}"
        )

    def _selected_strategy_id(self) -> str:
        strategy_id = self.strategy_text.get()
        if strategy_id not in _STRATEGY_IDS:
            raise ValueError(f"Невідома strategy: {strategy_id}")
        return strategy_id

    def _selected_configuration(self) -> tuple[str, ResearchStrategyPlan | None]:
        strategy_id = self._selected_strategy_id()
        spec = strategy_spec(strategy_id)
        if spec.requires_research_plan:
            if self.research_plan is None:
                raise ValueError(
                    "research-replay-v1 потребує typed research plan. Натисніть «Вибрати research plan»."
                )
            return strategy_id, self.research_plan
        return strategy_id, None

    def _activate_configuration(
        self,
        strategy_id: str,
        research_plan: ResearchStrategyPlan | None,
    ) -> None:
        workspace = workspace_for_strategy(self.workspace_root, strategy_id, research_plan)
        if self.session is not None:
            self.session.close()
        self.active_workspace = workspace
        self.active_strategy_id = strategy_id
        self.active_research_plan = research_plan
        self.session = AutosportSession(
            workspace,
            "10000",
            strategy_id=strategy_id,
            research_plan=research_plan,
        )
        self.bank.set(self._bank_text())
        self._refresh_tickets()

    def _on_strategy_changed(self, _event=None) -> None:
        if self.replay_worker.busy:
            self.strategy_text.set(self.active_strategy_id)
            self.status.set("Strategy не можна змінювати під час active economic replay.")
            return
        try:
            strategy_id = self._selected_strategy_id()
            spec = strategy_spec(strategy_id)
        except Exception as exc:
            self.status.set(str(exc))
            return
        if spec.requires_research_plan and self.research_plan is None:
            if self.session is not None:
                self.session.close()
                self.session = None
            self.active_strategy_id = strategy_id
            self.active_research_plan = None
            self.active_workspace = self.workspace_root / "strategies" / "research-replay-v1-pending-plan"
            self.research_plan_text.set("Research plan: обов'язковий для research-replay-v1; виберіть JSON plan.")
            self.bank.set(self._bank_text())
            self._refresh_tickets()
            self.status.set("Research strategy вибрана, але replay заблоковано до валідного typed research plan.")
            return
        research_plan = self.research_plan if spec.requires_research_plan else None
        try:
            self._activate_configuration(strategy_id, research_plan)
        except Exception as exc:
            self.status.set(f"Strategy configuration відхилено: {exc}")
            return
        if spec.requires_research_plan and research_plan is not None:
            self.research_plan_text.set(
                f"Research plan: {self.research_plan_path}; SHA-256={research_plan.source_sha256[:16]}…"
            )
        else:
            self.research_plan_text.set(f"Research plan: не використовується strategy {strategy_id}.")
        self.status.set(f"Strategy активна: {strategy_id}. Economic workspace ізольовано за strategy identity.")

    def choose_research_plan(self) -> None:
        if self.replay_worker.busy:
            self.status.set("Research plan не можна змінювати під час active economic replay.")
            return
        try:
            strategy_id = self._selected_strategy_id()
        except Exception as exc:
            self.status.set(str(exc))
            return
        if not strategy_spec(strategy_id).requires_research_plan:
            self.status.set("Поточна strategy не використовує research plan; спочатку виберіть research-replay-v1.")
            return
        selected = filedialog.askopenfilename(
            title="Вибрати Autosport typed research plan",
            filetypes=(("JSON", "*.json"), ("Усі файли", "*.*")),
        )
        if not selected:
            return
        try:
            plan = ResearchStrategyPlan.from_path(selected)
        except Exception as exc:
            messagebox.showerror("Автоспорт", f"Research plan відхилено: {exc}")
            self.status.set("Research plan не активовано; economic state не змінено.")
            return
        self.research_plan_path = Path(selected)
        self.research_plan = plan
        try:
            self._activate_configuration(strategy_id, plan)
        except Exception as exc:
            messagebox.showerror("Автоспорт", f"Strategy workspace відхилено: {exc}")
            self.status.set("Research strategy не активовано.")
            return
        self.research_plan_text.set(
            f"Research plan: {self.research_plan_path}; SHA-256={plan.source_sha256[:16]}…"
        )
        self.status.set(
            "Typed research plan перевірено. Research replay використовуватиме exact plan object/hash у власному economic workspace."
        )

    def choose_dataset(self) -> None:
        if self.replay_worker.busy:
            self.status.set("Replay уже виконується; вибір іншого dataset доступний після завершення поточного run.")
            return
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
        if self.replay_worker.busy:
            self.live_status.set("Live snapshot відкладено: economic replay уже виконується у цьому workspace.")
            return
        mode = self.live_mode_text.get()
        public_preview = _LIVE_MODES.get(mode)
        if public_preview is None:
            self.live_status.set("Невідомий live режим; snapshot не запущено.")
            return
        workspace = self.workspace_root

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

    def _set_replay_controls_busy(self, busy: bool) -> None:
        if busy:
            self.choose_button.state(["disabled"])
            self.run_button.state(["disabled"])
            self.speed.configure(state="disabled")
            self.strategy.configure(state="disabled")
            self.research_plan_button.state(["disabled"])
            self.live_mode.configure(state="disabled")
            self.live_refresh_button.state(["disabled"])
            return
        self.choose_button.state(["!disabled"])
        self.run_button.state(["!disabled"])
        self.speed.configure(state="readonly")
        self.strategy.configure(state="readonly")
        self.research_plan_button.state(["!disabled"])
        self.live_mode.configure(state="readonly")
        self.live_refresh_button.state(["!disabled"])

    def run_dataset(self) -> None:
        if not self.dataset_path:
            messagebox.showinfo("Автоспорт", "Спочатку виберіть dataset.")
            return
        if self.replay_worker.busy:
            self.status.set("Paper replay уже виконується; дочекайтеся його terminal state.")
            return
        if self.live_worker.busy:
            self.status.set("Live snapshot ще виконується; paper replay почнеться лише після його завершення.")
            return
        try:
            strategy_id, research_plan = self._selected_configuration()
            workspace = workspace_for_strategy(self.workspace_root, strategy_id, research_plan)
        except Exception as exc:
            messagebox.showerror("Автоспорт", str(exc))
            self.status.set("Replay не запущено: strategy configuration incomplete/invalid.")
            self.research_plan_button.focus_set()
            return

        dataset_path = self.dataset_path
        speed = _SPEEDS[self.speed_text.get()]
        self.active_workspace = workspace
        self.active_strategy_id = strategy_id
        self.active_research_plan = research_plan
        if self.session is not None:
            self.session.close()
            self.session = None

        def task():
            return run_workspace_dataset_once(
                workspace,
                dataset_path,
                initial_bankroll="10000",
                speed=speed,
                strategy_id=strategy_id,
                research_plan=research_plan,
            )

        if not self.replay_worker.start(task):
            self.session = AutosportSession(
                workspace,
                "10000",
                strategy_id=strategy_id,
                research_plan=research_plan,
            )
            self.status.set("Paper replay уже виконується; новий run не запущено.")
            return

        self._set_replay_controls_busy(True)
        self.bank.set(self._bank_text())
        self._refresh_tickets()
        self.status.set(
            f"Replay strategy={strategy_id} виконується у фоновому worker. Клавіатура, фокус, F6/F7 і журнал залишаються доступними; "
            "закриття програми заблоковано до завершення economic transaction boundary."
        )
        self._append_log(
            f"Paper replay запущено у background worker; strategy={strategy_id}; workspace={workspace}; Tk/UIA thread не блокується."
        )
        self.after(100, self._poll_replay_worker)

    def _reopen_active_session(self) -> None:
        self.session = AutosportSession(
            self.active_workspace,
            "10000",
            strategy_id=self.active_strategy_id,
            research_plan=self.active_research_plan,
        )
        self.bank.set(self._bank_text())
        self._refresh_tickets()

    def _poll_replay_worker(self) -> None:
        message = self.replay_worker.poll()
        if message is None:
            self.after(100, self._poll_replay_worker)
            return

        self._set_replay_controls_busy(False)
        try:
            self._reopen_active_session()
        except Exception as exc:
            text = f"Replay завершено, але strategy workspace не вдалося перевідкрити: {exc}"
            self._append_log(text)
            self.status.set(text)
            messagebox.showerror("Автоспорт", text)
            return

        if message.error is not None:
            text = f"Paper replay помилка: {message.error}"
            self._append_log(text)
            self.status.set(
                "Replay завершився помилкою; UI знову доступний. Якщо workspace має unresolved transaction, "
                "виконайте repair-workspace перед наступним economic run."
            )
            messagebox.showerror("Автоспорт", text)
            return

        result = message.result
        if result is None:
            self.status.set("Replay worker завершився без terminal result; новий run не запускайте до перевірки workspace.")
            return
        summary = result_summary(result)
        self.status.set(f"strategy={self.active_strategy_id}; {summary}")
        self._append_log(f"strategy={self.active_strategy_id}; {summary}")

    def _refresh_tickets(self) -> None:
        self.tickets.delete(0, "end")
        if self.session is None:
            if self.replay_worker.busy:
                self.tickets.insert("end", "Replay виконується; ticket state оновиться після завершення transaction.")
            else:
                self.tickets.insert("end", "Economic state недоступний до завершення strategy configuration.")
            return
        for line in ticket_lines(self.session):
            self.tickets.insert("end", line)

    def close_app(self) -> None:
        if self.replay_worker.busy:
            text = (
                "Paper replay ще виконується. Закриття програми заблоковано до завершення economic transaction boundary, "
                "щоб процес не обірвав commit у довільній точці."
            )
            self.status.set(text)
            self._append_log(text)
            self.bell()
            return
        self._closing = True
        try:
            if self.session is not None:
                self.session.close()
                self.session = None
        finally:
            self.destroy()


def main() -> int:
    AutosportApp().mainloop()
    return 0
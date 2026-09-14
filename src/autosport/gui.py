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
from .recovery import reconcile_late_crashes
from .replay_worker import (
    OneShotReplayWorker,
    run_workspace_dataset_once,
    workspace_for_strategy,
)
from .research_strategy import ResearchStrategyPlan
from .session import AutosportSession
from .strategies import (
    available_strategies,
    strategy_spec,
    validate_strategy_configuration,
)
from .ui_model import (
    evaluation_lines,
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
_STRATEGY_CHOICES = {
    f"{spec.label} — {spec.strategy_id}": spec.strategy_id
    for spec in available_strategies()
}
_DEFAULT_STRATEGY_TEXT = next(
    label for label, strategy_id in _STRATEGY_CHOICES.items()
    if strategy_id == "baseline-v1"
)

AUTOMATION_IDS = {
    "choose_dataset": 101,
    "run_replay": 102,
    "replay_speed": 103,
    "live_mode": 104,
    "live_refresh": 105,
    "strategy": 106,
    "research_plan": 107,
    "repair_workspace": 108,
    "tickets": 201,
    "log": 202,
    "live_quotes": 203,
    "evaluation": 204,
}


def strategy_id_from_display(display: str) -> str:
    try:
        return _STRATEGY_CHOICES[display]
    except KeyError as exc:
        raise ValueError(f"Невідома canonical strategy: {display!r}") from exc


class AutosportApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Автоспорт — V1 Windows Paper Lab")
        self.geometry("1080x860")
        self.minsize(820, 680)
        self.dataset_path: Path | None = None
        self.research_plan_path: Path | None = None
        self.research_plan: ResearchStrategyPlan | None = None
        self.workspace = default_workspace()
        self._active_workspace = self.workspace
        self._recovery_required_workspaces: set[Path] = set()
        self._startup_economic_error: str | None = None
        self.session: AutosportSession | None = None
        try:
            self.session = AutosportSession(self.workspace, "10000")
        except Exception as exc:
            self._block_workspace_for_recovery(Path(self.workspace))
            self._startup_economic_error = f"{type(exc).__name__}: {exc}"
        self.replay_worker = OneShotReplayWorker()
        self.live_worker = OneShotObservationWorker()
        self._active_strategy_id = "baseline-v1"
        self._active_research_plan: ResearchStrategyPlan | None = None
        self._closing = False
        startup_status = (
            "Готово. Виберіть папку replay dataset або оновіть live snapshot."
            if self._startup_economic_error is None
            else (
                "Economic session не пройшла startup validation; paper replay для baseline workspace "
                "заблоковано до recovery. Read-only live snapshot доступний через Control+L."
            )
        )
        self.status = tk.StringVar(value=startup_status)
        self.bank = tk.StringVar(value=self._bank_text())
        self.dataset_text = tk.StringVar(value="Dataset не вибраний.")
        self.strategy_text = tk.StringVar(value=_DEFAULT_STRATEGY_TEXT)
        self.strategy_status = tk.StringVar(value=self._strategy_status_text())
        self.research_plan_text = tk.StringVar(value="Research plan: не потрібен для baseline-v1.")
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
        ttk.Label(frame, textvariable=self.bank, wraplength=1000).pack(anchor="w", pady=(12, 4))
        ttk.Label(frame, textvariable=self.dataset_text, wraplength=1000).pack(anchor="w", pady=(0, 4))
        ttk.Label(frame, textvariable=self.status, wraplength=1000).pack(anchor="w", pady=(0, 10))

        strategy_controls = ttk.Frame(frame)
        strategy_controls.pack(fill="x", pady=(0, 4))
        self.strategy_label = ttk.Label(strategy_controls, text="Стратегія:")
        self.strategy_label.pack(side="left", padx=(0, 4))
        self.strategy = ttk.Combobox(
            strategy_controls,
            textvariable=self.strategy_text,
            values=list(_STRATEGY_CHOICES),
            state="readonly",
            width=40,
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
        ttk.Label(frame, textvariable=self.strategy_status, wraplength=1000).pack(anchor="w", pady=(2, 2))
        ttk.Label(frame, textvariable=self.research_plan_text, wraplength=1000).pack(anchor="w", pady=(0, 8))

        controls = ttk.Frame(frame)
        controls.pack(fill="x")
        self.choose_button = ttk.Button(controls, text="Вибрати dataset", command=self.choose_dataset)
        self.choose_button.pack(side="left", padx=(0, 8))
        self.run_button = ttk.Button(controls, text="Запустити paper replay", command=self.run_dataset)
        self.run_button.pack(side="left", padx=(0, 8))
        self.repair_button = ttk.Button(
            controls,
            text="Відновити workspace",
            command=self.repair_workspace,
        )
        self.repair_button.pack(side="left", padx=(0, 8))
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
        ttk.Label(frame, textvariable=self.live_status, wraplength=1000).pack(anchor="w", pady=(6, 4))
        self.live_quotes_label = ttk.Label(frame, text="Live quotes")
        self.live_quotes_label.pack(anchor="w", pady=(4, 4))
        self.live_quotes = tk.Listbox(frame, height=6, takefocus=True)
        self.live_quotes.pack(fill="x")
        self._set_live_lines(["Live quotes ще відсутні."])

        self.tickets_label = ttk.Label(frame, text="Paper tickets і результати")
        self.tickets_label.pack(anchor="w", pady=(14, 4))
        self.tickets = tk.Listbox(frame, height=7, takefocus=True)
        self.tickets.pack(fill="x")
        self.evaluation_label = ttk.Label(frame, text="Evaluation і portfolio evidence")
        self.evaluation_label.pack(anchor="w", pady=(14, 4))
        self.evaluation = tk.Listbox(frame, height=5, takefocus=True)
        self.evaluation.pack(fill="x")
        self._set_evaluation_lines(["Evaluation ще відсутня. Запустіть paper replay."])
        self.log_label = ttk.Label(frame, text="Журнал")
        self.log_label.pack(anchor="w", pady=(14, 4))
        self.log = tk.Text(frame, height=9, wrap="word", takefocus=True)
        self.log.pack(fill="both", expand=True)

        self.bind("<Control-o>", lambda _event: self.choose_dataset())
        self.bind("<Control-r>", lambda _event: self.run_dataset())
        self.bind("<Control-Shift-R>", lambda _event: self.repair_workspace())
        self.bind("<Control-l>", lambda _event: self.refresh_live_snapshot())
        self.bind("<F6>", lambda _event: self.tickets.focus_set())
        self.bind("<F7>", lambda _event: self.live_quotes.focus_set())
        self.bind("<F8>", lambda _event: self.evaluation.focus_set())
        self._refresh_tickets()

    def _configure_accessibility(self) -> None:
        self.accessibility_strategy = tk_uia.enable(self)
        controls = (
            (self.strategy, "Стратегія replay", "Canonical selectable strategy implementation. Для Typed research replay потрібен research plan.", AUTOMATION_IDS["strategy"]),
            (self.research_plan_button, "Вибрати research plan", "Вибирає та валідовує typed causal research-plan JSON для research-replay-v1.", AUTOMATION_IDS["research_plan"]),
            (self.choose_button, "Вибрати replay dataset", "Відкриває вибір папки replay dataset. Гаряча клавіша Control+O.", AUTOMATION_IDS["choose_dataset"]),
            (self.run_button, "Запустити paper replay", "Запускає causal paper replay для вибраного dataset і canonical strategy. Гаряча клавіша Control+R.", AUTOMATION_IDS["run_replay"]),
            (self.repair_button, "Відновити workspace", "Запускає fail-closed crash recovery для workspace вибраної canonical strategy. Гаряча клавіша Control+Shift+R.", AUTOMATION_IDS["repair_workspace"]),
            (self.speed, "Швидкість replay", "Вибір подієвого, 1×, 10×, 100× або 1000× режиму replay.", AUTOMATION_IDS["replay_speed"]),
            (self.live_mode, "Режим live observation", "Public preview без ключа або authenticated API key з environment.", AUTOMATION_IDS["live_mode"]),
            (self.live_refresh_button, "Оновити live snapshot", "Запускає один read-only table-tennis snapshot у worker thread. Гаряча клавіша Control+L.", AUTOMATION_IDS["live_refresh"]),
            (self.live_quotes, "Live quotes", "Поточні read-only quotes останнього snapshot. F7 переводить сюди фокус.", AUTOMATION_IDS["live_quotes"]),
            (self.tickets, "Paper tickets і результати", "Список віртуальних tickets та їх поточних результатів. F6 переводить сюди фокус.", AUTOMATION_IDS["tickets"]),
            (self.evaluation, "Evaluation і portfolio evidence", "Підсумок останнього terminal paper replay: bankroll, ROI, ticket outcomes і truth-labelled portfolio scenarios. F8 переводить сюди фокус.", AUTOMATION_IDS["evaluation"]),
            (self.log, "Журнал виконання", "Текстовий журнал replay, observation, settlement та evaluation.", AUTOMATION_IDS["log"]),
        )
        for widget, name, description, automation_id in controls:
            tk_uia.set_acc_name(widget, name)
            tk_uia.set_acc_description(widget, description)
            tk_uia.set_automation_id(widget, automation_id)

    def _strategy_status_text(self) -> str:
        strategy_id = strategy_id_from_display(self.strategy_text.get())
        spec = strategy_spec(strategy_id)
        plan_requirement = "research plan обов’язковий" if spec.requires_research_plan else "research plan не потрібен"
        ticket_mode = "може відкривати paper tickets" if spec.opens_paper_tickets else "paper tickets не відкриває"
        return (
            f"Strategy: {spec.strategy_id}; {spec.label}; {plan_requirement}; {ticket_mode}. "
            "Economic history одного workspace не змішується між різними strategy/plan identities."
        )

    def _on_strategy_changed(self, _event=None) -> None:
        if self.replay_worker.busy:
            return
        strategy_id = strategy_id_from_display(self.strategy_text.get())
        spec = strategy_spec(strategy_id)
        if not spec.requires_research_plan:
            self.research_plan = None
            self.research_plan_path = None
            self.research_plan_text.set(f"Research plan: не потрібен для {strategy_id}.")
        elif self.research_plan is None:
            self.research_plan_text.set("Research plan: обов’язковий; файл ще не вибраний.")
        self.strategy_status.set(self._strategy_status_text())
        self.status.set(f"Вибрано canonical strategy {strategy_id}.")

    def choose_research_plan(self) -> None:
        if self.replay_worker.busy:
            self.status.set("Research plan не можна змінювати під час economic replay.")
            return
        strategy_id = strategy_id_from_display(self.strategy_text.get())
        spec = strategy_spec(strategy_id)
        if not spec.requires_research_plan:
            messagebox.showinfo(
                "Автоспорт",
                f"{strategy_id} не використовує research plan. Виберіть Typed research replay.",
            )
            self.status.set(f"{strategy_id}: research plan не потрібен.")
            return
        selected = filedialog.askopenfilename(
            title="Вибрати Autosport research plan",
            filetypes=(("JSON files", "*.json"), ("All files", "*.*")),
        )
        if not selected:
            return
        try:
            plan = ResearchStrategyPlan.from_path(selected)
            validate_strategy_configuration(strategy_id, plan)
        except Exception as exc:
            messagebox.showerror("Автоспорт", f"Research plan відхилено: {exc}")
            self.status.set("Research plan не змінено: файл не пройшов fail-closed validation.")
            return
        self.research_plan_path = Path(selected)
        self.research_plan = plan
        self.research_plan_text.set(
            f"Research plan: {self.research_plan_path.name}; SHA-256={plan.source_sha256}"
        )
        self.status.set(
            f"Research plan перевірено і прив’язано до {strategy_id}; SHA-256={plan.source_sha256[:12]}…"
        )

    def _selected_replay_configuration(self) -> tuple[str, ResearchStrategyPlan | None]:
        strategy_id = strategy_id_from_display(self.strategy_text.get())
        spec = strategy_spec(strategy_id)
        plan = self.research_plan if spec.requires_research_plan else None
        validate_strategy_configuration(strategy_id, plan)
        return strategy_id, plan

    def _open_session(
        self,
        strategy_id: str = "baseline-v1",
        research_plan: ResearchStrategyPlan | None = None,
    ) -> AutosportSession:
        workspace = workspace_for_strategy(self.workspace, strategy_id, research_plan)
        self._active_workspace = workspace
        return AutosportSession(
            workspace,
            "10000",
            strategy_id=strategy_id,
            research_plan=research_plan,
        )

    def _bank_text(self) -> str:
        if self.session is None:
            if self.__dict__.get("_startup_economic_error"):
                return (
                    "Віртуальний банк: недоступний до успішного recovery; "
                    f"workspace: {self._active_workspace}"
                )
            return f"Віртуальний банк: оновлюється після replay; workspace: {self._active_workspace}"
        return (
            f"Віртуальний банк: {self.session.book.balance}; "
            f"committed: {self.session.book.committed_stake}; "
            f"strategy: {self.session.strategy_id}; "
            f"workspace: {self.session.workspace}"
        )

    def _block_workspace_for_recovery(self, workspace: Path) -> None:
        self._recovery_required_workspaces.add(Path(workspace))

    def _hide_uncertain_economic_state(self, ticket_message: str) -> bool:
        session = self.session
        self.session = None
        self.bank.set(
            "Віртуальний банк: недоступний до підтвердженого terminal state/recovery; "
            f"workspace: {self._active_workspace}"
        )
        self.tickets.delete(0, "end")
        self.tickets.insert("end", ticket_message)
        if session is None:
            return True
        try:
            session.close()
        except Exception as exc:
            session_workspace = Path(session.workspace)
            self._recovery_required_workspaces.add(session_workspace)
            self._append_log(
                "Economic session teardown після quarantine завершився помилкою; "
                f"workspace={session_workspace}; secondary={type(exc).__name__}: {exc}"
            )
            return False
        return True

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
        workspace = self.workspace

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

    def _set_evaluation_lines(self, lines: list[str]) -> None:
        self.evaluation.delete(0, "end")
        for line in lines:
            self.evaluation.insert("end", line)

    def _append_log(self, text: str) -> None:
        self.log.insert("end", text + "\n")
        self.log.see("end")

    def _set_replay_controls_busy(self, busy: bool) -> None:
        if busy:
            self.strategy.configure(state="disabled")
            self.research_plan_button.state(["disabled"])
            self.choose_button.state(["disabled"])
            self.run_button.state(["disabled"])
            self.repair_button.state(["disabled"])
            self.speed.configure(state="disabled")
            self.live_mode.configure(state="disabled")
            self.live_refresh_button.state(["disabled"])
            return
        self.strategy.configure(state="readonly")
        self.research_plan_button.state(["!disabled"])
        self.choose_button.state(["!disabled"])
        self.run_button.state(["!disabled"])
        self.repair_button.state(["!disabled"])
        self.speed.configure(state="readonly")
        self.live_mode.configure(state="readonly")
        self.live_refresh_button.state(["!disabled"])

    def repair_workspace(self) -> None:
        if self._closing:
            return
        if self.replay_worker.busy:
            self.status.set("Recovery заблоковано: economic replay ще виконується.")
            return
        if self.live_worker.busy:
            self.status.set("Recovery заблоковано: live snapshot ще виконується.")
            return
        try:
            strategy_id, research_plan = self._selected_replay_configuration()
            replay_workspace = workspace_for_strategy(self.workspace, strategy_id, research_plan)
        except Exception as exc:
            messagebox.showerror("Автоспорт", f"Recovery configuration відхилено: {exc}")
            self.status.set("Recovery не запущено: canonical strategy configuration не пройшла fail-closed validation.")
            return

        replay_workspace = Path(replay_workspace)
        self._active_workspace = replay_workspace
        self._active_strategy_id = strategy_id
        self._active_research_plan = research_plan
        self._recovery_required_workspaces.add(replay_workspace)
        teardown_succeeded = self._hide_uncertain_economic_state(
            "Workspace recovery виконується; economic session state недоступний до завершення перевірки."
        )
        if not teardown_succeeded:
            detail = (
                "Workspace recovery відхилено fail-closed: previous economic session teardown failed; "
                "reconciliation/reopen не запускаються."
            )
            self.status.set(
                "Workspace recovery не завершено: previous economic session teardown failed; "
                "economic state лишається недоступним, а новий replay заблоковано."
            )
            self._append_log(detail)
            messagebox.showerror("Автоспорт", detail)
            return

        try:
            report = reconcile_late_crashes(replay_workspace)
        except Exception as exc:
            self._recovery_required_workspaces.add(replay_workspace)
            self._hide_uncertain_economic_state(
                "Workspace recovery не завершено; economic session state недоступний."
            )
            detail = f"Workspace recovery відхилено fail-closed: {exc}"
            self.status.set(
                "Workspace recovery не завершено; economic state лишається недоступним, "
                "а новий replay заблоковано до усунення причини."
            )
            self._append_log(detail)
            messagebox.showerror("Автоспорт", detail)
            return

        summary = (
            "Workspace recovery: "
            f"reconciled={len(report.reconciled_keys)}; "
            f"aborted_uncommitted={len(report.aborted_uncommitted_keys)}; "
            f"unresolved={len(report.unresolved_without_summary)}; "
            f"workspace={replay_workspace}"
        )
        self._append_log(summary)
        if report.unresolved_without_summary:
            self._recovery_required_workspaces.add(replay_workspace)
            self._hide_uncertain_economic_state(
                "Workspace recovery має unresolved run; economic session state недоступний."
            )
            self.status.set(
                summary
                + ". Є unresolved legacy run без достатнього summary proof; economic state приховано, "
                "а replay лишається fail-closed для конфліктного experiment."
            )
            messagebox.showwarning(
                "Автоспорт",
                "Recovery завершив перевірку, але залишив unresolved run без достатнього доказу completion. "
                "Economic state не публікується; не обходьте цей стан через allow-repeat.",
            )
            return

        try:
            self.session = self._open_session(strategy_id, research_plan)
        except Exception as exc:
            self._recovery_required_workspaces.add(replay_workspace)
            self._hide_uncertain_economic_state(
                "Recovery завершено, але economic session state не пройшов reopen validation."
            )
            detail = (
                "Post-recovery workspace reopen відхилено fail-closed: "
                f"{type(exc).__name__}: {exc}"
            )
            self.status.set(
                "Recovery reconciliation завершено, але economic session state лишається недоступним; "
                "новий replay заблоковано до успішного recovery/reopen."
            )
            self._append_log(detail)
            messagebox.showerror("Автоспорт", detail)
            return

        self._startup_economic_error = None
        self.bank.set(self._bank_text())
        self._refresh_tickets()
        self._recovery_required_workspaces.discard(replay_workspace)
        self.status.set(summary + ". Workspace готовий до наступного перевіреного paper replay.")
        messagebox.showinfo("Автоспорт", "Workspace recovery завершено без unresolved runs.")

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
            strategy_id, research_plan = self._selected_replay_configuration()
            replay_workspace = Path(workspace_for_strategy(self.workspace, strategy_id, research_plan))
        except Exception as exc:
            messagebox.showerror("Автоспорт", f"Strategy configuration відхилено: {exc}")
            self.status.set("Replay не запущено: canonical strategy configuration не пройшла fail-closed validation.")
            return

        if replay_workspace in self._recovery_required_workspaces:
            self._active_workspace = replay_workspace
            text = (
                "Paper replay заблоковано: цей economic workspace має непідтверджений terminal state. "
                "Виконайте «Відновити workspace» або Control+Shift+R; новий run дозволяється лише після "
                "успішного recovery без unresolved runs."
            )
            self.status.set(text)
            self._append_log(text)
            messagebox.showwarning("Автоспорт", text)
            return

        dataset_path = self.dataset_path
        speed = _SPEEDS[self.speed_text.get()]
        self._active_workspace = replay_workspace
        if self.session is not None:
            self.session.close()
            self.session = None

        def task():
            return run_workspace_dataset_once(
                replay_workspace,
                dataset_path,
                initial_bankroll="10000",
                speed=speed,
                strategy_id=strategy_id,
                research_plan=research_plan,
            )

        if not self.replay_worker.start(task):
            self.session = self._open_session(strategy_id, research_plan)
            self.status.set("Paper replay уже виконується; новий run не запущено.")
            return

        self._active_strategy_id = strategy_id
        self._active_research_plan = research_plan
        self._set_replay_controls_busy(True)
        self.bank.set(self._bank_text())
        self._refresh_tickets()
        self._set_evaluation_lines([
            "Replay виконується; evaluation оновиться лише після terminal settlement/evaluation boundary."
        ])
        plan_identity = (
            f"; plan={research_plan.source_sha256[:12]}…"
            if research_plan is not None
            else ""
        )
        self.status.set(
            f"Replay виконується у фоновому worker; strategy={strategy_id}{plan_identity}. "
            "Клавіатура, фокус, F6/F7/F8 і журнал залишаються доступними; "
            "закриття програми заблоковано до завершення economic transaction boundary."
        )
        self._append_log(
            f"Paper replay запущено у background worker; strategy={strategy_id}{plan_identity}; workspace={replay_workspace}; Tk/UIA thread не блокується."
        )
        self.after(100, self._poll_replay_worker)

    def _poll_replay_worker(self) -> None:
        message = self.replay_worker.poll()
        if message is None:
            self.after(100, self._poll_replay_worker)
            return

        self._set_replay_controls_busy(False)
        active_workspace = Path(self._active_workspace)
        if message.error is not None:
            self._recovery_required_workspaces.add(active_workspace)
            self._hide_uncertain_economic_state(
                "Replay завершився помилкою; economic session state недоступний до recovery."
            )
            text = f"Paper replay помилка: {message.error}"
            self._append_log(text)
            self._set_evaluation_lines([
                "Evaluation недоступна: replay не досяг terminal settlement/evaluation boundary."
            ])
            self.status.set(
                "Replay завершився помилкою; economic state приховано, а workspace механічно "
                "заблоковано до recovery. Натисніть «Відновити workspace» або Control+Shift+R "
                "перед наступним economic run."
            )
            messagebox.showerror("Автоспорт", text)
            return

        result = message.result
        if result is None:
            self._recovery_required_workspaces.add(active_workspace)
            self._hide_uncertain_economic_state(
                "Replay worker не повернув terminal result; economic session state недоступний до recovery."
            )
            self._set_evaluation_lines([
                "Evaluation недоступна: worker не повернув terminal SessionResult."
            ])
            text = (
                "Replay worker завершився без terminal result; economic state приховано, а workspace "
                "механічно заблоковано до успішного recovery."
            )
            self.status.set(text)
            self._append_log(text)
            return

        try:
            self.session = self._open_session(
                self._active_strategy_id,
                self._active_research_plan,
            )
        except Exception as exc:
            self._recovery_required_workspaces.add(active_workspace)
            self._hide_uncertain_economic_state(
                "Replay завершено, але economic session state недоступний; виконайте recovery workspace."
            )
            self._set_evaluation_lines([
                "Evaluation недоступна: post-replay workspace reopen не пройшов fail-closed validation."
            ])
            detail = f"Post-replay workspace reopen відхилено fail-closed: {type(exc).__name__}: {exc}"
            self.status.set(
                "Replay terminal state не можна безпечно підтвердити; economic session state недоступний. "
                "Виконайте «Відновити workspace» або Control+Shift+R перед наступним economic run."
            )
            self._append_log(detail)
            messagebox.showerror("Автоспорт", detail)
            return

        self._startup_economic_error = None
        self.bank.set(self._bank_text())
        self._refresh_tickets()
        summary = result_summary(result)
        self._set_evaluation_lines(evaluation_lines(result))
        self.status.set(summary)
        self._append_log(summary)

    def _refresh_tickets(self) -> None:
        self.tickets.delete(0, "end")
        if self.session is None:
            if self.__dict__.get("_startup_economic_error"):
                self.tickets.insert(
                    "end",
                    "Economic state недоступний через startup validation failure; "
                    "read-only live snapshot лишається доступним, а paper replay потребує recovery.",
                )
            else:
                self.tickets.insert("end", "Replay виконується; ticket state оновиться після завершення transaction.")
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
        if self.live_worker.busy:
            text = (
                "Live snapshot ще виконується. Закриття програми заблоковано до terminal live observation state, "
                "щоб процес не приховав незавершений market/source-health persistence boundary."
            )
            self.live_status.set(text)
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

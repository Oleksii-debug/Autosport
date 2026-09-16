from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import tk_uia

from .dataset import ReplayDataset, load_dataset
from .dataset_worker import OneShotDatasetValidationWorker
from .live_observation import OneShotObservationWorker, observe_workspace_once
from .localization import text
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
    text("ui.speed.event_driven"): 0.0,
    text("ui.speed.realtime"): 1.0,
    text("ui.speed.10x"): 10.0,
    text("ui.speed.100x"): 100.0,
    text("ui.speed.1000x"): 1000.0,
}
_LIVE_MODES = {
    text("ui.live_mode.public_preview"): True,
    text("ui.live_mode.api_key"): False,
}
_STRATEGY_CHOICES = {
    text("ui.strategy.display", strategy_id=spec.strategy_id): spec.strategy_id
    for spec in available_strategies()
}
_DEFAULT_STRATEGY_TEXT = next(
    label for label, strategy_id in _STRATEGY_CHOICES.items()
    if strategy_id == "baseline-v1"
)
_DEFAULT_SPEED_TEXT = next(label for label, speed in _SPEEDS.items() if speed == 0.0)
_DEFAULT_LIVE_MODE_TEXT = next(
    label for label, public_preview in _LIVE_MODES.items() if public_preview is True
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
        raise ValueError(text("ui.error.strategy.unknown_display", display=repr(display))) from exc


def _safe_exception_text(exc: BaseException) -> str:
    """Describe a caught failure without allowing hostile metadata/stringification to escape."""

    try:
        name = type.__getattribute__(type(exc), "__name__")
    except BaseException:
        name = "BaseException"
    try:
        detail = str(exc)
    except BaseException:
        return text("ui.error.exception.message_unavailable", exception_type=name)
    return f"{name}: {detail}" if detail else name


class AutosportApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(text("ui.app.title"))
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
            self._startup_economic_error = _safe_exception_text(exc)
        self.replay_worker = OneShotReplayWorker()
        self.live_worker = OneShotObservationWorker()
        self.dataset_worker = OneShotDatasetValidationWorker()
        self._pending_dataset_path: Path | None = None
        self._active_strategy_id = "baseline-v1"
        self._active_research_plan: ResearchStrategyPlan | None = None
        self._closing = False
        startup_status = (
            text("ui.status.startup.ready")
            if self._startup_economic_error is None
            else text("ui.status.startup.recovery_required")
        )
        self.status = tk.StringVar(value=startup_status)
        self.bank = tk.StringVar(value=self._bank_text())
        self.dataset_text = tk.StringVar(value=text("ui.status.dataset.none"))
        self.strategy_text = tk.StringVar(value=_DEFAULT_STRATEGY_TEXT)
        self.strategy_status = tk.StringVar(value=self._strategy_status_text())
        self.research_plan_text = tk.StringVar(value=text("ui.status.research_plan.baseline"))
        self.speed_text = tk.StringVar(value=_DEFAULT_SPEED_TEXT)
        self.live_mode_text = tk.StringVar(value=_DEFAULT_LIVE_MODE_TEXT)
        self.live_status = tk.StringVar(value=text("ui.status.live.never"))
        self._build()
        self.update_idletasks()
        self._configure_accessibility()
        self.protocol("WM_DELETE_WINDOW", self.close_app)

    def _build(self) -> None:
        frame = ttk.Frame(self, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=text("ui.app.title"), font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(frame, textvariable=self.bank, wraplength=1000).pack(anchor="w", pady=(12, 4))
        ttk.Label(frame, textvariable=self.dataset_text, wraplength=1000).pack(anchor="w", pady=(0, 4))
        ttk.Label(frame, textvariable=self.status, wraplength=1000).pack(anchor="w", pady=(0, 10))

        strategy_controls = ttk.Frame(frame)
        strategy_controls.pack(fill="x", pady=(0, 4))
        self.strategy_label = ttk.Label(strategy_controls, text=text("ui.label.strategy"))
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
            text=text("ui.button.research_plan"),
            command=self.choose_research_plan,
        )
        self.research_plan_button.pack(side="left")
        ttk.Label(frame, textvariable=self.strategy_status, wraplength=1000).pack(anchor="w", pady=(2, 2))
        ttk.Label(frame, textvariable=self.research_plan_text, wraplength=1000).pack(anchor="w", pady=(0, 8))

        controls = ttk.Frame(frame)
        controls.pack(fill="x")
        self.choose_button = ttk.Button(controls, text=text("ui.button.choose_dataset"), command=self.choose_dataset)
        self.choose_button.pack(side="left", padx=(0, 8))
        self.run_button = ttk.Button(controls, text=text("ui.button.run_replay"), command=self.run_dataset)
        self.run_button.pack(side="left", padx=(0, 8))
        self.repair_button = ttk.Button(
            controls,
            text=text("ui.button.repair_workspace"),
            command=self.repair_workspace,
        )
        self.repair_button.pack(side="left", padx=(0, 8))
        self.speed_label = ttk.Label(controls, text=text("ui.label.speed"))
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
        self.live_mode_label = ttk.Label(live_controls, text=text("ui.label.live_mode"))
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
            text=text("ui.button.live_refresh"),
            command=self.refresh_live_snapshot,
        )
        self.live_refresh_button.pack(side="left")
        ttk.Label(frame, textvariable=self.live_status, wraplength=1000).pack(anchor="w", pady=(6, 4))
        self.live_quotes_label = ttk.Label(frame, text=text("ui.label.live_quotes"))
        self.live_quotes_label.pack(anchor="w", pady=(4, 4))
        self.live_quotes = tk.Listbox(frame, height=6, takefocus=True)
        self.live_quotes.pack(fill="x")
        self._set_live_lines([text("ui.status.live_quotes.empty")])

        self.tickets_label = ttk.Label(frame, text=text("ui.label.tickets"))
        self.tickets_label.pack(anchor="w", pady=(14, 4))
        self.tickets = tk.Listbox(frame, height=7, takefocus=True)
        self.tickets.pack(fill="x")
        self.evaluation_label = ttk.Label(frame, text=text("ui.label.evaluation"))
        self.evaluation_label.pack(anchor="w", pady=(14, 4))
        self.evaluation = tk.Listbox(frame, height=5, takefocus=True)
        self.evaluation.pack(fill="x")
        self._set_evaluation_lines([text("ui.status.evaluation.empty")])
        self.log_label = ttk.Label(frame, text=text("ui.label.log"))
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
            (self.strategy, text("ui.accessibility.strategy.name"), text("ui.accessibility.strategy.description"), AUTOMATION_IDS["strategy"]),
            (self.research_plan_button, text("ui.accessibility.research_plan.name"), text("ui.accessibility.research_plan.description"), AUTOMATION_IDS["research_plan"]),
            (self.choose_button, text("ui.accessibility.choose_dataset.name"), text("ui.accessibility.choose_dataset.description"), AUTOMATION_IDS["choose_dataset"]),
            (self.run_button, text("ui.accessibility.run_replay.name"), text("ui.accessibility.run_replay.description"), AUTOMATION_IDS["run_replay"]),
            (self.repair_button, text("ui.accessibility.repair_workspace.name"), text("ui.accessibility.repair_workspace.description"), AUTOMATION_IDS["repair_workspace"]),
            (self.speed, text("ui.accessibility.replay_speed.name"), text("ui.accessibility.replay_speed.description"), AUTOMATION_IDS["replay_speed"]),
            (self.live_mode, text("ui.accessibility.live_mode.name"), text("ui.accessibility.live_mode.description"), AUTOMATION_IDS["live_mode"]),
            (self.live_refresh_button, text("ui.accessibility.live_refresh.name"), text("ui.accessibility.live_refresh.description"), AUTOMATION_IDS["live_refresh"]),
            (self.live_quotes, text("ui.accessibility.live_quotes.name"), text("ui.accessibility.live_quotes.description"), AUTOMATION_IDS["live_quotes"]),
            (self.tickets, text("ui.accessibility.tickets.name"), text("ui.accessibility.tickets.description"), AUTOMATION_IDS["tickets"]),
            (self.evaluation, text("ui.accessibility.evaluation.name"), text("ui.accessibility.evaluation.description"), AUTOMATION_IDS["evaluation"]),
            (self.log, text("ui.accessibility.log.name"), text("ui.accessibility.log.description"), AUTOMATION_IDS["log"]),
        )
        for widget, name, description, automation_id in controls:
            tk_uia.set_acc_name(widget, name)
            tk_uia.set_acc_description(widget, description)
            tk_uia.set_automation_id(widget, automation_id)

    def _strategy_status_text(self) -> str:
        strategy_id = strategy_id_from_display(self.strategy_text.get())
        spec = strategy_spec(strategy_id)
        plan_requirement = text(
            "ui.strategy.plan.required" if spec.requires_research_plan else "ui.strategy.plan.not_required"
        )
        ticket_mode = text(
            "ui.strategy.ticket.opens" if spec.opens_paper_tickets else "ui.strategy.ticket.observe_only"
        )
        return text(
            "ui.strategy.status",
            strategy_id=spec.strategy_id,
            label=spec.label,
            plan_requirement=plan_requirement,
            ticket_mode=ticket_mode,
        )

    def _on_strategy_changed(self, _event=None) -> None:
        if self._dataset_busy:
            self.status.set(text("ui.status.strategy.dataset_busy"))
            return
        if self.replay_worker.busy:
            return
        strategy_id = strategy_id_from_display(self.strategy_text.get())
        spec = strategy_spec(strategy_id)
        if not spec.requires_research_plan:
            self.research_plan = None
            self.research_plan_path = None
            self.research_plan_text.set(
                text("ui.status.research_plan.not_required", strategy_id=strategy_id)
            )
        elif self.research_plan is None:
            self.research_plan_text.set(text("ui.status.research_plan.required_missing"))
        self.strategy_status.set(self._strategy_status_text())
        self.status.set(text("ui.status.strategy.selected", strategy_id=strategy_id))

    def choose_research_plan(self) -> None:
        if self._dataset_busy:
            self.status.set(text("ui.status.research_plan.dataset_busy"))
            return
        if self.replay_worker.busy:
            self.status.set(text("ui.status.research_plan.replay_busy"))
            return
        strategy_id = strategy_id_from_display(self.strategy_text.get())
        spec = strategy_spec(strategy_id)
        if not spec.requires_research_plan:
            messagebox.showinfo(
                text("ui.dialog.title"),
                text("ui.info.research_plan.not_supported", strategy_id=strategy_id),
            )
            self.status.set(
                text("ui.status.research_plan.not_required_short", strategy_id=strategy_id)
            )
            return
        selected = filedialog.askopenfilename(
            title=text("ui.dialog.research_plan.choose_title"),
            filetypes=((text("ui.filetype.json"), "*.json"), (text("ui.filetype.all"), "*.*")),
        )
        if not selected:
            return
        try:
            plan = ResearchStrategyPlan.from_path(selected)
            validate_strategy_configuration(strategy_id, plan)
        except Exception as exc:
            messagebox.showerror(
                text("ui.dialog.title"),
                text("ui.error.research_plan.rejected", detail=_safe_exception_text(exc)),
            )
            self.status.set(text("ui.status.research_plan.validation_failed"))
            return
        self.research_plan_path = Path(selected)
        self.research_plan = plan
        self.research_plan_text.set(
            text(
                "ui.status.research_plan.selected",
                name=self.research_plan_path.name,
                sha256=plan.source_sha256,
            )
        )
        self.status.set(
            text(
                "ui.status.research_plan.bound",
                strategy_id=strategy_id,
                sha_short=plan.source_sha256[:12],
            )
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
                return text(
                    "ui.status.bank.recovery_required",
                    workspace=self._active_workspace,
                )
            return text("ui.status.bank.pending", workspace=self._active_workspace)
        return text(
            "ui.status.bank.current",
            balance=self.session.book.balance,
            committed_stake=self.session.book.committed_stake,
            strategy_id=self.session.strategy_id,
            workspace=self.session.workspace,
        )

    def _block_workspace_for_recovery(self, workspace: Path) -> None:
        self._recovery_required_workspaces.add(Path(workspace))

    @property
    def _dataset_busy(self) -> bool:
        worker = self.__dict__.get("dataset_worker")
        return bool(worker is not None and worker.busy)

    def _dataset_selection_blocker(self) -> str | None:
        if self._dataset_busy:
            return text("ui.status.dataset.validation_busy")
        if self.replay_worker.busy:
            return text("ui.status.dataset.replay_busy")
        if self.live_worker.busy:
            return text("ui.status.dataset.live_busy")
        recovery_worker = self.__dict__.get("recovery_worker")
        if recovery_worker is not None and recovery_worker.busy:
            return text("ui.status.dataset.recovery_busy")
        return None

    def _hide_uncertain_economic_state(self, ticket_message: str) -> bool:
        session = self.session
        self.session = None
        self.bank.set(
            text("ui.status.bank.quarantined", workspace=self._active_workspace)
        )
        self.tickets.delete(0, "end")
        self.tickets.insert("end", ticket_message)
        if session is None:
            return True
        try:
            session.close()
        except BaseException as exc:
            session_workspace = Path(session.workspace)
            self._block_workspace_for_recovery(session_workspace)
            if not isinstance(exc, Exception):
                raise
            self._append_log(
                text(
                    "ui.log.session.teardown_secondary",
                    workspace=session_workspace,
                    detail=_safe_exception_text(exc),
                )
            )
            return False
        return True

    def choose_dataset(self) -> None:
        if self._closing:
            return
        blocker = self._dataset_selection_blocker()
        if blocker is not None:
            self.status.set(blocker)
            return
        selected = filedialog.askdirectory(title=text("ui.dialog.dataset.choose_title"))
        if not selected:
            return

        # ``askdirectory`` runs a nested Tk event loop. Re-check every worker after
        # the dialog closes so an operation started during that interval cannot be
        # raced by validation/control-state publication.
        blocker = self._dataset_selection_blocker()
        if blocker is not None:
            self.status.set(blocker)
            return

        selected_path = Path(selected).absolute()
        self._pending_dataset_path = selected_path

        def task() -> ReplayDataset:
            return load_dataset(selected_path)

        try:
            started = self.dataset_worker.start(task)
        except BaseException:
            self._pending_dataset_path = None
            raise
        if not started:
            self._pending_dataset_path = None
            message_text = text("ui.error.dataset.worker_not_started")
            self.status.set(message_text)
            self._append_log(message_text)
            messagebox.showerror(text("ui.dialog.title"), message_text)
            return

        self._set_replay_controls_busy(True)
        self.status.set(
            text("ui.status.dataset.validation_running", path=selected_path)
        )
        self._append_log(text("ui.log.dataset.validation_started", path=selected_path))
        self.after(100, self._poll_dataset_worker)

    def _poll_dataset_worker(self) -> None:
        if self._closing:
            return
        message = self.dataset_worker.poll()
        if message is None:
            self.after(100, self._poll_dataset_worker)
            return

        pending_path = self._pending_dataset_path
        self._pending_dataset_path = None
        self._set_replay_controls_busy(False)
        if message.error is not None:
            message_text = text("ui.error.dataset.rejected", detail=message.error)
            self.status.set(text("ui.status.dataset.validation_failed"))
            self._append_log(message_text)
            messagebox.showerror(text("ui.dialog.title"), message_text)
            return

        dataset = message.result
        if (
            not isinstance(dataset, ReplayDataset)
            or pending_path is None
            or Path(dataset.root) != pending_path
        ):
            message_text = text("ui.error.dataset.identity_mismatch")
            self.status.set(text("ui.status.dataset.identity_mismatch"))
            self._append_log(message_text)
            messagebox.showerror(text("ui.dialog.title"), message_text)
            return

        self.dataset_path = pending_path
        self.dataset_text.set(
            text(
                "ui.status.dataset.summary",
                name=dataset.name,
                sport=dataset.sport,
                market_sha=dataset.market_sha256[:12],
                results_sha=dataset.results_sha256[:12],
            )
        )
        self.status.set(text("ui.status.dataset.ready"))
        self._append_log(
            text(
                "ui.log.dataset.validation_success",
                name=dataset.name,
                sport=dataset.sport,
                root=pending_path,
            )
        )

    def refresh_live_snapshot(self) -> None:
        if self._closing:
            return
        if self._dataset_busy:
            self.live_status.set(text("ui.status.live.dataset_busy"))
            self.status.set(text("ui.status.live.dataset_blocks"))
            return
        if self.replay_worker.busy:
            self.live_status.set(text("ui.status.live.replay_busy"))
            return
        mode = self.live_mode_text.get()
        public_preview = _LIVE_MODES.get(mode)
        if public_preview is None:
            self.live_status.set(text("ui.status.live.unknown_mode"))
            return
        workspace = self.workspace

        def task():
            api_key = None if public_preview else os.environ.get("AUTOSPORT_PARLAYAPI_KEY")
            if not public_preview and not api_key:
                raise RuntimeError(text("ui.error.live.api_key_missing"))
            provider = ParlayApiTableTennisProvider(api_key, public_preview=public_preview)
            return observe_workspace_once(workspace, provider, max_items=250)

        if not self.live_worker.start(task):
            self.live_status.set(text("ui.status.live.busy"))
            return
        self.live_refresh_button.state(["disabled"])
        self.live_status.set(text("ui.status.live.running"))
        self.status.set(text("ui.status.live.read_only_running"))
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
            message_text = text("ui.error.live.snapshot", detail=message.error)
            self.live_status.set(message_text)
            self.status.set(text("ui.status.live.failed"))
            self._append_log(message_text)
            return
        result = message.result
        if result is None:
            self.live_status.set(text("ui.status.live.no_result"))
            return
        summary = observation_summary(result)
        self.live_status.set(summary)
        self.status.set(text("ui.status.live.updated"))
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
        if busy or self._dataset_busy:
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
        if self._dataset_busy:
            self.status.set(text("ui.status.recovery.dataset_busy"))
            return
        if self.replay_worker.busy:
            self.status.set(text("ui.status.recovery.replay_busy"))
            return
        if self.live_worker.busy:
            self.status.set(text("ui.status.recovery.live_busy"))
            return
        try:
            strategy_id, research_plan = self._selected_replay_configuration()
            replay_workspace = workspace_for_strategy(self.workspace, strategy_id, research_plan)
        except Exception as exc:
            messagebox.showerror(
                text("ui.dialog.title"),
                text("ui.error.recovery.configuration", detail=_safe_exception_text(exc)),
            )
            self.status.set(text("ui.status.recovery.configuration_rejected"))
            return

        replay_workspace = Path(replay_workspace)
        self._active_workspace = replay_workspace
        self._active_strategy_id = strategy_id
        self._active_research_plan = research_plan
        self._recovery_required_workspaces.add(replay_workspace)
        teardown_succeeded = self._hide_uncertain_economic_state(
            text("ui.status.recovery.in_progress_ticket")
        )
        if not teardown_succeeded:
            detail = text("ui.error.recovery.teardown_no_reopen")
            self.status.set(text("ui.status.recovery.teardown_blocked"))
            self._append_log(detail)
            messagebox.showerror(text("ui.dialog.title"), detail)
            return

        try:
            report = reconcile_late_crashes(replay_workspace)
        except Exception as exc:
            self._recovery_required_workspaces.add(replay_workspace)
            self._hide_uncertain_economic_state(text("ui.status.recovery.state_unavailable"))
            detail = text("ui.error.recovery.failure", detail=_safe_exception_text(exc))
            self.status.set(text("ui.status.recovery.failed_until_fixed"))
            self._append_log(detail)
            messagebox.showerror(text("ui.dialog.title"), detail)
            return

        summary = text(
            "ui.recovery.summary",
            reconciled=len(report.reconciled_keys),
            aborted_uncommitted=len(report.aborted_uncommitted_keys),
            unresolved=len(report.unresolved_without_summary),
            workspace=replay_workspace,
        )
        self._append_log(summary)
        if report.unresolved_without_summary:
            self._recovery_required_workspaces.add(replay_workspace)
            self._hide_uncertain_economic_state(text("ui.status.recovery.unresolved_ticket"))
            self.status.set(summary + text("ui.status.recovery.unresolved_suffix"))
            messagebox.showwarning(
                text("ui.dialog.title"),
                text("ui.warning.recovery.unresolved"),
            )
            return

        try:
            self.session = self._open_session(strategy_id, research_plan)
        except Exception as exc:
            self._recovery_required_workspaces.add(replay_workspace)
            self._hide_uncertain_economic_state(text("ui.status.recovery.reopen_validation"))
            detail = text(
                "ui.error.recovery.reopen",
                detail=_safe_exception_text(exc),
            )
            self.status.set(text("ui.status.recovery.reopen_blocked"))
            self._append_log(detail)
            messagebox.showerror(text("ui.dialog.title"), detail)
            return

        self._startup_economic_error = None
        self.bank.set(self._bank_text())
        self._refresh_tickets()
        self._recovery_required_workspaces.discard(replay_workspace)
        self.status.set(summary + text("ui.status.recovery.ready_suffix"))
        messagebox.showinfo(text("ui.dialog.title"), text("ui.info.recovery.complete"))

    def run_dataset(self) -> None:
        if self._dataset_busy:
            self.status.set(text("ui.status.replay.dataset_busy"))
            return
        if not self.dataset_path:
            messagebox.showinfo(text("ui.dialog.title"), text("ui.info.replay.dataset_required"))
            return
        if self.replay_worker.busy:
            self.status.set(text("ui.status.replay.already_busy"))
            return
        if self.live_worker.busy:
            self.status.set(text("ui.status.replay.live_busy"))
            return
        try:
            strategy_id, research_plan = self._selected_replay_configuration()
            replay_workspace = Path(workspace_for_strategy(self.workspace, strategy_id, research_plan))
        except Exception as exc:
            messagebox.showerror(
                text("ui.dialog.title"),
                text("ui.error.replay.configuration", detail=_safe_exception_text(exc)),
            )
            self.status.set(text("ui.status.replay.configuration_rejected"))
            return

        if replay_workspace in self._recovery_required_workspaces:
            self._active_workspace = replay_workspace
            message_text = text("ui.status.replay.quarantined")
            self.status.set(message_text)
            self._append_log(message_text)
            messagebox.showwarning(text("ui.dialog.title"), message_text)
            return

        dataset_path = self.dataset_path
        speed = _SPEEDS[self.speed_text.get()]
        teardown_succeeded = self._hide_uncertain_economic_state(
            text("ui.status.replay.preparing_ticket")
        )
        if not teardown_succeeded:
            detail = text("ui.error.replay.teardown")
            self.status.set(text("ui.status.replay.teardown_blocked"))
            self._append_log(detail)
            messagebox.showerror(text("ui.dialog.title"), detail)
            return

        self._active_workspace = replay_workspace

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
            self.status.set(text("ui.status.replay.start_failed"))
            return

        self._active_strategy_id = strategy_id
        self._active_research_plan = research_plan
        self._set_replay_controls_busy(True)
        self.bank.set(self._bank_text())
        self._refresh_tickets()
        self._set_evaluation_lines([text("ui.evaluation.running")])
        plan_identity = (
            f"; plan={research_plan.source_sha256[:12]}…"
            if research_plan is not None
            else ""
        )
        self.status.set(
            text(
                "ui.status.replay.running",
                strategy_id=strategy_id,
                plan_identity=plan_identity,
            )
        )
        self._append_log(
            text(
                "ui.log.replay.started",
                strategy_id=strategy_id,
                plan_identity=plan_identity,
                workspace=replay_workspace,
            )
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
            self._hide_uncertain_economic_state(text("ui.status.replay.error_ticket"))
            message_text = text("ui.error.replay.worker", detail=message.error)
            self._append_log(message_text)
            self._set_evaluation_lines([text("ui.evaluation.replay_failed")])
            self.status.set(text("ui.status.replay.failed_recovery"))
            messagebox.showerror(text("ui.dialog.title"), message_text)
            return

        result = message.result
        if result is None:
            self._recovery_required_workspaces.add(active_workspace)
            self._hide_uncertain_economic_state(text("ui.status.replay.no_result_ticket"))
            self._set_evaluation_lines([text("ui.evaluation.no_terminal_result")])
            message_text = text("ui.status.replay.no_terminal_result")
            self.status.set(message_text)
            self._append_log(message_text)
            return

        try:
            self.session = self._open_session(
                self._active_strategy_id,
                self._active_research_plan,
            )
        except Exception as exc:
            self._recovery_required_workspaces.add(active_workspace)
            self._hide_uncertain_economic_state(text("ui.status.replay.reopen_ticket"))
            self._set_evaluation_lines([text("ui.evaluation.reopen_failed")])
            detail = text(
                "ui.error.replay.reopen",
                detail=_safe_exception_text(exc),
            )
            self.status.set(text("ui.status.replay.reopen_blocked"))
            self._append_log(detail)
            messagebox.showerror(text("ui.dialog.title"), detail)
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
                self.tickets.insert("end", text("ui.status.tickets.startup_failure"))
            else:
                self.tickets.insert("end", text("ui.status.tickets.replay_running"))
            return
        for line in ticket_lines(self.session):
            self.tickets.insert("end", line)

    def close_app(self) -> None:
        if self.replay_worker.busy:
            close_message = text("ui.status.close.replay_busy")
            self.status.set(close_message)
            self._append_log(close_message)
            self.bell()
            return
        if self.live_worker.busy:
            close_message = text("ui.status.close.live_busy")
            self.live_status.set(close_message)
            self.status.set(close_message)
            self._append_log(close_message)
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
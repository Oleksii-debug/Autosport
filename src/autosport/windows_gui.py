from __future__ import annotations

from pathlib import Path
from tkinter import messagebox, ttk

import tk_uia

from .gui import AutosportApp
from .localization import text
from .recovery_worker import OneShotRecoveryWorker, RecoverySessionView, recover_workspace_once
from .replay_worker import workspace_for_strategy
from .ui_model import evaluation_lines, result_summary, ticket_lines


WINDOWS_BANKROLL_AUTOMATION_ID = 205


def _safe_exception_detail(exc: BaseException) -> str:
    """Render fail-closed GUI diagnostics without trusting exception metadata."""

    try:
        exception_type = type.__getattribute__(type(exc), "__name__")
    except BaseException:
        exception_type = "BaseException"
    try:
        detail = str(exc)
    except BaseException:
        return f"{exception_type}: exception details unavailable"
    return f"{exception_type}: {detail}"


class WindowsAutosportApp(AutosportApp):
    """Windows product GUI with recovery orchestration kept off the Tk/UIA thread."""

    def __init__(self) -> None:
        self.recovery_worker: OneShotRecoveryWorker | None = None
        self._recovery_view: RecoverySessionView | None = None
        self._recovery_blocked_workspace: Path | None = None
        # Keep fail-closed recovery state isolated per economic workspace. A later
        # successful recovery for strategy/workspace B must never clear an earlier
        # failed or unresolved recovery for workspace A.
        self._recovery_blocked_workspaces: set[Path] = set()
        super().__init__()
        self.recovery_worker = OneShotRecoveryWorker()

    def _build(self) -> None:
        super()._build()
        frame = next(iter(self.winfo_children()), None)
        if frame is None:
            raise RuntimeError("Windows GUI root frame is missing")

        bank_label = None
        bank_variable = str(self.bank)
        for child in frame.winfo_children():
            try:
                if "textvariable" in child.keys() and str(child.cget("textvariable")) == bank_variable:
                    bank_label = child
                    break
            except Exception:
                continue
        if bank_label is None:
            raise RuntimeError("Virtual bankroll summary label is missing")

        siblings = frame.winfo_children()
        bank_index = siblings.index(bank_label)
        before = siblings[bank_index + 1] if bank_index + 1 < len(siblings) else None
        bank_label.destroy()
        self.bank_summary = ttk.Entry(
            frame,
            textvariable=self.bank,
            state="readonly",
            takefocus=True,
        )
        if before is None:
            self.bank_summary.pack(fill="x", pady=(12, 4))
        else:
            self.bank_summary.pack(fill="x", pady=(12, 4), before=before)

    def _configure_accessibility(self) -> None:
        super()._configure_accessibility()
        tk_uia.set_acc_name(self.bank_summary, text("ui.accessibility.bankroll.name"))
        tk_uia.set_acc_description(
            self.bank_summary,
            text("ui.accessibility.bankroll.description"),
        )
        tk_uia.set_automation_id(self.bank_summary, WINDOWS_BANKROLL_AUTOMATION_ID)

    @property
    def _recovery_busy(self) -> bool:
        worker = self.recovery_worker
        return bool(worker is not None and worker.busy)

    def _blocked_recovery_workspaces(self) -> set[Path]:
        """Return every workspace that still requires successful recovery.

        Some headless GUI contract tests instantiate the Tk subclass with
        ``object.__new__``. Reading a missing attribute through ``getattr`` would
        fall into ``tkinter.Misc.__getattr__`` and recurse without a Tk
        interpreter, so internal process state is read directly from ``__dict__``.
        The legacy single slot is lazily migrated into the canonical set so older
        callers/tests cannot accidentally bypass a fail-closed recovery block.
        """

        blocked_workspaces = self.__dict__.get("_recovery_blocked_workspaces")
        if blocked_workspaces is None:
            blocked_workspaces = set()
            self.__dict__["_recovery_blocked_workspaces"] = blocked_workspaces
        legacy = self.__dict__.get("_recovery_blocked_workspace")
        if legacy is not None:
            blocked_workspaces.add(Path(legacy))
        return blocked_workspaces

    def _block_workspace_for_recovery(self, workspace: Path) -> None:
        workspace = Path(workspace)
        self._blocked_recovery_workspaces().add(workspace)
        self._recovery_blocked_workspace = workspace

    def _unblock_workspace_after_recovery(self, workspace: Path) -> None:
        workspace = Path(workspace)
        self._blocked_recovery_workspaces().discard(workspace)
        if self.__dict__.get("_recovery_blocked_workspace") == workspace:
            self._recovery_blocked_workspace = None

    def _workspace_requires_recovery(self, workspace: Path) -> bool:
        return Path(workspace) in self._blocked_recovery_workspaces()

    def _bank_text(self) -> str:
        source = (
            self._recovery_view
            if self._recovery_view is not None
            else getattr(self, "session", None)
        )
        if source is None:
            return text("ui.status.bank.pending", workspace=self._active_workspace)
        return text(
            "ui.status.bank.current",
            balance=source.book.balance,
            committed_stake=source.book.committed_stake,
            strategy_id=source.strategy_id,
            workspace=source.workspace,
        )

    def _refresh_tickets(self) -> None:
        self.tickets.delete(0, "end")
        source = (
            self._recovery_view
            if self._recovery_view is not None
            else getattr(self, "session", None)
        )
        if source is None:
            self.tickets.insert("end", text("ui.status.windows.economic_unavailable"))
            return
        for line in ticket_lines(source):
            self.tickets.insert("end", line)

    def _on_strategy_changed(self, _event=None) -> None:
        if self._recovery_busy:
            self.status.set(text("ui.status.windows.strategy_recovery_busy"))
            return
        self._recovery_view = None
        super()._on_strategy_changed(_event)

    def choose_research_plan(self) -> None:
        if self._recovery_busy:
            self.status.set(text("ui.status.windows.research_plan_recovery_busy"))
            return
        super().choose_research_plan()

    def choose_dataset(self) -> None:
        if self._recovery_busy:
            self.status.set(text("ui.status.windows.dataset_recovery_busy"))
            return
        super().choose_dataset()

    def refresh_live_snapshot(self) -> None:
        if self._recovery_busy:
            self.live_status.set(text("ui.status.windows.live_recovery_busy"))
            self.status.set(text("ui.status.windows.live_recovery_blocked"))
            return
        super().refresh_live_snapshot()

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
        if self._recovery_busy:
            self.status.set(text("ui.status.recovery.already_busy"))
            return
        try:
            strategy_id, research_plan = self._selected_replay_configuration()
            replay_workspace = workspace_for_strategy(self.workspace, strategy_id, research_plan)
        except Exception as exc:
            messagebox.showerror(
                text("ui.dialog.title"),
                text("ui.error.recovery.configuration", detail=exc),
            )
            self.status.set(text("ui.status.recovery.configuration_rejected"))
            return

        prior_workspace = self.__dict__.get("_active_workspace")
        self._active_workspace = replay_workspace
        self._active_strategy_id = strategy_id
        self._active_research_plan = research_plan
        self._block_workspace_for_recovery(replay_workspace)
        self._recovery_view = None
        session = self.session
        self.session = None
        if session is not None:
            # Bind teardown uncertainty to the exact workspace owned by the detached
            # economic session. Headless/legacy fixtures without a workspace field
            # fall back to the previously active workspace rather than inventing one.
            prior_session_workspace = prior_workspace
            try:
                session_state = object.__getattribute__(session, "__dict__")
            except BaseException:
                session_state = None
            if isinstance(session_state, dict) and session_state.get("workspace") is not None:
                try:
                    prior_session_workspace = Path(session_state["workspace"])
                except (TypeError, ValueError):
                    prior_session_workspace = prior_workspace
            try:
                session.close()
            except BaseException as exc:
                if prior_session_workspace is not None:
                    self._block_workspace_for_recovery(prior_session_workspace)
                # Establish fail-closed UI/economic projection state before any
                # process-control BaseException is allowed to continue unwinding.
                self.bank.set(self._bank_text())
                self._refresh_tickets()
                if not isinstance(exc, Exception):
                    raise
                detail = text(
                    "ui.error.recovery.teardown",
                    detail=_safe_exception_detail(exc),
                )
                self.status.set(text("ui.status.recovery.teardown_blocked"))
                self._append_log(detail)
                messagebox.showerror(text("ui.dialog.title"), detail)
                return

        def task():
            return recover_workspace_once(
                replay_workspace,
                initial_bankroll="10000",
                strategy_id=strategy_id,
                research_plan=research_plan,
            )

        worker = self.recovery_worker
        if worker is None or not worker.start(task):
            self.status.set(text("ui.status.recovery.start_failed"))
            self.bank.set(self._bank_text())
            self._refresh_tickets()
            return

        self._set_replay_controls_busy(True)
        self.bank.set(self._bank_text())
        self._refresh_tickets()
        plan_identity = (
            f"; plan={research_plan.source_sha256[:12]}…"
            if research_plan is not None
            else ""
        )
        self.status.set(
            text(
                "ui.status.recovery.running",
                strategy_id=strategy_id,
                plan_identity=plan_identity,
            )
        )
        self._append_log(
            text(
                "ui.log.recovery.started",
                strategy_id=strategy_id,
                plan_identity=plan_identity,
                workspace=replay_workspace,
            )
        )
        self.after(100, self._poll_recovery_worker)

    def _poll_recovery_worker(self) -> None:
        worker = self.recovery_worker
        if worker is None:
            return
        message = worker.poll()
        if message is None:
            self.after(100, self._poll_recovery_worker)
            return

        self._set_replay_controls_busy(False)
        if message.error is not None:
            self._recovery_view = None
            self.bank.set(self._bank_text())
            self._refresh_tickets()
            detail = text("ui.error.recovery.worker", detail=message.error)
            self.status.set(text("ui.status.recovery.blocked"))
            self._append_log(detail)
            messagebox.showerror(text("ui.dialog.title"), detail)
            return

        result = message.result
        if result is None:
            self._recovery_view = None
            self.bank.set(self._bank_text())
            self._refresh_tickets()
            self.status.set(text("ui.status.recovery.no_result"))
            return

        expected_workspace = Path(self._active_workspace)
        expected_strategy_id = (
            self._active_research_plan.experiment_strategy_id
            if self._active_research_plan is not None
            else self._active_strategy_id
        )
        try:
            result_workspace = Path(result.session_view.workspace)
        except (TypeError, ValueError):
            result_workspace = None
        if (
            result_workspace != expected_workspace
            or result.session_view.strategy_id != expected_strategy_id
        ):
            self._recovery_view = None
            self._block_workspace_for_recovery(expected_workspace)
            self.bank.set(self._bank_text())
            self._refresh_tickets()
            detail = text(
                "ui.error.recovery.identity_mismatch",
                expected_workspace=expected_workspace,
                expected_strategy_id=expected_strategy_id,
                received_workspace=repr(result.session_view.workspace),
                received_strategy_id=repr(result.session_view.strategy_id),
            )
            self.status.set(text("ui.status.recovery.identity_mismatch"))
            self._append_log(detail)
            messagebox.showerror(text("ui.dialog.title"), detail)
            return

        report = result.report
        summary = text(
            "ui.recovery.summary",
            reconciled=len(report.reconciled_keys),
            aborted_uncommitted=len(report.aborted_uncommitted_keys),
            unresolved=len(report.unresolved_without_summary),
            workspace=result.session_view.workspace,
        )
        self._append_log(summary)
        if report.unresolved_without_summary:
            self._recovery_view = None
            self.session = None
            self._block_workspace_for_recovery(expected_workspace)
            self.bank.set(self._bank_text())
            self._refresh_tickets()
            self.status.set(summary + text("ui.status.recovery.unresolved_suffix"))
            messagebox.showwarning(
                text("ui.dialog.title"),
                text("ui.warning.recovery.unresolved"),
            )
            return

        self._recovery_view = result.session_view
        self.bank.set(self._bank_text())
        self._refresh_tickets()
        self._unblock_workspace_after_recovery(result.session_view.workspace)
        self.status.set(summary + text("ui.status.recovery.ready_suffix"))
        messagebox.showinfo(text("ui.dialog.title"), text("ui.info.recovery.complete"))

    def _poll_replay_worker(self) -> None:
        """Consume replay terminal state and quarantine uncertain economic state."""

        message = self.replay_worker.poll()
        if message is None:
            self.after(100, self._poll_replay_worker)
            return

        self._set_replay_controls_busy(False)
        if message.error is not None or message.result is None:
            self._block_workspace_for_recovery(self._active_workspace)
            self._recovery_view = None
            self.session = None
            self.bank.set(self._bank_text())
            self._refresh_tickets()
            if message.error is not None:
                replay_error = text("ui.error.recovery.worker", detail=message.error)
                self._append_log(replay_error)
                self._set_evaluation_lines([text("ui.evaluation.replay_failed")])
                self.status.set(text("ui.status.replay.failed_recovery"))
                messagebox.showerror(text("ui.dialog.title"), replay_error)
                return

            self._set_evaluation_lines([text("ui.evaluation.no_terminal_result")])
            self.status.set(text("ui.status.replay.no_terminal_result"))
            return

        try:
            self.session = self._open_session(
                self._active_strategy_id,
                self._active_research_plan,
            )
        except BaseException as exc:
            self.session = None
            self._recovery_view = None
            self._block_workspace_for_recovery(self._active_workspace)
            self.bank.set(self._bank_text())
            self._refresh_tickets()
            self._set_evaluation_lines([text("ui.evaluation.reopen_failed")])
            if not isinstance(exc, Exception):
                raise
            detail = text(
                "ui.error.replay.reopen",
                detail=_safe_exception_detail(exc),
            )
            self.status.set(text("ui.status.replay.reopen_blocked"))
            self._append_log(detail)
            messagebox.showerror(text("ui.dialog.title"), detail)
            return

        self.bank.set(self._bank_text())
        self._refresh_tickets()

        result = message.result
        summary = result_summary(result)
        self._set_evaluation_lines(evaluation_lines(result))
        self.status.set(summary)
        self._append_log(summary)

    def run_dataset(self) -> None:
        if self._recovery_busy:
            self.status.set(text("ui.status.replay.recovery_busy"))
            return
        if self._blocked_recovery_workspaces():
            try:
                strategy_id, research_plan = self._selected_replay_configuration()
                replay_workspace = workspace_for_strategy(self.workspace, strategy_id, research_plan)
            except Exception:
                # Preserve the base GUI's canonical configuration validation path
                # instead of masking it as an unrelated recovery quarantine.
                return super().run_dataset()
            if self._workspace_requires_recovery(replay_workspace):
                self.status.set(text("ui.status.replay.recovery_required"))
                return
        self._recovery_view = None
        super().run_dataset()

    def close_app(self) -> None:
        if self._recovery_busy:
            close_text = text("ui.status.close.recovery_busy")
            self.status.set(close_text)
            self._append_log(close_text)
            self.bell()
            return
        super().close_app()


def main() -> int:
    WindowsAutosportApp().mainloop()
    return 0
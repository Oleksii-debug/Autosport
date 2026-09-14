from __future__ import annotations

from pathlib import Path
from tkinter import messagebox, ttk

import tk_uia

from .gui import AutosportApp
from .recovery_worker import OneShotRecoveryWorker, RecoverySessionView, recover_workspace_once
from .replay_worker import workspace_for_strategy
from .ui_model import evaluation_lines, result_summary, ticket_lines


WINDOWS_BANKROLL_AUTOMATION_ID = 205


class WindowsAutosportApp(AutosportApp):
    """Windows product GUI with recovery orchestration kept off the Tk/UIA thread."""

    def __init__(self) -> None:
        self.recovery_worker: OneShotRecoveryWorker | None = None
        self._recovery_view: RecoverySessionView | None = None
        self._recovery_blocked_workspace: Path | None = None
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
        tk_uia.set_acc_name(self.bank_summary, "Віртуальний банк")
        tk_uia.set_acc_description(
            self.bank_summary,
            "Read-only summary поточного virtual bankroll, committed paper stake, canonical strategy та workspace. Доступний через Tab traversal.",
        )
        tk_uia.set_automation_id(self.bank_summary, WINDOWS_BANKROLL_AUTOMATION_ID)

    @property
    def _recovery_busy(self) -> bool:
        worker = self.recovery_worker
        return bool(worker is not None and worker.busy)

    def _blocked_recovery_workspaces(self) -> set[Path]:
        """Return the in-process quarantine set, migrating the legacy single slot lazily.

        The Windows GUI may host multiple isolated strategy/plan workspaces in one
        process. A failed recovery or uncertain replay in one workspace must not
        poison every strategy, but failure in a second workspace must not erase the
        first quarantine either. The legacy single-path attribute is retained as a
        compatibility/display hint for existing callers and tests; enforcement uses
        the set.
        """

        # Avoid Tkinter.Misc.__getattr__ here: headless/partially constructed app
        # instances legitimately exercise this fail-closed state helper in tests.
        blocked = self.__dict__.get("_recovery_blocked_workspaces")
        if blocked is None:
            blocked = set()
            self._recovery_blocked_workspaces = blocked
        legacy = self.__dict__.get("_recovery_blocked_workspace")
        if legacy is not None:
            blocked.add(Path(legacy))
        return blocked

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
            return f"Віртуальний банк: оновлюється після replay; workspace: {self._active_workspace}"
        return (
            f"Віртуальний банк: {source.book.balance}; "
            f"committed: {source.book.committed_stake}; "
            f"strategy: {source.strategy_id}; "
            f"workspace: {source.workspace}"
        )

    def _refresh_tickets(self) -> None:
        self.tickets.delete(0, "end")
        source = (
            self._recovery_view
            if self._recovery_view is not None
            else getattr(self, "session", None)
        )
        if source is None:
            self.tickets.insert(
                "end",
                "Economic state тимчасово недоступний; дочекайтеся terminal replay/recovery boundary.",
            )
            return
        for line in ticket_lines(source):
            self.tickets.insert("end", line)

    def _on_strategy_changed(self, _event=None) -> None:
        if self._recovery_busy:
            self.status.set("Strategy configuration заблоковано: workspace recovery ще виконується.")
            return
        self._recovery_view = None
        super()._on_strategy_changed(_event)

    def choose_research_plan(self) -> None:
        if self._recovery_busy:
            self.status.set("Research plan не можна змінювати під час workspace recovery.")
            return
        super().choose_research_plan()

    def choose_dataset(self) -> None:
        if self._recovery_busy:
            self.status.set("Dataset не можна змінювати під час workspace recovery.")
            return
        super().choose_dataset()

    def refresh_live_snapshot(self) -> None:
        if self._recovery_busy:
            self.live_status.set("Live snapshot відкладено: workspace recovery ще виконується.")
            self.status.set("Read-only live observation не запускається одночасно з workspace recovery.")
            return
        super().refresh_live_snapshot()

    def repair_workspace(self) -> None:
        if self._closing:
            return
        if self.replay_worker.busy:
            self.status.set("Recovery заблоковано: economic replay ще виконується.")
            return
        if self.live_worker.busy:
            self.status.set("Recovery заблоковано: live snapshot ще виконується.")
            return
        if self._recovery_busy:
            self.status.set("Workspace recovery уже виконується; другий recovery не запущено.")
            return
        try:
            strategy_id, research_plan = self._selected_replay_configuration()
            replay_workspace = workspace_for_strategy(self.workspace, strategy_id, research_plan)
        except Exception as exc:
            messagebox.showerror("Автоспорт", f"Recovery configuration відхилено: {exc}")
            self.status.set("Recovery не запущено: canonical strategy configuration не пройшла fail-closed validation.")
            return

        self._active_workspace = replay_workspace
        self._active_strategy_id = strategy_id
        self._active_research_plan = research_plan
        self._block_workspace_for_recovery(replay_workspace)
        self._recovery_view = None
        if self.session is not None:
            self.session.close()
            self.session = None

        def task():
            return recover_workspace_once(
                replay_workspace,
                initial_bankroll="10000",
                strategy_id=strategy_id,
                research_plan=research_plan,
            )

        worker = self.recovery_worker
        if worker is None or not worker.start(task):
            self.status.set(
                "Workspace recovery не запущено; session state лишається fail-closed до повторного успішного recovery."
            )
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
            f"Workspace recovery виконується у background worker; strategy={strategy_id}{plan_identity}. "
            "Tk/UIA/NVDA thread залишається responsive; replay, live, recovery і configuration controls заблоковано до terminal state."
        )
        self._append_log(
            f"Workspace recovery запущено у background worker; strategy={strategy_id}{plan_identity}; workspace={replay_workspace}."
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
            detail = f"Workspace recovery відхилено fail-closed: {message.error}"
            self.status.set(
                "Workspace recovery не завершено; цей economic workspace заблоковано для нового replay до успішного recovery."
            )
            self._append_log(detail)
            messagebox.showerror("Автоспорт", detail)
            return

        result = message.result
        if result is None:
            self._recovery_view = None
            self.bank.set(self._bank_text())
            self._refresh_tickets()
            self.status.set(
                "Workspace recovery worker завершився без terminal result; цей workspace лишається fail-closed."
            )
            return

        self._recovery_view = result.session_view
        self.bank.set(self._bank_text())
        self._refresh_tickets()
        report = result.report
        summary = (
            "Workspace recovery: "
            f"reconciled={len(report.reconciled_keys)}; "
            f"aborted_uncommitted={len(report.aborted_uncommitted_keys)}; "
            f"unresolved={len(report.unresolved_without_summary)}; "
            f"workspace={result.session_view.workspace}"
        )
        self._append_log(summary)
        if report.unresolved_without_summary:
            self.status.set(
                summary
                + ". Є unresolved legacy run без достатнього summary proof; economic replay лишається fail-closed для цього workspace."
            )
            messagebox.showwarning(
                "Автоспорт",
                "Recovery завершив перевірку, але залишив unresolved run без достатнього доказу completion. "
                "Не обходьте цей стан через allow-repeat.",
            )
            return

        self._unblock_workspace_after_recovery(result.session_view.workspace)
        self.status.set(summary + ". Workspace готовий до наступного перевіреного paper replay.")
        messagebox.showinfo("Автоспорт", "Workspace recovery завершено без unresolved runs.")

    def _poll_replay_worker(self) -> None:
        """Consume replay terminal state and quarantine any uncertain workspace.

        A replay exception does not prove that no durable mutation happened before
        the exception. Likewise, a failure to reopen/validate the workspace after a
        nominally successful worker result makes its economic state uncertain. The
        packaged Windows product therefore requires an explicit successful recovery
        before another replay can mutate that same workspace.
        """

        message = self.replay_worker.poll()
        if message is None:
            self.after(100, self._poll_replay_worker)
            return

        self._set_replay_controls_busy(False)
        if message.error is not None or message.result is None:
            self._block_workspace_for_recovery(self._active_workspace)
            self._recovery_view = None

        try:
            self.session = self._open_session(
                self._active_strategy_id,
                self._active_research_plan,
            )
        except Exception as exc:
            self.session = None
            self._recovery_view = None
            self._block_workspace_for_recovery(self._active_workspace)
            self.bank.set(self._bank_text())
            self._refresh_tickets()
            self._set_evaluation_lines([
                "Evaluation недоступна: post-replay workspace reopen не пройшов fail-closed validation."
            ])
            detail = f"Post-replay workspace reopen відхилено fail-closed: {type(exc).__name__}: {exc}"
            if message.error is not None:
                detail = f"Paper replay помилка: {message.error}; {detail}"
            self.status.set(
                "Replay terminal state не можна безпечно підтвердити; цей economic workspace заблоковано fail-closed. "
                "Виконайте «Відновити workspace» або Control+Shift+R перед наступним replay у цьому workspace."
            )
            self._append_log(detail)
            messagebox.showerror("Автоспорт", detail)
            return

        self.bank.set(self._bank_text())
        self._refresh_tickets()

        if message.error is not None:
            text = f"Paper replay помилка: {message.error}"
            self._append_log(text)
            self._set_evaluation_lines([
                "Evaluation недоступна: replay не досяг terminal settlement/evaluation boundary."
            ])
            self.status.set(
                "Replay завершився помилкою; цей economic workspace заблоковано fail-closed. "
                "Виконайте «Відновити workspace» або Control+Shift+R перед наступним replay у цьому workspace."
            )
            messagebox.showerror("Автоспорт", text)
            return

        result = message.result
        if result is None:
            self._set_evaluation_lines([
                "Evaluation недоступна: worker не повернув terminal SessionResult."
            ])
            self.status.set(
                "Replay worker завершився без terminal result; цей economic workspace заблоковано fail-closed. "
                "Виконайте «Відновити workspace» перед наступним replay у цьому workspace."
            )
            return
        summary = result_summary(result)
        self._set_evaluation_lines(evaluation_lines(result))
        self.status.set(summary)
        self._append_log(summary)

    def run_dataset(self) -> None:
        if self._recovery_busy:
            self.status.set("Paper replay не запускається: workspace recovery ще виконується.")
            return
        if self._blocked_recovery_workspaces():
            try:
                strategy_id, research_plan = self._selected_replay_configuration()
                replay_workspace = workspace_for_strategy(self.workspace, strategy_id, research_plan)
            except Exception:
                # Let the base implementation render the canonical configuration
                # validation error rather than masking it as a recovery quarantine.
                return super().run_dataset()
            if self._workspace_requires_recovery(replay_workspace):
                self.status.set(
                    "Paper replay заблоковано fail-closed: поточний workspace не має успішного terminal recovery result."
                )
                return
        self._recovery_view = None
        super().run_dataset()

    def close_app(self) -> None:
        if self._recovery_busy:
            text = (
                "Workspace recovery ще виконується. Закриття програми заблоковано до terminal recovery state, "
                "щоб процес не обірвав economic reconciliation у довільній точці."
            )
            self.status.set(text)
            self._append_log(text)
            self.bell()
            return
        super().close_app()


def main() -> int:
    WindowsAutosportApp().mainloop()
    return 0
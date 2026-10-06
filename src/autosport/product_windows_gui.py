from __future__ import annotations

import os
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import tk_uia

from .localization import text
from .operator_source_registry import list_product_source_entries
from .product_gui_worker import ProductGuiMessage, ProductGuiWorker
from .replay_worker import workspace_for_strategy
from .research_strategy import ResearchStrategyPlan
from .windows_gui import WindowsAutosportApp


PRODUCT_RUNTIME_AUTOMATION_IDS = {
    "start": 206,
    "stop": 207,
    "status": 208,
}
_PRODUCT_SOURCE_FACTORY_ENV = "AUTOSPORT_PRODUCT_SOURCE_FACTORY"
_PRODUCT_POLL_SECONDS = 30.0


class ProductWindowsAutosportApp(WindowsAutosportApp):
    """Windows shell that orchestrates the existing canonical durable PAPER runtime."""

    def __init__(self) -> None:
        self.product_worker = ProductGuiWorker()
        self._product_close_pending = False
        self._product_last_stop: ProductGuiMessage | None = None
        self._product_runtime_workspace: Path | None = None
        self._product_restore_strategy_id: str | None = None
        self._product_restore_research_plan: ResearchStrategyPlan | None = None
        super().__init__()

    @property
    def _product_busy(self) -> bool:
        worker = self.__dict__.get("product_worker")
        return bool(worker is not None and worker.busy)

    def _resolve_product_runtime_binding(
        self,
    ) -> tuple[Path, str, ResearchStrategyPlan | None]:
        """Bind the runtime to the exact currently active economic workspace."""

        strategy_id = self.__dict__.get("_active_strategy_id")
        research_plan = self.__dict__.get("_active_research_plan")
        if (
            type(strategy_id) is not str
            or not strategy_id
            or strategy_id.strip() != strategy_id
        ):
            raise RuntimeError("active strategy identity is not canonical")

        expected_workspace = Path(
            workspace_for_strategy(self.workspace, strategy_id, research_plan)
        )
        active_workspace = self.__dict__.get("_active_workspace")
        try:
            active_matches = (
                active_workspace is not None
                and Path(active_workspace) == expected_workspace
            )
        except (TypeError, ValueError, OSError) as exc:
            raise RuntimeError("active economic workspace is not canonical") from exc
        if not active_matches:
            raise RuntimeError(
                "active economic workspace does not match active strategy identity"
            )

        session = self.__dict__.get("session")
        if session is not None:
            try:
                session_workspace = Path(session.workspace)
            except (AttributeError, TypeError, ValueError, OSError) as exc:
                raise RuntimeError("active session workspace is not canonical") from exc
            if session_workspace != expected_workspace:
                raise RuntimeError(
                    "active session workspace does not match active strategy identity"
                )
        return expected_workspace, strategy_id, research_plan

    def _clear_product_runtime_binding(self) -> None:
        self._product_runtime_workspace = None
        self._product_restore_strategy_id = None
        self._product_restore_research_plan = None

    def _product_runtime_target_workspace(self) -> Path:
        bound = self.__dict__.get("_product_runtime_workspace")
        if bound is not None:
            return Path(bound)
        active = self.__dict__.get("_active_workspace")
        if active is not None:
            return Path(active)
        return Path(self.workspace)

    def _build(self) -> None:
        super()._build()
        self.product_status = tk.StringVar(
            value=text("ui.product_runtime.status.idle")
        )

        # Preserve the compact Windows geometry: product controls share the
        # existing live-controls row instead of creating another vertical panel.
        live_controls = self.live_refresh_button.master
        self.product_start_button = ttk.Button(
            live_controls,
            text=text("ui.product_runtime.button.start"),
            command=self.start_product_runtime,
        )
        self.product_start_button.pack(side="left", padx=(8, 4))
        self.product_stop_button = ttk.Button(
            live_controls,
            text=text("ui.product_runtime.button.stop"),
            command=self.stop_product_runtime,
        )
        self.product_stop_button.pack(side="left", padx=(0, 4))
        self.product_stop_button.state(["disabled"])
        self.product_status_entry = ttk.Entry(
            live_controls,
            textvariable=self.product_status,
            state="readonly",
            takefocus=True,
            width=30,
        )
        self.product_status_entry.pack(side="left", fill="x", expand=True)

    def _configure_accessibility(self) -> None:
        super()._configure_accessibility()
        controls = (
            (
                self.product_start_button,
                text("ui.product_runtime.accessibility.start.name"),
                text("ui.product_runtime.accessibility.start.description"),
                PRODUCT_RUNTIME_AUTOMATION_IDS["start"],
            ),
            (
                self.product_stop_button,
                text("ui.product_runtime.accessibility.stop.name"),
                text("ui.product_runtime.accessibility.stop.description"),
                PRODUCT_RUNTIME_AUTOMATION_IDS["stop"],
            ),
            (
                self.product_status_entry,
                text("ui.product_runtime.accessibility.status.name"),
                text("ui.product_runtime.accessibility.status.description"),
                PRODUCT_RUNTIME_AUTOMATION_IDS["status"],
            ),
        )
        for widget, name, description, automation_id in controls:
            tk_uia.set_acc_name(widget, name)
            tk_uia.set_acc_description(widget, description)
            tk_uia.set_automation_id(widget, automation_id)

    def _product_operation_blocked(self) -> bool:
        if self._dataset_busy or self.replay_worker.busy or self.live_worker.busy:
            return True
        if self._evidence_export_busy or self._recovery_busy:
            return True
        return False

    def _set_product_controls_running(self, running: bool) -> None:
        if running:
            self.product_start_button.state(["disabled"])
            self.product_stop_button.state(["!disabled"])
            self._set_replay_controls_busy(True)
            return
        self.product_stop_button.state(["disabled"])
        self.product_start_button.state(["!disabled"])
        self._set_replay_controls_busy(False)

    def _product_blocks_base_operation(self) -> bool:
        if not self._product_busy:
            return False
        message = text("ui.product_runtime.status.operation_busy")
        self.status.set(message)
        self.product_status.set(message)
        self.bell()
        return True

    def choose_research_plan(self) -> None:
        if self._product_blocks_base_operation():
            return
        super().choose_research_plan()

    def choose_dataset(self) -> None:
        if self._product_blocks_base_operation():
            return
        super().choose_dataset()

    def refresh_live_snapshot(self) -> None:
        if self._product_blocks_base_operation():
            return
        super().refresh_live_snapshot()

    def export_evidence(self) -> None:
        if self._product_blocks_base_operation():
            return
        super().export_evidence()

    def repair_workspace(self) -> None:
        if self._product_blocks_base_operation():
            return
        super().repair_workspace()

    def run_dataset(self) -> None:
        if self._product_blocks_base_operation():
            return
        super().run_dataset()

    def start_product_runtime(self) -> None:
        if self.__dict__.get("_closing", False) or self._product_busy:
            return
        if self._product_operation_blocked():
            message = text("ui.product_runtime.status.operation_busy")
            self.product_status.set(message)
            self.status.set(message)
            self.bell()
            return

        try:
            workspace, restore_strategy_id, restore_research_plan = (
                self._resolve_product_runtime_binding()
            )
        except Exception:
            message = text("ui.product_runtime.status.workspace_identity_mismatch")
            self.product_status.set(message)
            self.status.set(message)
            self.bell()
            return

        if self._workspace_requires_recovery(workspace):
            message = text("ui.product_runtime.status.recovery_required")
            self.product_status.set(message)
            self.status.set(message)
            self.bell()
            return

        source_factory = os.environ.get(_PRODUCT_SOURCE_FACTORY_ENV)
        if source_factory is None or not source_factory:
            message = text("ui.product_runtime.status.configuration_missing")
            self.product_status.set(message)
            self.status.set(message)
            self.bell()
            return
        if source_factory.strip() != source_factory:
            message = text("ui.product_runtime.status.configuration_invalid")
            self.product_status.set(message)
            self.status.set(message)
            self.bell()
            return

        source_entries = tuple(
            entry
            for entry in list_product_source_entries()
            if entry.factory_spec == source_factory
        )
        if len(source_entries) != 1:
            message = text("ui.product_runtime.status.configuration_invalid")
            self.product_status.set(message)
            self.status.set(message)
            self.bell()
            return
        expected_source_id = source_entries[0].expected_provider_source_id

        # Freeze one exact economic identity before detaching the local session.
        # Runtime failure/recovery and the later local reopen must all refer to the
        # same strategy workspace, never silently fall back to the root workspace.
        self._product_runtime_workspace = workspace
        self._product_restore_strategy_id = restore_strategy_id
        self._product_restore_research_plan = restore_research_plan
        self._active_workspace = workspace
        self._recovery_view = None
        try:
            teardown_succeeded = self._hide_uncertain_economic_state(
                text("ui.product_runtime.status.starting")
            )
        except BaseException:
            self._clear_product_runtime_binding()
            raise
        if not teardown_succeeded:
            self._block_workspace_for_recovery(workspace)
            self._clear_product_runtime_binding()
            message = text("ui.product_runtime.status.session_close_failed")
            self.product_status.set(message)
            self.status.set(message)
            self.bell()
            return

        start_error: BaseException | None = None
        try:
            started = self.product_worker.start(
                workspace=workspace,
                source_factory=source_factory,
                expected_source_id=expected_source_id,
                initial_bankroll="10000",
                poll_seconds=_PRODUCT_POLL_SECONDS,
            )
        except BaseException as exc:
            start_error = exc
            started = False

        if not started:
            try:
                restored = self._restore_base_session_after_product()
            except BaseException:
                if start_error is not None and not isinstance(start_error, Exception):
                    raise start_error
                raise
            if start_error is not None and not isinstance(start_error, Exception):
                raise start_error
            # A failed reopen is the stronger safety truth; do not overwrite it
            # with the less specific worker-start failure presentation.
            if restored:
                message = text("ui.product_runtime.status.start_failed")
                self.product_status.set(message)
                self.status.set(message)
                self.bell()
            return

        self._product_last_stop = None
        self._set_product_controls_running(True)
        message = text("ui.product_runtime.status.starting")
        self.product_status.set(message)
        self.status.set(message)
        self._append_log(message)
        self.after(100, self._poll_product_worker)

    def stop_product_runtime(self) -> None:
        if not self._product_busy:
            message = text("ui.product_runtime.status.stop_not_running")
            self.product_status.set(message)
            return

        stop_error: BaseException | None = None
        try:
            accepted = self.product_worker.request_stop("operator_stop")
        except BaseException as exc:
            # ProductGuiWorker linearizes STOP before calling the runtime-specific
            # callback. Preserve that accepted STOP presentation even if the
            # callback faults; process-control exceptions are re-raised afterward.
            accepted = True
            stop_error = exc
        if accepted:
            self.product_stop_button.state(["disabled"])
            message = text("ui.product_runtime.status.stopping")
            self.product_status.set(message)
            self.status.set(message)
            self._append_log(message)
        if stop_error is not None and not isinstance(stop_error, Exception):
            raise stop_error

    def _restore_base_session_after_product(self) -> bool:
        if self.__dict__.get("_product_close_pending", False):
            return True

        target_workspace = self._product_runtime_target_workspace()
        strategy_id = self.__dict__.get("_product_restore_strategy_id")
        research_plan = self.__dict__.get("_product_restore_research_plan")

        if self.session is not None:
            try:
                if Path(self.session.workspace) != target_workspace:
                    raise RuntimeError(
                        "existing local session does not match product runtime workspace"
                    )
            except (AttributeError, TypeError, ValueError, OSError, RuntimeError):
                self._block_workspace_for_recovery(target_workspace)
                return False
            self._clear_product_runtime_binding()
            return True

        restored_session = None
        try:
            if (
                type(strategy_id) is not str
                or not strategy_id
                or strategy_id.strip() != strategy_id
            ):
                raise RuntimeError("product restore strategy identity is unavailable")
            restored_session = self._open_session(strategy_id, research_plan)
            if (
                Path(self._active_workspace) != target_workspace
                or Path(restored_session.workspace) != target_workspace
            ):
                raise RuntimeError(
                    "reopened local session workspace does not match product runtime"
                )
            self.session = restored_session
        except BaseException as exc:
            if restored_session is not None:
                try:
                    restored_session.close()
                except BaseException:
                    pass
            self.session = None
            self._active_workspace = target_workspace
            self._recovery_view = None
            self._block_workspace_for_recovery(target_workspace)
            self.bank.set(self._bank_text())
            self._refresh_tickets()
            message = text("ui.product_runtime.status.base_session_reopen_failed")
            self.product_status.set(message)
            self.status.set(message)
            self._append_log(message)
            if not isinstance(exc, Exception):
                raise
            return False

        self._recovery_view = None
        self._startup_economic_error = None
        self.bank.set(self._bank_text())
        self._refresh_tickets()
        self._clear_product_runtime_binding()
        message = text("ui.product_runtime.status.base_session_reopened")
        self.status.set(message)
        return True

    @staticmethod
    def _display_instant(value: str | None) -> str:
        return value if value is not None else "—"

    def _apply_product_message(self, message: ProductGuiMessage) -> None:
        if message.kind == "STARTED" and message.status is not None:
            status_text = text(
                "ui.product_runtime.status.running",
                source_id=message.status.source_id,
                cycles=message.status.cycles_completed,
                last_success_at=self._display_instant(message.status.last_success_at),
            )
            self.product_status.set(status_text)
            self.status.set(status_text)
            self._append_log(status_text)
            return

        if message.kind == "TICK" and message.tick is not None:
            status_text = text(
                "ui.product_runtime.status.tick",
                cycle_index=message.tick.cycle_index,
                committed=len(message.tick.committed_delta_ids),
                delivered=len(message.tick.delivered_delta_ids),
                settled=len(message.tick.settled_ticket_ids),
                last_success_at=self._display_instant(message.tick.last_success_at),
            )
            self.product_status.set(status_text)
            self.status.set(status_text)
            return

        if message.kind == "STOPPED" and message.status is not None:
            self._product_last_stop = message
            status_text = text(
                "ui.product_runtime.status.stopped",
                reason=message.stop_reason or "operator_stop",
                cycles=message.status.cycles_completed,
            )
            self.product_status.set(status_text)
            self.status.set(status_text)
            self._append_log(status_text)
            return

        if message.kind == "ERROR" and message.error_type is not None:
            self._product_last_stop = None
            self._block_workspace_for_recovery(
                self._product_runtime_target_workspace()
            )
            status_text = text(
                "ui.product_runtime.status.error",
                error_type=message.error_type,
            )
            self.product_status.set(status_text)
            self.status.set(status_text)
            self._append_log(status_text)

    def _poll_product_worker(self) -> None:
        message = self.product_worker.poll()
        if message is not None:
            self._apply_product_message(message)
            self.after(50, self._poll_product_worker)
            return

        if self._product_busy:
            self.after(100, self._poll_product_worker)
            return

        # Drain any terminal message enqueued immediately before busy was cleared.
        message = self.product_worker.poll()
        if message is not None:
            self._apply_product_message(message)
            self.after(0, self._poll_product_worker)
            return

        self._set_product_controls_running(False)
        if self.__dict__.get("_product_close_pending", False):
            super().close_app()
            return
        if self._product_last_stop is not None:
            self._restore_base_session_after_product()
        else:
            self._block_workspace_for_recovery(
                self._product_runtime_target_workspace()
            )
            self.bank.set(self._bank_text())
            self._refresh_tickets()

    def close_app(self) -> None:
        if self._product_close_pending:
            return
        if self._product_busy:
            self._product_close_pending = True
            stop_error: BaseException | None = None
            try:
                self.product_worker.request_stop("app_close")
            except BaseException as exc:
                # The canonical worker accepts/revokes STOP under its lifecycle
                # lock before invoking a runtime-specific stop callback. Keep the
                # close pending state and let the already-running poll loop consume
                # terminal truth even if that callback faults.
                stop_error = exc
            self.product_start_button.state(["disabled"])
            self.product_stop_button.state(["disabled"])
            message = text("ui.product_runtime.status.close_wait")
            self.product_status.set(message)
            self.status.set(message)
            self._append_log(message)
            # start_product_runtime already owns exactly one polling loop. Do not
            # schedule a second competing consumer during application close.
            if stop_error is not None and not isinstance(stop_error, Exception):
                raise stop_error
            return
        super().close_app()



def main() -> int:
    ProductWindowsAutosportApp().mainloop()
    return 0

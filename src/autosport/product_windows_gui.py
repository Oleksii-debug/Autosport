from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import ttk

import tk_uia

from .localization_product_runtime import product_text
from .operator_source_configuration import (
    OperatorSourceConfigurationError,
    load_operator_source_configuration,
    save_operator_source_configuration,
)
from .operator_source_registry import list_product_source_entries
from .product_gui_worker import ProductGuiMessage, ProductGuiWorker
from .session import AutosportSession
from .windows_gui import WindowsAutosportApp


PRODUCT_RUNTIME_AUTOMATION_IDS = {
    "start": 206,
    "stop": 207,
    "status": 208,
    "source": 209,
    "source_save": 210,
}
_PRODUCT_POLL_SECONDS = 30.0


class ProductWindowsAutosportApp(WindowsAutosportApp):
    """Packaged Windows shell over the canonical durable PAPER runtime."""

    def __init__(self) -> None:
        self.product_worker = ProductGuiWorker()
        self._product_close_pending = False
        self._product_last_stop: ProductGuiMessage | None = None
        super().__init__()

    @property
    def _product_busy(self) -> bool:
        worker = self.__dict__.get("product_worker")
        return bool(worker is not None and worker.busy)

    def _build(self) -> None:
        super()._build()
        self.product_status = tk.StringVar(
            value=product_text("ui.product_runtime.status.idle")
        )
        self.product_source = tk.StringVar(value="")
        entries = list_product_source_entries()
        self._product_source_display_to_id = {
            product_text(
                f"ui.product_runtime.source.option.{entry.source_id}"
            ): entry.source_id
            for entry in entries
        }
        self._product_source_id_to_display = {
            source_id: display
            for display, source_id in self._product_source_display_to_id.items()
        }

        live_controls = self.live_refresh_button.master
        self.product_source_label = ttk.Label(
            live_controls,
            text=product_text("ui.product_runtime.source.label"),
        )
        self.product_source_label.pack(side="left", padx=(8, 4))
        self.product_source_choice = ttk.Combobox(
            live_controls,
            textvariable=self.product_source,
            values=tuple(self._product_source_display_to_id),
            state="readonly",
            width=24,
            takefocus=True,
        )
        self.product_source_choice.pack(side="left", padx=(0, 4))
        self.product_source_save_button = ttk.Button(
            live_controls,
            text=product_text("ui.product_runtime.source.save"),
            command=self.save_product_source_configuration,
        )
        self.product_source_save_button.pack(side="left", padx=(0, 8))
        self.product_start_button = ttk.Button(
            live_controls,
            text=product_text("ui.product_runtime.button.start"),
            command=self.start_product_runtime,
        )
        self.product_start_button.pack(side="left", padx=(0, 4))
        self.product_stop_button = ttk.Button(
            live_controls,
            text=product_text("ui.product_runtime.button.stop"),
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
        self._load_product_source_for_display()

    def _configure_accessibility(self) -> None:
        super()._configure_accessibility()
        controls = (
            (
                self.product_start_button,
                product_text("ui.product_runtime.accessibility.start.name"),
                product_text("ui.product_runtime.accessibility.start.description"),
                PRODUCT_RUNTIME_AUTOMATION_IDS["start"],
            ),
            (
                self.product_stop_button,
                product_text("ui.product_runtime.accessibility.stop.name"),
                product_text("ui.product_runtime.accessibility.stop.description"),
                PRODUCT_RUNTIME_AUTOMATION_IDS["stop"],
            ),
            (
                self.product_status_entry,
                product_text("ui.product_runtime.accessibility.status.name"),
                product_text("ui.product_runtime.accessibility.status.description"),
                PRODUCT_RUNTIME_AUTOMATION_IDS["status"],
            ),
            (
                self.product_source_choice,
                product_text("ui.product_runtime.accessibility.source.name"),
                product_text("ui.product_runtime.accessibility.source.description"),
                PRODUCT_RUNTIME_AUTOMATION_IDS["source"],
            ),
            (
                self.product_source_save_button,
                product_text("ui.product_runtime.accessibility.source_save.name"),
                product_text(
                    "ui.product_runtime.accessibility.source_save.description"
                ),
                PRODUCT_RUNTIME_AUTOMATION_IDS["source_save"],
            ),
        )
        for widget, name, description, automation_id in controls:
            tk_uia.set_acc_name(widget, name)
            tk_uia.set_acc_description(widget, description)
            tk_uia.set_automation_id(widget, automation_id)

    def _load_product_source_for_display(self) -> None:
        workspace = Path(self.workspace)
        try:
            configured = load_operator_source_configuration(workspace)
        except OperatorSourceConfigurationError:
            self.product_source.set("")
            self.product_status.set(
                product_text("ui.product_runtime.status.configuration_invalid")
            )
            return
        if configured is None:
            self.product_source.set("")
            self.product_status.set(
                product_text("ui.product_runtime.status.configuration_required")
            )
            return
        display = self._product_source_id_to_display.get(configured.source_id)
        if display is None:
            self.product_source.set("")
            self.product_status.set(
                product_text("ui.product_runtime.status.configuration_invalid")
            )
            return
        self.product_source.set(display)

    def save_product_source_configuration(self) -> None:
        if self.__dict__.get("_closing", False):
            return
        if self._product_busy:
            message = product_text("ui.product_runtime.status.configuration_busy")
            self.product_status.set(message)
            self.status.set(message)
            self.bell()
            return
        display = self.product_source.get()
        source_id = self._product_source_display_to_id.get(display)
        if source_id is None:
            message = product_text(
                "ui.product_runtime.status.configuration_invalid"
            )
            self.product_status.set(message)
            self.status.set(message)
            self.bell()
            return
        try:
            configured = save_operator_source_configuration(
                Path(self.workspace),
                source_id,
            )
        except OperatorSourceConfigurationError:
            message = product_text(
                "ui.product_runtime.status.configuration_invalid"
            )
            self.product_status.set(message)
            self.status.set(message)
            self.bell()
            return
        configured_display = self._product_source_id_to_display.get(
            configured.source_id
        )
        if configured_display is None:
            message = product_text(
                "ui.product_runtime.status.configuration_invalid"
            )
            self.product_status.set(message)
            self.status.set(message)
            self.bell()
            return
        self.product_source.set(configured_display)
        message = product_text(
            "ui.product_runtime.status.configuration_saved",
            source_id=configured.source_id,
        )
        self.product_status.set(message)
        self.status.set(message)
        self._append_log(message)

    def _product_operation_blocked(self) -> bool:
        replay_worker = self.__dict__.get("replay_worker")
        live_worker = self.__dict__.get("live_worker")
        if self._dataset_busy:
            return True
        if replay_worker is not None and replay_worker.busy:
            return True
        if live_worker is not None and live_worker.busy:
            return True
        if self._evidence_export_busy or self._recovery_busy:
            return True
        return False

    def _set_product_controls_running(self, running: bool) -> None:
        if running:
            self.product_start_button.state(["disabled"])
            self.product_stop_button.state(["!disabled"])
            self.product_source_choice.configure(state="disabled")
            self.product_source_save_button.state(["disabled"])
            self._set_replay_controls_busy(True)
            return
        self.product_stop_button.state(["disabled"])
        self.product_start_button.state(["!disabled"])
        self.product_source_choice.configure(state="readonly")
        self.product_source_save_button.state(["!disabled"])
        self._set_replay_controls_busy(False)

    def _product_blocks_base_operation(self) -> bool:
        if not self._product_busy:
            return False
        message = product_text("ui.product_runtime.status.operation_busy")
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
            message = product_text("ui.product_runtime.status.operation_busy")
            self.product_status.set(message)
            self.status.set(message)
            self.bell()
            return

        workspace = Path(self.workspace)
        if self._workspace_requires_recovery(workspace):
            message = product_text("ui.product_runtime.status.recovery_required")
            self.product_status.set(message)
            self.status.set(message)
            self.bell()
            return

        try:
            configured = load_operator_source_configuration(workspace)
        except OperatorSourceConfigurationError:
            message = product_text(
                "ui.product_runtime.status.configuration_invalid"
            )
            self.product_status.set(message)
            self.status.set(message)
            self.bell()
            return
        if configured is None:
            message = product_text(
                "ui.product_runtime.status.configuration_required"
            )
            self.product_status.set(message)
            self.status.set(message)
            self.bell()
            return
        configured_display = self._product_source_id_to_display.get(
            configured.source_id
        )
        if (
            configured_display is None
            or self.product_source.get() != configured_display
        ):
            message = product_text(
                "ui.product_runtime.status.configuration_unsaved"
            )
            self.product_status.set(message)
            self.status.set(message)
            self.bell()
            return

        self._active_workspace = workspace
        self._recovery_view = None
        if not self._hide_uncertain_economic_state(
            product_text("ui.product_runtime.status.starting")
        ):
            self._block_workspace_for_recovery(workspace)
            message = product_text(
                "ui.product_runtime.status.session_close_failed"
            )
            self.product_status.set(message)
            self.status.set(message)
            self.bell()
            return

        try:
            started = self.product_worker.start(
                workspace=workspace,
                source_factory=configured.entry.factory_spec,
                expected_source_id=configured.entry.expected_provider_source_id,
                initial_bankroll="10000",
                poll_seconds=_PRODUCT_POLL_SECONDS,
            )
        except Exception:
            started = False

        if not started:
            self._restore_base_session_after_product()
            message = product_text("ui.product_runtime.status.start_failed")
            self.product_status.set(message)
            self.status.set(message)
            self.bell()
            return

        self._product_last_stop = None
        self._set_product_controls_running(True)
        message = product_text("ui.product_runtime.status.starting")
        self.product_status.set(message)
        self.status.set(message)
        self._append_log(message)
        self.after(100, self._poll_product_worker)

    def stop_product_runtime(self) -> None:
        if not self._product_busy:
            self.product_status.set(
                product_text("ui.product_runtime.status.stop_not_running")
            )
            return
        if self.product_worker.request_stop("operator_stop"):
            self.product_stop_button.state(["disabled"])
            message = product_text("ui.product_runtime.status.stopping")
            self.product_status.set(message)
            self.status.set(message)
            self._append_log(message)

    def _restore_base_session_after_product(self) -> bool:
        if self.__dict__.get("_product_close_pending", False):
            return True
        if self.session is not None:
            return True
        try:
            self.session = AutosportSession(self.workspace, "10000")
        except Exception:
            self.session = None
            self._block_workspace_for_recovery(Path(self.workspace))
            self.bank.set(self._bank_text())
            self._refresh_tickets()
            message = product_text(
                "ui.product_runtime.status.base_session_reopen_failed"
            )
            self.product_status.set(message)
            self.status.set(message)
            self._append_log(message)
            return False
        self._recovery_view = None
        self._active_workspace = Path(self.workspace)
        self.bank.set(self._bank_text())
        self._refresh_tickets()
        message = product_text(
            "ui.product_runtime.status.base_session_reopened"
        )
        self.status.set(message)
        return True

    @staticmethod
    def _display_instant(value: str | None) -> str:
        return value if value is not None else "—"

    def _apply_product_message(self, message: ProductGuiMessage) -> None:
        if message.kind == "STARTED" and message.status is not None:
            status_text = product_text(
                "ui.product_runtime.status.running",
                source_id=message.status.source_id,
                cycles=message.status.cycles_completed,
                last_success_at=self._display_instant(
                    message.status.last_success_at
                ),
            )
            self.product_status.set(status_text)
            self.status.set(status_text)
            self._append_log(status_text)
            return

        if message.kind == "TICK" and message.tick is not None:
            status_text = product_text(
                "ui.product_runtime.status.tick",
                cycle_index=message.tick.cycle_index,
                committed=len(message.tick.committed_delta_ids),
                delivered=len(message.tick.delivered_delta_ids),
                settled=len(message.tick.settled_ticket_ids),
                last_success_at=self._display_instant(
                    message.tick.last_success_at
                ),
            )
            self.product_status.set(status_text)
            self.status.set(status_text)
            return

        if message.kind == "STOPPED" and message.status is not None:
            self._product_last_stop = message
            status_text = product_text(
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
            self._block_workspace_for_recovery(Path(self.workspace))
            status_text = product_text(
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
            self._block_workspace_for_recovery(Path(self.workspace))
            self.bank.set(self._bank_text())
            self._refresh_tickets()

    def close_app(self) -> None:
        if self._product_close_pending:
            return
        if self._product_busy:
            self._product_close_pending = True
            self.product_worker.request_stop("app_close")
            self.product_start_button.state(["disabled"])
            self.product_stop_button.state(["disabled"])
            self.product_source_choice.configure(state="disabled")
            self.product_source_save_button.state(["disabled"])
            message = product_text("ui.product_runtime.status.close_wait")
            self.product_status.set(message)
            self.status.set(message)
            self._append_log(message)
            self.after(100, self._poll_product_worker)
            return
        super().close_app()


def main() -> int:
    ProductWindowsAutosportApp().mainloop()
    return 0

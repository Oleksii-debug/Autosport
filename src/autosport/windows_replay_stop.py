from __future__ import annotations

from tkinter import messagebox, ttk

import tk_uia

from .localization import require_keys, text


REPLAY_STOP_AUTOMATION_ID = 110
REPLAY_STOP_LOCALIZATION_KEYS = frozenset(
    {
        "ui.windows.replay_stop.button",
        "ui.windows.replay_stop.accessibility.name",
        "ui.windows.replay_stop.accessibility.description",
        "ui.windows.replay_stop.status.requested",
        "ui.windows.replay_stop.status.stopped",
        "ui.windows.replay_stop.status.unavailable",
        "ui.windows.replay_stop.evaluation.stopped",
        "ui.windows.replay_stop.log.requested",
        "ui.windows.replay_stop.log.stopped",
    }
)
require_keys(REPLAY_STOP_LOCALIZATION_KEYS)


def _stop_control_enabled(worker) -> bool:
    """Keep STOP discoverable when idle; disable only during an un-stoppable flight."""

    if worker is None or not worker.busy:
        return True
    return bool(worker.stop_available)


def _set_stop_control_state(button, worker) -> None:
    if button is None:
        return
    if _stop_control_enabled(worker):
        button.state(["!disabled"])
    else:
        button.state(["disabled"])


def _request_replay_stop(app) -> None:
    worker = app.__dict__.get("replay_worker")
    if worker is None or not worker.request_stop():
        # Idle STOP remains a real, focusable action so keyboard/NVDA users can
        # discover it and receive truthful "nothing to stop" feedback. If a
        # worker is in the narrow post-completion/pre-poll window, keep it disabled.
        _set_stop_control_state(app.__dict__.get("stop_replay_button"), worker)
        message = text("ui.windows.replay_stop.status.unavailable")
        app.status.set(message)
        app._append_log(message)
        app.bell()
        return

    button = app.__dict__.get("stop_replay_button")
    if button is not None:
        # The accepted request is single-shot. Re-enable only after the worker
        # publishes and the GUI consumes its terminal state.
        button.state(["disabled"])
    app.status.set(text("ui.windows.replay_stop.status.requested"))
    app._append_log(text("ui.windows.replay_stop.log.requested"))


def install_windows_replay_stop() -> None:
    """Install the packaged Windows STOP surface before GUI/audit construction.

    This follows the same pre-construction wrapper pattern as windows_layout: the
    packaged interactive app and machine accessibility audits therefore see the
    same keyboard-focusable/UIA-addressable control.
    """

    from .gui import AutosportApp
    from .windows_gui import WindowsAutosportApp, _safe_exception_detail

    if getattr(AutosportApp, "_windows_replay_stop_installed", False):
        return

    original_build = AutosportApp._build
    original_configure_accessibility = AutosportApp._configure_accessibility
    original_set_replay_controls_busy = AutosportApp._set_replay_controls_busy
    original_windows_poll_replay_worker = WindowsAutosportApp._poll_replay_worker

    def build_with_replay_stop(self) -> None:
        original_build(self)
        controls = self.run_button.master
        self.stop_replay_button = ttk.Button(
            controls,
            text=text("ui.windows.replay_stop.button"),
            command=lambda: _request_replay_stop(self),
            takefocus=True,
        )
        self.stop_replay_button.pack(
            side="left",
            padx=(0, 8),
            after=self.run_button,
        )
        self.bind("<Control-s>", lambda _event: _request_replay_stop(self))

    def configure_with_replay_stop(self) -> None:
        original_configure_accessibility(self)
        tk_uia.set_acc_name(
            self.stop_replay_button,
            text("ui.windows.replay_stop.accessibility.name"),
        )
        tk_uia.set_acc_description(
            self.stop_replay_button,
            text("ui.windows.replay_stop.accessibility.description"),
        )
        tk_uia.set_automation_id(self.stop_replay_button, REPLAY_STOP_AUTOMATION_ID)

    def set_controls_with_replay_stop(self, busy: bool) -> None:
        original_set_replay_controls_busy(self, busy)
        _set_stop_control_state(
            self.__dict__.get("stop_replay_button"),
            self.__dict__.get("replay_worker"),
        )

    def poll_windows_replay_worker_with_stop(self) -> None:
        worker = self.replay_worker
        if not worker.stopped_pending:
            return original_windows_poll_replay_worker(self)

        message = worker.poll()
        if message is None:
            self.after(100, self._poll_replay_worker)
            return

        self._set_replay_controls_busy(False)
        if not message.stopped:
            # stopped_pending and poll() are one queue/lock contract. If that
            # invariant is ever broken, fail closed instead of fabricating success.
            self._block_workspace_for_recovery(self._active_workspace)
            self._recovery_view = None
            self.session = None
            self.bank.set(self._bank_text())
            self._refresh_tickets()
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

        self._startup_economic_error = None
        self._recovery_view = None
        self.bank.set(self._bank_text())
        self._refresh_tickets()
        self._set_evaluation_lines([text("ui.windows.replay_stop.evaluation.stopped")])
        self.status.set(text("ui.windows.replay_stop.status.stopped"))
        self._append_log(text("ui.windows.replay_stop.log.stopped"))

    # Also make the request operation available to headless contract tests and
    # assistive wrappers without requiring a second controller object.
    AutosportApp.request_replay_stop = _request_replay_stop
    AutosportApp._build = build_with_replay_stop
    AutosportApp._configure_accessibility = configure_with_replay_stop
    AutosportApp._set_replay_controls_busy = set_controls_with_replay_stop
    WindowsAutosportApp._poll_replay_worker = poll_windows_replay_worker_with_stop
    AutosportApp._windows_replay_stop_installed = True

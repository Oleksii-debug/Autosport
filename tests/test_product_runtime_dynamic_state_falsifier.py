from __future__ import annotations

from pathlib import Path

from autosport.continuous_session import ContinuousSessionStatus, SessionState
from autosport.product_gui_worker import ProductGuiMessage
from autosport.product_windows_gui import ProductWindowsAutosportApp


class _Button:
    def __init__(self, *, disabled: bool) -> None:
        self.disabled = disabled

    def state(self, states=None):
        if states is None:
            return ("disabled",) if self.disabled else ()
        for state in states:
            if state == "disabled":
                self.disabled = True
            elif state == "!disabled":
                self.disabled = False
        return ()


class _Value:
    def __init__(self) -> None:
        self.value = None
        self.history: list[object] = []

    def set(self, value) -> None:
        self.value = value
        self.history.append(value)


class _IdleWorker:
    busy = False

    @staticmethod
    def poll():
        return None


class _Harness:
    _set_product_controls_running = ProductWindowsAutosportApp._set_product_controls_running

    def __init__(self) -> None:
        # Begin in the exact control state established for RUNNING.
        self.product_start_button = _Button(disabled=True)
        self.product_stop_button = _Button(disabled=False)
        self.product_status = _Value()
        self.status = _Value()
        self.bank = _Value()
        self.product_worker = _IdleWorker()
        self._product_busy = False
        self._product_last_stop = None
        self._product_close_pending = False
        self.workspace = "dynamic-state-test-workspace"
        self.replay_busy_history: list[bool] = []
        self.recovery_blocks: list[Path] = []
        self.log: list[str] = []
        self.refresh_count = 0

    def _set_replay_controls_busy(self, busy: bool) -> None:
        self.replay_busy_history.append(busy)

    def _append_log(self, message: str) -> None:
        self.log.append(message)

    def _block_workspace_for_recovery(self, workspace: Path) -> None:
        self.recovery_blocks.append(workspace)

    @staticmethod
    def _bank_text() -> str:
        return "bank"

    def _refresh_tickets(self) -> None:
        self.refresh_count += 1


def _stopped_status() -> ContinuousSessionStatus:
    return ContinuousSessionStatus(
        session_id="session-1",
        source_id="source-1",
        state=SessionState.STOPPED,
        cycles_completed=3,
        last_success_at="2026-09-21T18:15:00+00:00",
        last_error_code=None,
        last_full_refresh_at=None,
        settlement_evidence=(),
    )


def test_stopped_status_switches_actionability_in_the_same_ui_callback() -> None:
    """STOPPED text and START/STOP machine actionability must change atomically."""

    app = _Harness()
    message = ProductGuiMessage(
        kind="STOPPED",
        status=_stopped_status(),
        stop_reason="operator_stop",
    )

    ProductWindowsAutosportApp._apply_product_message(app, message)

    assert app.product_start_button.disabled is False, (
        "once the accessible runtime status is STOPPED, START must no longer remain "
        "disabled merely until a later polling callback"
    )
    assert app.product_stop_button.disabled is True, (
        "once the accessible runtime status is STOPPED, STOP must not remain "
        "machine-actionable until a later polling callback"
    )


def test_error_recovery_terminal_state_never_reenables_start_control() -> None:
    """A recovery-blocked workspace must not advertise START as actionable."""

    app = _Harness()
    message = ProductGuiMessage(kind="ERROR", error_type="RuntimeError")

    ProductWindowsAutosportApp._apply_product_message(app, message)

    # The terminal error has already moved the workspace into recovery-required
    # truth, so STOP is no longer a live runtime action and START cannot be offered.
    assert app.recovery_blocks == [Path(app.workspace)]
    assert app.product_start_button.disabled is True
    assert app.product_stop_button.disabled is True, (
        "terminal ERROR status must not leave a stale live STOP action enabled"
    )

    # The next empty poll must not turn START back on while recovery remains required.
    ProductWindowsAutosportApp._poll_product_worker(app)

    assert app.product_start_button.disabled is True, (
        "terminal recovery must not advertise START merely because the worker is idle"
    )
    assert app.product_stop_button.disabled is True
    assert app.recovery_blocks[-1] == Path(app.workspace)

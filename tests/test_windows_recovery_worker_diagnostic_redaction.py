from __future__ import annotations

from autosport.recovery_worker import RecoveryWorkerMessage
from autosport.windows_gui import WindowsAutosportApp
import autosport.windows_gui as windows_gui


_SECRET = "Authorization: Bearer AUTOSPORT-SECRET-SENTINEL"


class _ValueSink:
    def __init__(self) -> None:
        self.values: list[str] = []

    def set(self, value: str) -> None:
        self.values.append(value)


class _OneMessageWorker:
    def __init__(self, error: str) -> None:
        self._message = RecoveryWorkerMessage(error=error)

    def poll(self) -> RecoveryWorkerMessage:
        return self._message


class _RecoveryPollHarness:
    def __init__(self, error: str) -> None:
        self.recovery_worker = _OneMessageWorker(error)
        self._recovery_view = object()
        self.bank = _ValueSink()
        self.status = _ValueSink()
        self.log: list[str] = []
        self.busy_transitions: list[bool] = []
        self.refreshed = False

    def _set_replay_controls_busy(self, busy: bool) -> None:
        self.busy_transitions.append(busy)

    def _bank_text(self) -> str:
        return "safe-bank-summary"

    def _refresh_tickets(self) -> None:
        self.refreshed = True

    def _append_log(self, value: str) -> None:
        self.log.append(value)


def test_recovery_worker_raw_secret_diagnostic_never_reaches_operator_surfaces(
    monkeypatch,
) -> None:
    dialogs: list[tuple[str, str]] = []
    monkeypatch.setattr(
        windows_gui.messagebox,
        "showerror",
        lambda title, message: dialogs.append((title, message)),
    )
    app = _RecoveryPollHarness(_SECRET)

    WindowsAutosportApp._poll_recovery_worker(app)

    assert app._recovery_view is None
    assert app.busy_transitions == [False]
    assert app.refreshed is True
    assert dialogs

    rendered = "\n".join(
        app.log
        + app.status.values
        + [title for title, _message in dialogs]
        + [message for _title, message in dialogs]
    )
    for forbidden in (
        "AUTOSPORT-SECRET-SENTINEL",
        "Authorization",
        "Bearer",
    ):
        assert forbidden not in rendered

    assert "Відновлення" in rendered or "відновлення" in rendered

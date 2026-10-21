"""Plan 6 section 3A: native Windows worker error sinks cannot reveal secrets."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from autosport.secret_redaction import REDACTED
from autosport.windows_gui import WindowsAutosportApp


class _Value:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value


@pytest.mark.parametrize("worker_kind", ("recovery", "replay"))
def test_native_worker_error_is_redacted_in_log_and_dialog(
    worker_kind: str, tmp_path: Path,
) -> None:
    secret = "SYNTHETIC_WINDOWS_NATIVE_WORKER_SECRET_20261021"
    message = SimpleNamespace(
        error="Workspace could not be recovered: api_key=" + secret,
        result=None,
    )
    worker = SimpleNamespace(poll=lambda: message)
    app = object.__new__(WindowsAutosportApp)
    app._logs = []
    app._append_log = lambda entry: app._logs.append(entry)
    app._set_replay_controls_busy = lambda busy: None
    app._bank_text = lambda: "no economic state"
    app._refresh_tickets = lambda: None
    app.bank = _Value()
    app.status = _Value()
    app._recovery_view = None
    app._active_workspace = tmp_path / "paper"

    if worker_kind == "recovery":
        app.recovery_worker = worker
        method = WindowsAutosportApp._poll_recovery_worker
    else:
        app.replay_worker = worker
        app._block_workspace_for_recovery = lambda workspace: None
        app._set_evaluation_lines = lambda lines: None
        method = WindowsAutosportApp._poll_replay_worker

    with patch("autosport.windows_gui.messagebox.showerror") as showerror:
        method(app)

    assert showerror.call_count == 1
    displayed = repr((app._logs, showerror.call_args, app.status.value))
    assert secret not in displayed
    assert REDACTED in displayed
    assert "Workspace could not be recovered" in displayed

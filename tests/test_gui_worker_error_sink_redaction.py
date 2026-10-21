"""Plan 6 section 3A: worker-originated errors are presentation-redacted.

These tests use the real GUI poll methods without constructing a Tk window,
so message processing, log export and dialog/error sinks are all exercised.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from autosport.gui import AutosportApp, _safe_worker_error_text
from autosport.secret_redaction import REDACTED


class _Value:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value


class _Worker:
    def __init__(self, error: object) -> None:
        self._message = SimpleNamespace(error=error, result=None)

    def poll(self):
        return self._message


def _bare_app() -> AutosportApp:
    app = object.__new__(AutosportApp)
    app._closing = False
    app._logs = []
    app._append_log = lambda message: app._logs.append(message)
    app._set_replay_controls_busy = lambda busy: None
    app.status = _Value()
    app.live_status = _Value()
    return app


@pytest.mark.parametrize("sink", ("dataset", "live", "evidence", "replay"))
def test_worker_secret_never_reaches_operator_status_log_or_dialog(
    sink: str, tmp_path: Path,
) -> None:
    secret = "SYNTHETIC_GUI_WORKER_SECRET_20261021"
    error = "Provider unavailable: api_key=" + secret
    worker = _Worker(error)
    app = _bare_app()

    if sink == "dataset":
        app.dataset_worker = worker
        app._pending_dataset_path = None
        call = AutosportApp._poll_dataset_worker
    elif sink == "live":
        app.live_worker = worker
        app.live_refresh_button = SimpleNamespace(state=lambda state: None)
        call = AutosportApp._poll_live_worker
    elif sink == "evidence":
        app.evidence_export_worker = worker
        call = AutosportApp._poll_evidence_export_worker
    else:
        app.replay_worker = worker
        app._active_workspace = tmp_path / "paper"
        app._recovery_required_workspaces = set()
        app._hide_uncertain_economic_state = lambda status: None
        app._set_evaluation_lines = lambda lines: None
        call = AutosportApp._poll_replay_worker

    with patch("autosport.gui.messagebox.showerror") as showerror:
        call(app)

    displayed = repr(
        (app.status.value, app.live_status.value, app._logs, showerror.call_args)
    )
    assert secret not in displayed
    assert REDACTED in displayed
    assert "Provider unavailable" in displayed


def test_worker_error_redaction_preserves_benign_failure_and_masks_credentials() -> None:
    message = "ValueError: failed to download source; Authorization: Bearer abcdef.12345"
    rendered = _safe_worker_error_text(message)
    assert "failed to download source" in rendered
    assert "abcdef.12345" not in rendered
    assert REDACTED in rendered


def test_worker_error_rejects_untrusted_non_string_without_stringifying() -> None:
    class Dangerous:
        def __str__(self) -> str:
            raise RuntimeError("secret must not be stringified")

    assert _safe_worker_error_text(Dangerous()) == "worker error details unavailable"
    assert _safe_worker_error_text(None) == "worker error details unavailable"


def test_worker_error_rejects_string_subclass_with_hostile_override() -> None:
    class DangerousString(str):
        def __str__(self) -> str:
            raise RuntimeError("malicious string subclass")

    assert _safe_worker_error_text(DangerousString("api_key=private")) == (
        "worker error details unavailable"
    )

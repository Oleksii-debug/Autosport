from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from autosport.gui import _safe_exception_text
from autosport.secret_redaction import REDACTED
from autosport.windows_gui import WindowsAutosportApp, _safe_exception_detail


_RENDERERS = (_safe_exception_text, _safe_exception_detail)


@pytest.mark.parametrize("renderer", _RENDERERS)
def test_operator_gui_exception_text_redacts_credentials(
    renderer,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment_secret = "env-secret-token-93715"
    bearer_secret = "bearer-secret-token-48126"
    query_secret = "query-secret-token-72504"
    monkeypatch.setenv("AUTOSPORT_PARLAYAPI_KEY", environment_secret)

    rendered = renderer(
        RuntimeError(
            "Authorization: Bearer "
            + bearer_secret
            + "; api_key="
            + query_secret
            + "; password=abc; echo="
            + environment_secret
        )
    )

    assert rendered.startswith("RuntimeError:")
    assert REDACTED in rendered
    for secret in (
        environment_secret,
        bearer_secret,
        query_secret,
        "password=abc",
    ):
        assert secret not in rendered


@pytest.mark.parametrize("renderer", _RENDERERS)
def test_operator_gui_exception_type_name_cannot_carry_secret(renderer) -> None:
    type_secret = "TYPE-SECRET-58319"
    secret_type = type(
        "api_key=" + type_secret,
        (RuntimeError,),
        {},
    )

    rendered = renderer(secret_type("safe detail"))

    assert type_secret not in rendered
    assert REDACTED in rendered
    assert "safe detail" in rendered

def test_gui_exception_renderers_preserve_empty_message_semantics() -> None:
    assert _safe_exception_text(RuntimeError()) == "RuntimeError"
    assert _safe_exception_detail(RuntimeError()) == "RuntimeError: "

class _Value:
    def __init__(self) -> None:
        self.value = ""

    def set(self, value: str) -> None:
        self.value = value


def test_dataset_worker_error_is_redacted_at_gui_sink() -> None:
    secret = "dataset-worker-secret-47112"
    app = object.__new__(AutosportApp)
    app._closing = False
    app.dataset_worker = SimpleNamespace(
        poll=lambda: SimpleNamespace(
            error="api_key=" + secret,
            result=None,
        )
    )
    app._pending_dataset_path = Path("dataset.json")
    app._set_replay_controls_busy = lambda _busy: None
    app.status = _Value()
    logs: list[str] = []
    app._append_log = logs.append

    with patch("autosport.gui.messagebox.showerror") as showerror:
        AutosportApp._poll_dataset_worker(app)

    assert len(logs) == 1
    assert secret not in logs[0]
    assert REDACTED in logs[0]
    shown = showerror.call_args.args[1]
    assert secret not in shown
    assert REDACTED in shown


def test_windows_recovery_worker_error_is_redacted_at_gui_sink() -> None:
    secret = "recovery-worker-secret-58341"
    app = object.__new__(WindowsAutosportApp)
    app.recovery_worker = SimpleNamespace(
        poll=lambda: SimpleNamespace(
            error="Authorization: Bearer " + secret,
            result=None,
        )
    )
    app._set_replay_controls_busy = lambda _busy: None
    app._recovery_view = object()
    app.bank = _Value()
    app._bank_text = lambda: "bank"
    app._refresh_tickets = lambda: None
    app.status = _Value()
    logs: list[str] = []
    app._append_log = logs.append

    with patch("autosport.windows_gui.messagebox.showerror") as showerror:
        WindowsAutosportApp._poll_recovery_worker(app)

    assert app._recovery_view is None
    assert len(logs) == 1
    assert secret not in logs[0]
    assert REDACTED in logs[0]
    shown = showerror.call_args.args[1]
    assert secret not in shown
    assert REDACTED in shown


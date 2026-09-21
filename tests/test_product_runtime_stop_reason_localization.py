from __future__ import annotations

import pytest

from autosport.continuous_session import ContinuousSessionStatus, SessionState
from autosport.product_gui_worker import ProductGuiMessage
from autosport.product_windows_gui import ProductWindowsAutosportApp


class _TextSink:
    def __init__(self) -> None:
        self.values: list[str] = []

    def set(self, value: str) -> None:
        self.values.append(value)


def _stopped_status() -> ContinuousSessionStatus:
    return ContinuousSessionStatus(
        session_id="session-1",
        source_id="source-1",
        state=SessionState.STOPPED,
        cycles_completed=1,
        last_success_at="2026-09-21T20:40:00+00:00",
        last_error_code=None,
        last_full_refresh_at=None,
        settlement_evidence=(),
    )


@pytest.mark.parametrize("internal_reason", ("operator_stop", "app_close"))
def test_packaged_runtime_stopped_status_does_not_expose_internal_stop_reason(
    internal_reason: str,
) -> None:
    app = object.__new__(ProductWindowsAutosportApp)
    app._product_last_stop = None
    app.product_status = _TextSink()
    app.status = _TextSink()
    log: list[str] = []
    app._append_log = log.append

    message = ProductGuiMessage(
        kind="STOPPED",
        status=_stopped_status(),
        stop_reason=internal_reason,
    )

    ProductWindowsAutosportApp._apply_product_message(app, message)

    rendered = app.product_status.values[-1]
    assert internal_reason not in rendered
    assert app.status.values[-1] == rendered
    assert log[-1] == rendered

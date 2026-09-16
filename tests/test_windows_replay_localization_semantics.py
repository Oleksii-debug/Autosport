from __future__ import annotations

from types import SimpleNamespace

from autosport.localization import text
from autosport.windows_gui import WindowsAutosportApp


def test_replay_worker_error_uses_replay_not_recovery_presentation(monkeypatch) -> None:
    raw_detail = "RuntimeError: provider_raw_detail=ABC-123"
    logs: list[str] = []
    dialogs: list[tuple[str, str]] = []
    evaluations: list[list[str]] = []
    statuses: list[str] = []
    bank_values: list[str] = []

    app = SimpleNamespace(
        replay_worker=SimpleNamespace(
            poll=lambda: SimpleNamespace(error=raw_detail, result=None),
        ),
        _active_workspace="workspace-raw",
        _set_replay_controls_busy=lambda _busy: None,
        _block_workspace_for_recovery=lambda _workspace: None,
        _recovery_view=object(),
        session=object(),
        bank=SimpleNamespace(set=bank_values.append),
        _bank_text=lambda: "bank-quarantined",
        _refresh_tickets=lambda: None,
        _append_log=logs.append,
        _set_evaluation_lines=evaluations.append,
        status=SimpleNamespace(set=statuses.append),
    )
    monkeypatch.setattr(
        "autosport.windows_gui.messagebox.showerror",
        lambda title, body: dialogs.append((title, body)),
    )

    WindowsAutosportApp._poll_replay_worker(app)

    expected = text("ui.error.replay.worker", detail=raw_detail)
    assert expected.startswith("Помилка паперового повтору:")
    assert "Відновлення робочої області" not in expected
    assert raw_detail in expected
    assert logs == [expected]
    assert dialogs == [(text("ui.dialog.title"), expected)]
    assert evaluations == [[text("ui.evaluation.replay_failed")]]
    assert statuses == [text("ui.status.replay.failed_recovery")]
    assert bank_values == ["bank-quarantined"]
    assert app._recovery_view is None
    assert app.session is None

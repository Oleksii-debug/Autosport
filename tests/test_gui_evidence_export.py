from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import autosport.gui as gui
import autosport.gui_evidence_export as gui_export
from autosport.gui import AutosportApp
from autosport.gui_evidence_export import (
    OneShotEvidenceExportWorker,
    resolve_evidence_output_destination,
)
from autosport.localization import text


def _join_worker(worker: OneShotEvidenceExportWorker) -> None:
    thread = worker._thread
    assert thread is not None
    thread.join(timeout=2)
    assert not thread.is_alive()


def _partial_app(*, recovery_busy: bool = False) -> AutosportApp:
    app = object.__new__(AutosportApp)
    app.__dict__.update(
        dataset_worker=SimpleNamespace(busy=False),
        replay_worker=SimpleNamespace(busy=False),
        live_worker=SimpleNamespace(busy=False),
        recovery_worker=SimpleNamespace(busy=recovery_busy),
        evidence_export_worker=SimpleNamespace(busy=False),
    )
    return app


def test_export_worker_calls_canonical_exporter_and_reports_output(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[tuple[Path, Path]] = []

    def fake_export(workspace, output):
        calls.append((Path(workspace), Path(output)))
        return {"schema": "test"}

    monkeypatch.setattr(gui_export, "export_evidence_manifest", fake_export)
    worker = OneShotEvidenceExportWorker()
    workspace = tmp_path / "workspace"
    output = tmp_path / "evidence.json"

    assert worker.start(workspace, output) is True
    _join_worker(worker)

    message = worker.poll()
    assert message is not None
    assert message.output == output
    assert message.error is None
    assert calls == [(workspace, output)]
    assert worker.busy is False


def test_export_worker_reports_structural_failure_without_secret_or_human_text(
    monkeypatch, tmp_path: Path
) -> None:
    secret = r"token=SUPERSECRET C:\Users\name\credentials.json"

    def fail_export(workspace, output):
        raise RuntimeError(secret)

    monkeypatch.setattr(gui_export, "export_evidence_manifest", fail_export)
    worker = OneShotEvidenceExportWorker()

    assert worker.start(tmp_path, tmp_path / "evidence.json") is True
    _join_worker(worker)

    message = worker.poll()
    assert message is not None
    assert message.output is None
    assert message.error == "RuntimeError"
    for forbidden in (
        "SUPERSECRET",
        "credentials.json",
        "token=",
        r"C:\Users",
        "evidence export failed",
    ):
        assert forbidden not in message.error
    assert worker.busy is False


def test_export_worker_rejects_overlap_until_terminal_message_is_polled(
    monkeypatch, tmp_path: Path
) -> None:
    entered = threading.Event()
    release = threading.Event()

    def blocked_export(workspace, output):
        entered.set()
        assert release.wait(timeout=2)
        return {"schema": "test"}

    monkeypatch.setattr(gui_export, "export_evidence_manifest", blocked_export)
    worker = OneShotEvidenceExportWorker()

    assert worker.start(tmp_path, tmp_path / "first.json") is True
    assert entered.wait(timeout=2)
    assert worker.start(tmp_path, tmp_path / "second.json") is False
    release.set()
    _join_worker(worker)

    message = worker.poll()
    assert message is not None
    assert message.output == tmp_path / "first.json"
    assert worker.busy is False


def test_destination_preflight_accepts_safe_ancestor_destination(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = tmp_path / "evidence.json"

    assert resolve_evidence_output_destination(workspace, output) == output.resolve()


def test_destination_preflight_rejects_workspace_and_reparentable_sibling(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    sibling = tmp_path / "sibling"
    sibling.mkdir()

    for output in (
        workspace / "forbidden.json",
        sibling / "reparentable.json",
    ):
        with pytest.raises(ValueError):
            resolve_evidence_output_destination(workspace, output)


def test_destination_preflight_does_not_depend_on_private_exporter_helper(
    monkeypatch, tmp_path: Path
) -> None:
    import autosport.evidence_export as evidence_export

    def private_helper_must_not_run(*_args, **_kwargs):
        raise AssertionError("GUI preflight called private canonical exporter helper")

    monkeypatch.setattr(
        evidence_export,
        "_resolve_output_destination",
        private_helper_must_not_run,
    )
    assert "_resolve_output_destination" not in gui_export.__dict__

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = tmp_path / "evidence.json"
    assert resolve_evidence_output_destination(workspace, output) == output.resolve()


def test_partial_gui_instance_without_export_worker_is_not_routed_to_tk_getattr() -> None:
    app = _partial_app()
    del app.__dict__["evidence_export_worker"]

    assert app._evidence_export_busy is False
    assert app._dataset_selection_blocker() is None


def test_export_evidence_uses_active_workspace_without_tk_getattr_fallback(
    monkeypatch, tmp_path: Path
) -> None:
    app = _partial_app()
    active_workspace = tmp_path / "active"
    chooser_kwargs: dict[str, object] = {}
    app.__dict__.update(
        _closing=False,
        _active_workspace=active_workspace,
    )
    assert "workspace" not in app.__dict__

    def fake_chooser(**kwargs):
        chooser_kwargs.update(kwargs)
        return ""

    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", fake_chooser)

    app.export_evidence()

    assert chooser_kwargs["initialdir"] == str(active_workspace.parent)


def test_control_e_path_is_blocked_while_recovery_worker_is_busy(
    monkeypatch, tmp_path: Path
) -> None:
    app = _partial_app(recovery_busy=True)
    status_values: list[str] = []
    log_values: list[str] = []
    app.__dict__.update(
        _closing=False,
        workspace=tmp_path / "workspace",
        _active_workspace=tmp_path / "workspace",
        status=SimpleNamespace(set=status_values.append),
        _append_log=log_values.append,
    )

    def chooser_must_not_open(**kwargs):
        raise AssertionError("chooser opened while recovery owns the workspace")

    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", chooser_must_not_open)

    app.export_evidence()

    expected = text("ui.status.evidence_export.recovery_busy")
    assert status_values == [expected]
    assert log_values == [expected]


def test_unsupported_destination_is_rejected_before_worker_start(
    monkeypatch, tmp_path: Path
) -> None:
    app = _partial_app()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    status_values: list[str] = []
    log_values: list[str] = []
    start_calls: list[tuple[Path, Path]] = []
    app.__dict__.update(
        _closing=False,
        workspace=workspace,
        _active_workspace=workspace,
        status=SimpleNamespace(set=status_values.append),
        _append_log=log_values.append,
    )
    app.evidence_export_worker.start = lambda workspace, output: start_calls.append(
        (Path(workspace), Path(output))
    ) or True

    monkeypatch.setattr(
        gui.filedialog,
        "asksaveasfilename",
        lambda **kwargs: str(workspace / "forbidden.json"),
    )
    monkeypatch.setattr(
        gui,
        "resolve_evidence_output_destination",
        lambda workspace, output: (_ for _ in ()).throw(ValueError("blocked")),
    )
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *args, **kwargs: None)

    app.export_evidence()

    expected = text("ui.status.evidence_export.destination_invalid")
    assert status_values == [expected]
    assert log_values == [expected]
    assert start_calls == []


def test_completed_export_does_not_echo_selected_filename_to_status_or_log(
    tmp_path: Path,
) -> None:
    app = _partial_app()
    sensitive_output = tmp_path / "token=SUPERSECRET-credentials.json"
    status_values: list[str] = []
    log_values: list[str] = []
    app.__dict__.update(
        _closing=False,
        status=SimpleNamespace(set=status_values.append),
        _append_log=log_values.append,
        _set_replay_controls_busy=lambda _busy: None,
        evidence_export_worker=SimpleNamespace(
            poll=lambda: SimpleNamespace(error=None, output=sensitive_output)
        ),
    )

    app._poll_evidence_export_worker()

    expected = text("ui.status.evidence_export.complete")
    assert status_values == [expected]
    assert log_values == [expected]
    for forbidden in ("SUPERSECRET", "credentials.json", "token="):
        assert forbidden not in expected

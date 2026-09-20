from __future__ import annotations

import threading
from pathlib import Path

import autosport.gui_evidence_export as gui_export
from autosport.gui_evidence_export import OneShotEvidenceExportWorker


def _join_worker(worker: OneShotEvidenceExportWorker) -> None:
    thread = worker._thread
    assert thread is not None
    thread.join(timeout=2)
    assert not thread.is_alive()


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


def test_export_worker_reports_safe_failure(monkeypatch, tmp_path: Path) -> None:
    def fail_export(workspace, output):
        raise FileExistsError("destination already exists")

    monkeypatch.setattr(gui_export, "export_evidence_manifest", fail_export)
    worker = OneShotEvidenceExportWorker()

    assert worker.start(tmp_path, tmp_path / "evidence.json") is True
    _join_worker(worker)

    message = worker.poll()
    assert message is not None
    assert message.output is None
    assert message.error == "FileExistsError: destination already exists"
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

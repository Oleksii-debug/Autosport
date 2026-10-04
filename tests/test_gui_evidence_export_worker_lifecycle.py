from __future__ import annotations

import threading
from pathlib import Path

from autosport import gui_evidence_export


def test_committed_evidence_export_worker_is_non_daemon(
    monkeypatch,
    tmp_path: Path,
) -> None:
    entered = threading.Event()
    release = threading.Event()
    calls: list[tuple[Path, Path]] = []

    def fake_export(workspace: Path, output: Path) -> None:
        calls.append((Path(workspace), Path(output)))
        entered.set()
        if not release.wait(5):
            raise RuntimeError("test export was not released")

    monkeypatch.setattr(
        gui_evidence_export,
        "export_evidence_manifest",
        fake_export,
    )

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    output = tmp_path / "evidence.json"
    worker = gui_evidence_export.OneShotEvidenceExportWorker()

    assert worker.start(workspace, output) is True
    assert entered.wait(5)

    thread = worker._thread
    assert thread is not None
    assert thread.daemon is False
    assert thread.is_alive()

    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()

    message = worker.poll()
    assert message == gui_evidence_export.EvidenceExportMessage(output=output)
    assert calls == [(workspace, output)]
    assert worker.busy is False

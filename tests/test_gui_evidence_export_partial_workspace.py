from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import autosport.gui as gui
from autosport.gui import AutosportApp


def _partial_export_app(active_workspace: Path) -> AutosportApp:
    app = object.__new__(AutosportApp)
    app.__dict__.update(
        _closing=False,
        _active_workspace=active_workspace,
        dataset_worker=SimpleNamespace(busy=False),
        replay_worker=SimpleNamespace(busy=False),
        live_worker=SimpleNamespace(busy=False),
        recovery_worker=SimpleNamespace(busy=False),
        evidence_export_worker=SimpleNamespace(busy=False),
        status=SimpleNamespace(set=lambda _value: None),
        _append_log=lambda _value: None,
    )
    return app


def test_export_chooser_uses_active_workspace_without_eager_tk_workspace_lookup(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    app = _partial_export_app(workspace)
    chooser_calls: list[dict[str, object]] = []

    def choose(**kwargs):
        chooser_calls.append(kwargs)
        return ""

    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", choose)

    app.export_evidence()

    assert chooser_calls == [
        {
            "title": gui.text("ui.dialog.evidence_export.choose_title"),
            "initialdir": str(workspace.parent),
            "initialfile": "autosport-evidence.json",
            "defaultextension": ".json",
            "filetypes": (
                (gui.text("ui.filetype.json"), "*.json"),
                (gui.text("ui.filetype.all"), "*.*"),
            ),
        }
    ]

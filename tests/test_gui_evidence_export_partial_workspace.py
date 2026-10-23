from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import autosport.gui as gui
from autosport.gui import AutosportApp


def _partial_export_app(
    workspace: Path,
    *,
    active_workspace: Path | None = None,
) -> AutosportApp:
    app = object.__new__(AutosportApp)
    app.__dict__.update(
        _closing=False,
        workspace=workspace,
        _active_workspace=active_workspace or workspace,
        dataset_worker=SimpleNamespace(busy=False),
        replay_worker=SimpleNamespace(busy=False),
        live_worker=SimpleNamespace(busy=False),
        recovery_worker=SimpleNamespace(busy=False),
        evidence_export_worker=SimpleNamespace(busy=False),
        status=SimpleNamespace(set=lambda _value: None),
        _append_log=lambda _value: None,
    )
    app._selected_replay_configuration = lambda: ("baseline-v1", None)
    return app


def test_export_chooser_uses_current_selected_workspace_not_stale_active_workspace(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    stale_workspace = tmp_path / "stale-workspace"
    app = _partial_export_app(workspace, active_workspace=stale_workspace)
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


def test_post_dialog_missing_current_workspace_fails_closed_before_export(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    output = tmp_path / "evidence.json"
    app = _partial_export_app(workspace)
    destination_calls: list[tuple[Path, Path]] = []
    start_calls: list[tuple[Path, Path]] = []

    def choose(**_kwargs):
        del app.__dict__["workspace"]
        return str(output)

    def resolve(workspace_arg, output_arg):
        destination_calls.append((Path(workspace_arg), Path(output_arg)))
        return Path(output_arg)

    app.evidence_export_worker.start = lambda workspace_arg, output_arg: start_calls.append(
        (Path(workspace_arg), Path(output_arg))
    ) or True
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", choose)
    monkeypatch.setattr(gui, "resolve_evidence_output_destination", resolve)

    app.export_evidence()

    assert destination_calls == []
    assert start_calls == []


def test_export_stops_if_app_closes_while_save_dialog_is_open(
    monkeypatch, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    output = tmp_path / "evidence.json"
    app = _partial_export_app(workspace)
    destination_calls: list[tuple[Path, Path]] = []
    start_calls: list[tuple[Path, Path]] = []

    def choose(**_kwargs):
        app.__dict__["_closing"] = True
        return str(output)

    def resolve(workspace_arg, output_arg):
        destination_calls.append((Path(workspace_arg), Path(output_arg)))
        return Path(output_arg)

    app.evidence_export_worker.start = lambda workspace_arg, output_arg: start_calls.append(
        (Path(workspace_arg), Path(output_arg))
    ) or True
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", choose)
    monkeypatch.setattr(gui, "resolve_evidence_output_destination", resolve)

    app.export_evidence()

    assert destination_calls == []
    assert start_calls == []

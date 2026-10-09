from __future__ import annotations

from pathlib import Path

import autosport.windows_webview_shell as shell
from autosport.windows_webview_shell import AutosportWebController


class _IdleWorker:
    busy = False


class _CapturingEvidenceExportWorker:
    def __init__(self) -> None:
        self.busy = False
        self.start_calls: list[tuple[Path, Path]] = []

    def start(self, workspace: str | Path, destination: str | Path) -> bool:
        self.start_calls.append((Path(workspace), Path(destination)))
        self.busy = True
        return True


def _controller(
    base_workspace: Path,
    stale_active_workspace: Path,
) -> tuple[AutosportWebController, _CapturingEvidenceExportWorker]:
    controller = AutosportWebController.__new__(AutosportWebController)
    controller.workspace = base_workspace
    controller._active_workspace = stale_active_workspace
    controller.dataset_worker = _IdleWorker()
    controller.replay_worker = _IdleWorker()
    controller.live_worker = _IdleWorker()
    controller.recovery_worker = _IdleWorker()
    worker = _CapturingEvidenceExportWorker()
    controller.evidence_export_worker = worker
    controller.product_worker = _IdleWorker()
    controller.status = ""
    controller.last_error = ""
    controller.log = []
    return controller, worker


def _bind_current_strategy_workspace(
    controller: AutosportWebController,
    monkeypatch,
    current_workspace: Path,
) -> None:
    plan = object()
    monkeypatch.setattr(
        controller,
        "_selected_configuration",
        lambda: ("research-v1", plan),
    )

    def resolve_workspace(
        root: Path,
        strategy_id: str,
        selected_plan: object,
    ) -> Path:
        assert Path(root) == controller.workspace
        assert strategy_id == "research-v1"
        assert selected_plan is plan
        return current_workspace

    monkeypatch.setattr(shell, "workspace_for_strategy", resolve_workspace)


def test_evidence_export_uses_current_selected_strategy_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_workspace = tmp_path / "workspace"
    stale_workspace = tmp_path / "workspace-baseline"
    current_workspace = tmp_path / "workspace-research"
    destination = tmp_path / "exports" / "evidence.json"
    controller, worker = _controller(base_workspace, stale_workspace)
    _bind_current_strategy_workspace(controller, monkeypatch, current_workspace)

    resolved_workspaces: list[Path] = []

    def resolve_destination(workspace: Path, requested: Path) -> Path:
        resolved_workspaces.append(Path(workspace))
        assert requested == destination.absolute()
        return destination

    monkeypatch.setattr(
        shell,
        "resolve_evidence_output_destination",
        resolve_destination,
    )

    result = controller._action_evidence_export({"path": str(destination)})

    assert result["status"] == "completed"
    assert resolved_workspaces == [current_workspace]
    assert worker.start_calls == [(current_workspace, destination)]


def test_evidence_export_fails_closed_when_current_strategy_cannot_resolve(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_workspace = tmp_path / "workspace"
    stale_workspace = tmp_path / "workspace-old-strategy"
    destination = tmp_path / "exports" / "evidence.json"
    controller, worker = _controller(base_workspace, stale_workspace)

    def invalid_configuration():
        raise ValueError("invalid current strategy configuration")

    monkeypatch.setattr(
        controller,
        "_selected_configuration",
        invalid_configuration,
    )
    monkeypatch.setattr(
        shell,
        "resolve_evidence_output_destination",
        lambda _workspace, _requested: destination,
    )

    result = controller._action_evidence_export({"path": str(destination)})

    assert result["status"] == "rejected"
    assert worker.start_calls == []

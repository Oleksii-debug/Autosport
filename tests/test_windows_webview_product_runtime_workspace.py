from __future__ import annotations

import inspect
import threading
from pathlib import Path

import autosport.windows_webview_shell as shell
from autosport.operator_source_store import OperatorSourceConfigStore
from autosport.windows_webview_shell import AutosportWebController


class _IdleWorker:
    @property
    def busy(self) -> bool:
        return False


class _CapturingProductWorker:
    def __init__(self) -> None:
        self.busy = False
        self.start_calls: list[dict[str, object]] = []

    def start(
        self,
        *,
        workspace: str | Path,
        source_factory: str,
        expected_source_id: str,
        initial_bankroll: str = "10000",
        poll_seconds: float = 30.0,
    ) -> bool:
        self.start_calls.append(
            {
                "workspace": Path(workspace),
                "source_factory": source_factory,
                "expected_source_id": expected_source_id,
                "initial_bankroll": initial_bankroll,
                "poll_seconds": poll_seconds,
            }
        )
        self.busy = True
        return True


def _controller(
    base_workspace: Path,
) -> tuple[AutosportWebController, _CapturingProductWorker]:
    controller = AutosportWebController.__new__(AutosportWebController)
    controller._lock = threading.RLock()
    controller.workspace = base_workspace
    controller._active_workspace = base_workspace
    controller._recovery_required_workspaces = set()
    controller.dataset_worker = _IdleWorker()
    controller.replay_worker = _IdleWorker()
    controller.live_worker = _IdleWorker()
    controller.recovery_worker = _IdleWorker()
    controller.evidence_export_worker = _IdleWorker()
    worker = _CapturingProductWorker()
    controller.product_worker = worker
    controller.product_runtime_status = "Тривала PAPER-робота не запущена."
    controller.status = ""
    controller.last_error = ""
    controller.log = []
    return controller, worker


def _configure_canonical_source(controller: AutosportWebController) -> None:
    OperatorSourceConfigStore(
        controller.workspace / "operator-source.json"
    ).write_source_id("parlayapi-table-tennis")


def _bind_strategy_workspace(
    controller: AutosportWebController,
    monkeypatch,
    strategy_workspace: Path,
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
        return strategy_workspace

    monkeypatch.setattr(shell, "workspace_for_strategy", resolve_workspace)


def test_product_runtime_start_uses_selected_strategy_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_workspace = tmp_path / "workspace"
    strategy_workspace = tmp_path / "workspace-research"
    controller, worker = _controller(base_workspace)
    _bind_strategy_workspace(controller, monkeypatch, strategy_workspace)
    _configure_canonical_source(controller)

    result = controller._action_product_runtime_start({})

    assert result["status"] == "completed"
    assert worker.start_calls == [
        {
            "workspace": strategy_workspace,
            "source_factory": "autosport.product_source:create_parlay_product_source",
            "expected_source_id": "parlayapi:table_tennis",
            "initial_bankroll": "10000",
            "poll_seconds": 30.0,
        }
    ]
    assert controller._active_workspace == strategy_workspace


def test_product_runtime_start_cannot_bypass_strategy_workspace_recovery_quarantine(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_workspace = tmp_path / "workspace"
    strategy_workspace = tmp_path / "workspace-research"
    controller, worker = _controller(base_workspace)
    _bind_strategy_workspace(controller, monkeypatch, strategy_workspace)
    controller._recovery_required_workspaces.add(strategy_workspace)
    monkeypatch.setenv(
        "AUTOSPORT_PRODUCT_SOURCE_FACTORY",
        "provider.module:factory",
    )

    result = controller._action_product_runtime_start({})

    assert result["status"] == "rejected"
    assert worker.start_calls == []
    assert controller.product_worker.busy is False


def test_product_runtime_actionability_uses_selected_strategy_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    base_workspace = tmp_path / "workspace"
    strategy_workspace = tmp_path / "workspace-research"
    controller, _worker = _controller(base_workspace)
    _bind_strategy_workspace(controller, monkeypatch, strategy_workspace)
    _configure_canonical_source(controller)

    assert controller._product_runtime_can_start() is True

    controller._recovery_required_workspaces.add(strategy_workspace)

    assert controller._product_runtime_can_start() is False


def test_product_runtime_actionability_fails_closed_for_invalid_strategy_configuration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    controller, _worker = _controller(tmp_path / "workspace")

    def invalid_target() -> Path:
        raise ValueError("invalid selected strategy configuration")

    monkeypatch.setattr(
        controller,
        "_product_runtime_target_workspace",
        invalid_target,
    )

    assert controller._product_runtime_can_start() is False


def test_product_runtime_state_projects_canonical_start_actionability() -> None:
    source = inspect.getsource(AutosportWebController.state)

    assert '"can_start": self._product_runtime_can_start(' in source
    assert "source_ready=source_entry is not None" in source


def test_product_runtime_terminal_error_quarantines_active_workspace() -> None:
    source = inspect.getsource(AutosportWebController._poll_workers)
    error_start = source.index('elif product_message.kind == "ERROR":')
    error_block = source[error_start:]

    assert "self._recovery_required_workspaces.add(Path(self._active_workspace))" in error_block
    assert "self._recovery_required_workspaces.add(Path(self.workspace))" not in error_block

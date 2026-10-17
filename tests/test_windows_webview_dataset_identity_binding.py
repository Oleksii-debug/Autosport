from __future__ import annotations

import re
from pathlib import Path

import autosport.windows_webview_shell as shell_module
from autosport.windows_webview_shell import AutosportWebController


_ROOT = Path(__file__).resolve().parents[1]


class _ReplayWorkerProbe:
    busy = False

    def __init__(self) -> None:
        self.start_calls = 0

    def start(self, _task) -> bool:
        self.start_calls += 1
        return True


def _bare_controller(tmp_path: Path) -> AutosportWebController:
    controller = AutosportWebController.__new__(AutosportWebController)
    controller.workspace = tmp_path
    controller._active_workspace = tmp_path
    controller._recovery_required_workspaces = set()
    controller.dataset_path = tmp_path / "validated-a"
    controller.research_plan_path = None
    controller.replay_speed = 0.0
    controller.replay_worker = _ReplayWorkerProbe()
    controller.evaluation = []
    controller.log = []
    controller.last_error = ""
    controller.status = ""
    controller._busy = lambda: False
    controller._selected_configuration = lambda: ("baseline-v1", None)
    return controller


def test_replay_rejects_visible_dataset_identity_mismatch_before_worker_start(
    tmp_path: Path, monkeypatch
) -> None:
    controller = _bare_controller(tmp_path)
    monkeypatch.setattr(
        shell_module,
        "workspace_for_strategy",
        lambda *_args, **_kwargs: tmp_path,
    )

    visible_unvalidated = tmp_path / "typed-but-unvalidated-b"
    result = controller._action_replay_run(
        {
            "dataset_path": str(visible_unvalidated),
            "research_plan_path": "",
        }
    )

    assert result["status"] == "rejected"
    assert controller.replay_worker.start_calls == 0


def test_replay_accepts_exact_visible_validated_dataset_identity(
    tmp_path: Path, monkeypatch
) -> None:
    controller = _bare_controller(tmp_path)
    monkeypatch.setattr(
        shell_module,
        "workspace_for_strategy",
        lambda *_args, **_kwargs: tmp_path,
    )

    result = controller._action_replay_run(
        {
            "dataset_path": str(controller.dataset_path),
            "research_plan_path": "",
        }
    )

    assert result["status"] == "completed"
    assert controller.replay_worker.start_calls == 1


def test_replay_dispatch_carries_the_current_visible_dataset_path() -> None:
    javascript = (
        _ROOT / "src" / "autosport" / "windows_web" / "app.js"
    ).read_text(encoding="utf-8")

    assert re.search(
        r'dispatch\(\s*"replay\.run"\s*,\s*\{[^}]*'
        r'dataset_path\s*:\s*byId\(\s*"dataset-path"\s*\)\.value',
        javascript,
        flags=re.DOTALL,
    )

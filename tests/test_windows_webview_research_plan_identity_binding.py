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
    controller.dataset_path = tmp_path / "validated-dataset"
    controller.research_plan_path = tmp_path / "validated-plan-a.json"
    controller.replay_speed = 0.0
    controller.replay_worker = _ReplayWorkerProbe()
    controller.evaluation = []
    controller.log = []
    controller.last_error = ""
    controller.status = ""
    controller._busy = lambda: False
    controller._selected_configuration = lambda: ("research-replay-v1", object())
    return controller


def test_research_replay_rejects_visible_plan_identity_mismatch_before_worker_start(
    tmp_path: Path, monkeypatch
) -> None:
    controller = _bare_controller(tmp_path)
    monkeypatch.setattr(
        shell_module,
        "workspace_for_strategy",
        lambda *_args, **_kwargs: tmp_path,
    )

    visible_unbound = tmp_path / "typed-but-unbound-plan-b.json"
    result = controller._action_replay_run(
        {"research_plan_path": str(visible_unbound)}
    )

    assert result["status"] == "rejected"
    assert controller.replay_worker.start_calls == 0


def test_replay_dispatch_carries_the_current_visible_research_plan_path() -> None:
    javascript = (
        _ROOT / "src" / "autosport" / "windows_web" / "app.js"
    ).read_text(encoding="utf-8")

    assert re.search(
        r'dispatch\(\s*"replay\.run"\s*,\s*\{[^}]*'
        r'research_plan_path\s*:\s*byId\(\s*"research-plan-path"\s*\)\.value',
        javascript,
        flags=re.DOTALL,
    )

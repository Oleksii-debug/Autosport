"""Plan 6 §3А: agent learning readback reflects only verified canonical facts."""
from pathlib import Path
import time
from unittest.mock import patch

from autosport.gui_learning_status import OneShotLearningStatusWorker
from autosport.windows_webview_shell import AutosportWebController


def _result(worker):
    for _ in range(200):
        response = worker.poll()
        if response is not None:
            return response
        time.sleep(0.005)
    raise AssertionError("no terminal readback")


def test_absent_scientific_evidence_is_not_misreported_as_training(tmp_path):
    worker = OneShotLearningStatusWorker()
    with patch("autosport.gui_learning_status.verified_learning_projection", return_value={
        "science": {"state": "NOT_PRESENT"},
        "research_runs": {"state": "NOT_PRESENT"},
        "agent_loop": {"state": "NOT_PRESENT"},
        "proven_live_model_learning": False,
        "economic_improvement_proven_by_this_report": False,
        "has_live_execution_authority": False,
    }):
        assert worker.start(tmp_path)
        result = _result(worker)
    assert "0" in result.lines[1]
    assert "немає" in result.lines[4]
    assert worker.busy is False


def test_forged_economic_learning_success_is_refused(tmp_path):
    worker = OneShotLearningStatusWorker()
    with patch("autosport.gui_learning_status.verified_learning_projection", return_value={
        "science": {"state": "NOT_PRESENT"},
        "research_runs": {"state": "NOT_PRESENT"},
        "agent_loop": {"state": "NOT_PRESENT"},
        "proven_live_model_learning": True,
        "economic_improvement_proven_by_this_report": True,
        "has_live_execution_authority": True,
    }):
        assert worker.start(tmp_path)
        result = _result(worker)
    assert "не підтверджено" in result.lines[0]
    assert worker.busy is False


def test_unconfigured_strategy_does_not_leak_root_science(tmp_path):
    controller = AutosportWebController(tmp_path)
    try:
        controller.strategy_id = "research-replay-v1"
        rejected = controller.dispatch({
            "request_id": "no-plan-agent-status",
            "action_id": "agents.status",
            "payload": {},
        })
        assert rejected["status"] == "rejected"
        assert controller.state()["agent_learning_busy"] is False
    finally:
        controller.close()


def test_semantic_web_ui_routes_actual_agent_status():
    root = Path(__file__).resolve().parents[1] / "src" / "autosport" / "windows_web"
    html = (root / "index.html").read_text(encoding="utf-8")
    js = (root / "app.js").read_text(encoding="utf-8")
    assert html.count('id="agent-learning-refresh"') == 1
    assert 'id="agent-learning-status" role="status"' in html
    assert 'dispatch("agents.status")' in js
    assert 'state.agent_learning_evidence' in js

import threading
import time
from pathlib import Path

from autosport.gui_sandbox_learning import SandboxLearningWorker
from autosport.paper_live_sandbox_campaign import BlindPaperSandboxCampaign
from autosport.windows_webview_shell import AutosportWebController


def test_desktop_copy_discloses_new_session_and_simulated_outcomes():
    html = (Path(__file__).resolve().parents[1] / "src/autosport/windows_web/index.html").read_text(encoding="utf-8")
    assert "Кожен запуск починає нову симуляцію" in html
    assert "не відновлюється через ці кнопки" in html
    assert "не фактичні результати матчів" in html
    assert "не доводить прибутковості" in html


def test_desktop_simulation_is_single_flight_and_checkpoints_on_stop(tmp_path, monkeypatch):
    started = threading.Event()
    saved = []

    def runner(campaign, *, workspace, stop_event, **kwargs):
        started.set()
        assert stop_event.wait(2)
        campaign.save(workspace)
        saved.append(workspace)
        return {"network_snapshots_segment": 0, "network_failures_segment": 0,
                "initial_bankroll": "10000", "final_balance": "10000"}

    monkeypatch.setattr("autosport.gui_sandbox_learning.run_network_endurance", runner)
    worker = SandboxLearningWorker()
    assert worker.start(tmp_path)
    assert started.wait(2)
    assert not worker.start(tmp_path)
    worker.request_stop()
    for _ in range(200):
        result = worker.poll()
        if result is not None:
            break
        time.sleep(0.005)
    assert not worker.busy
    assert "віртуальні" in result and "не підтверджено" in result
    assert len(saved) == 1 and saved[0].parent == tmp_path / "sandbox-learning"
    assert BlindPaperSandboxCampaign.load(saved[0]).summary()["truth"] == "simulated"


def test_controller_close_requests_stop_and_waits_for_checkpoint(tmp_path, monkeypatch):
    started = threading.Event()
    saved = []

    def runner(campaign, *, workspace, stop_event, **kwargs):
        started.set()
        assert stop_event.wait(2)
        campaign.save(workspace)
        saved.append(workspace)
        return {"network_snapshots_segment": 0, "network_failures_segment": 0,
                "initial_bankroll": "10000", "final_balance": "10000"}

    monkeypatch.setattr("autosport.gui_sandbox_learning.run_network_endurance", runner)
    controller = AutosportWebController(tmp_path)
    result = controller.dispatch({"request_id": "sandbox-start", "action_id": "agents.sandbox_start", "payload": {}})
    assert result["status"] != "rejected"
    assert started.wait(2)
    controller.close()
    assert len(saved) == 1
    assert BlindPaperSandboxCampaign.load(saved[0]).summary()["truth"] == "simulated"

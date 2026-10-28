import threading
import time
from pathlib import Path

from autosport.gui_sandbox_learning import SandboxLearningWorker
from autosport.paper_live_sandbox_campaign import BlindPaperSandboxCampaign
from autosport.windows_webview_shell import AutosportWebController


def test_desktop_copy_discloses_new_session_and_simulated_outcomes():
    html = (Path(__file__).resolve().parents[1] / "src/autosport/windows_web/index.html").read_text(encoding="utf-8")
    assert "Новий запуск починає симуляцію з нуля" in html
    assert "відновити протягом 24 годин" in html
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
        summary = campaign.summary()
        summary.update(network_snapshots_segment=0, network_failures_segment=0)
        return summary

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
    assert "умовних дій: 0, очікувань: 0" in result
    assert len(saved) == 1 and saved[0].parent == tmp_path / "sandbox-learning"
    assert BlindPaperSandboxCampaign.load(saved[0]).summary()["truth"] == "simulated"


def test_resume_restores_the_same_policy_checkpoint(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from decimal import Decimal
    from autosport.paper_live_sandbox_campaign import SandboxLiveQuote, SandboxLiveSnapshot

    observed_generations = []
    directories = []

    def runner(campaign, *, workspace, **kwargs):
        observed_generations.append(campaign.policy.generation)
        directories.append(workspace)
        now = datetime.now(timezone.utc) + timedelta(seconds=2 * len(directories))
        def snapshot(instant, odds):
            return SandboxLiveSnapshot(
                observed_at=instant.isoformat(timespec="microseconds"),
                available_at=(instant + timedelta(microseconds=1)).isoformat(timespec="microseconds"),
                quotes=(SandboxLiveQuote("one", "h2h", "home", Decimal(odds),
                                         int(instant.timestamp() * 1000), 0),),
            )
        campaign.step(snapshot(now, "2.0"), snapshot(now + timedelta(seconds=1), "1.9"))
        campaign.save(workspace)
        summary = campaign.summary()
        summary.update(network_snapshots_segment=2, network_failures_segment=0)
        return summary

    monkeypatch.setattr("autosport.gui_sandbox_learning.run_network_endurance", runner)
    worker = SandboxLearningWorker()
    for resume in (False, True):
        assert worker.start(tmp_path, resume=resume)
        for _ in range(200):
            result = worker.poll()
            if result is not None:
                break
            time.sleep(0.005)
        assert result is not None and "не підтверджено" in result
    assert directories[0] == directories[1]
    assert observed_generations == [0, 1]
    assert BlindPaperSandboxCampaign.load(directories[0]).policy.generation == 2


def test_resume_rejects_a_missing_or_untrusted_session(tmp_path):
    root = tmp_path / "sandbox-learning"
    root.mkdir()
    (root / "latest-session.json").write_text('{"session":"../outside"}', encoding="utf-8")
    worker = SandboxLearningWorker()
    assert worker.start(tmp_path, resume=True)
    for _ in range(200):
        result = worker.poll()
        if result is not None:
            break
        time.sleep(0.005)
    assert "Не вдалося відновити" in result
    assert not (tmp_path / "outside").exists()


def test_two_workers_cannot_mutate_the_same_sandbox_root(tmp_path, monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    def runner(campaign, *, workspace, **kwargs):
        entered.set()
        assert release.wait(2)
        campaign.save(workspace)
        summary = campaign.summary()
        summary.update(network_snapshots_segment=0, network_failures_segment=0)
        return summary

    monkeypatch.setattr("autosport.gui_sandbox_learning.run_network_endurance", runner)
    first, second = SandboxLearningWorker(), SandboxLearningWorker()
    assert first.start(tmp_path) and entered.wait(2)
    try:
        assert second.start(tmp_path, resume=True)
        for _ in range(200):
            result = second.poll()
            if result is not None:
                break
            time.sleep(0.005)
        assert "Не вдалося відновити" in result
    finally:
        release.set()
        for _ in range(200):
            if first.poll() is not None:
                break
            time.sleep(0.005)
    assert not first.busy and not second.busy


def test_controller_close_requests_stop_and_waits_for_checkpoint(tmp_path, monkeypatch):
    started = threading.Event()
    saved = []

    def runner(campaign, *, workspace, stop_event, **kwargs):
        started.set()
        assert stop_event.wait(2)
        campaign.save(workspace)
        saved.append(workspace)
        summary = campaign.summary()
        summary.update(network_snapshots_segment=0, network_failures_segment=0)
        return summary

    monkeypatch.setattr("autosport.gui_sandbox_learning.run_network_endurance", runner)
    controller = AutosportWebController(tmp_path)
    result = controller.dispatch({"request_id": "sandbox-start", "action_id": "agents.sandbox_start", "payload": {}})
    assert result["status"] != "rejected"
    assert started.wait(2)
    controller.close()
    assert len(saved) == 1
    assert BlindPaperSandboxCampaign.load(saved[0]).summary()["truth"] == "simulated"

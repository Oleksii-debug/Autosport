import json
from decimal import Decimal

import pytest

from autosport.prospective_forecast_learning import forecast_learning_cycle, _fit, _predict
from autosport.shadow_strategy_memory import digest
from test_prospective_shadow_observer import forecasts, results, stamp


def run(root, hours=0, result="", settled="0", data=None):
    return forecast_learning_cycle(root, predictions=data or forecasts(hours),
                                   results=results(result, settled), received_at=stamp(hours))


def state(root):
    return json.loads((root / "forecast-learning.json").read_text())["state"]


def test_no_value_or_actionable_flag_required_to_learn_declined_match(tmp_path):
    data = json.loads(forecasts())
    data["predictions"][0].update(value=None, status="expired")
    assert run(tmp_path, data=json.dumps(data).encode())["forecast_events"] == 1
    assert run(tmp_path, 4, "2", "1")["results_learned"] == 1
    event = state(tmp_path)["events"]["1"]
    assert event["training_events"] == 0
    assert event["baseline"] == event["challenger"]
    assert event["scores"]["baseline"] == pytest.approx((0.5**2 + 0.3**2 + 0.8**2)/3)
    assert run(tmp_path, 5, "2", "1")["results_learned"] == 1


def test_correction_is_quarantined_and_never_rewrites_original(tmp_path):
    run(tmp_path)
    run(tmp_path, 4, "2", "1")
    report = run(tmp_path, 5, "1", "1")
    assert report["source_conflicts"] == 1
    assert report["results_learned"] == 0
    assert state(tmp_path)["events"]["1"]["result"] == "2"


def test_future_late_known_and_invalid_probabilities_do_not_train(tmp_path):
    assert run(tmp_path, result="1", settled="1")["forecast_events"] == 0
    assert run(tmp_path, 3)["forecast_events"] == 0
    other = tmp_path / "other"
    data = json.loads(forecasts())
    data["predictions"][0]["fair_probabilities"]["home"] = .9
    assert run(other, data=json.dumps(data).encode())["forecast_events"] == 0
    with pytest.raises(ValueError, match="future"):
        run(tmp_path / "future", data=forecasts(1))


def test_predictions_frozen_before_same_cycle_feedback(tmp_path):
    run(tmp_path)
    data = json.loads(forecasts(4))
    data["predictions"][0].update(id=2, kickoff_utc=stamp(6))
    run(tmp_path, 4, "1", "1", data=json.dumps(data).encode())
    before = state(tmp_path)["events"]["2"]
    assert before["training_events"] == 0
    data = json.loads(forecasts(5))
    data["predictions"][0].update(id=3, kickoff_utc=stamp(7))
    run(tmp_path, 5, "1", "1", data=json.dumps(data).encode())
    assert state(tmp_path)["events"]["3"]["training_events"] == 1
    assert state(tmp_path)["events"]["2"] == before


def test_calibration_requires_support_and_moves_after_actual_feedback():
    p = {"home": Decimal(".5"), "draw": Decimal(".3"), "away": Decimal(".2")}
    event = {"baseline": {key: str(value) for key, value in p.items()}, "result": "2"}
    events = {str(i): event for i in range(99)}
    assert _predict(p, _fit(events)) == event["baseline"]
    events["99"] = event
    learned = _predict(p, _fit(events))
    assert Decimal(learned["away"]) > p["away"]
    assert Decimal(learned["home"]) < p["home"]
    assert sum(map(Decimal, learned.values())) == 1


def test_receipt_chain_checkpoint_and_failed_write_recovery(tmp_path, monkeypatch):
    import autosport.prospective_forecast_learning as module
    run(tmp_path)
    write = module.atomic_write_json
    def fail(*args):
        raise OSError("interrupted checkpoint")
    monkeypatch.setattr(module, "atomic_write_json", fail)
    with pytest.raises(OSError):
        run(tmp_path, 4, "1", "1")
    monkeypatch.setattr(module, "atomic_write_json", write)
    assert run(tmp_path, 5, "1", "1")["results_learned"] == 1
    s = state(tmp_path)
    assert s["events"]["1"]["result_received_at"] == stamp(5)
    assert len(s["receipts"]) == 2
    previous = None
    for receipt in s["receipts"]:
        assert receipt["previous_sha256"] == previous
        assert digest({k: v for k, v in receipt.items() if k != "sha256"}) == receipt["sha256"]
        previous = receipt["sha256"]
    path = tmp_path / "forecast-learning.json"
    wrapper = json.loads(path.read_text())
    wrapper["state"]["events"]["1"]["result"] = "2"
    path.write_text(json.dumps(wrapper))
    with pytest.raises(ValueError, match="checkpoint"):
        run(tmp_path, 6)

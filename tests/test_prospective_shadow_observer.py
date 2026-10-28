import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io
import json

import pytest

from autosport.prospective_shadow_observer import ObserverPlan, observe_cycle
from autosport.shadow_strategy_memory import ShadowStrategyMemory


START = datetime(2026, 10, 27, 12, tzinfo=timezone.utc)


def stamp(hours=0):
    return (START + timedelta(hours=hours)).isoformat()


def forecasts(hours=0, odds=2.2):
    return json.dumps({"generated_at": stamp(hours), "predictions": [{
        "id": 1, "home": "Home", "away": "Away", "league": "League", "kickoff_utc": stamp(2),
        "status": "actionable", "fair_source": "live", "benchmark_read_at": stamp(hours),
        "fair_read_at": stamp(hours), "fair_probabilities": {"home": 0.5, "draw": 0.3, "away": 0.2},
        "value": {"selection": "1", "bookmaker": "book", "best_soft_odds": odds}}]}).encode()


def results(result="", settled="0", **changes):
    stream = io.StringIO()
    row = {"id": "1", "home": "Home", "away": "Away", "league": "League", "kickoff_utc": stamp(2),
           "result": result, "settled": settled, **changes}
    writer = csv.DictWriter(stream, fieldnames=row)
    writer.writeheader()
    writer.writerow(row)
    return stream.getvalue().encode()


def cycle(root, hours=0, result="", settled="0", **kwargs):
    return observe_cycle(root, predictions=forecasts(hours), results=results(result, settled),
                         received_at=stamp(hours), **kwargs)


def test_observed_pre_match_price_then_real_result_trains_once(tmp_path):
    assert cycle(tmp_path)["candidates"] == 1
    assert cycle(tmp_path, 1)["candidates"] == 1
    assert cycle(tmp_path, 3, "1", "1")["results_recorded"] == 0
    report = cycle(tmp_path, 4, "1", "1")
    assert report["results_recorded"] == 1
    assert report["profit_earned"] is None
    assert cycle(tmp_path, 5, "1", "1")["results_recorded"] == 1
    m = ShadowStrategyMemory.load(tmp_path / "memory.json")
    records = m.payload()["records"]
    assert len(records) == 1
    assert Decimal(records[0]["net_returns"]["PRICE_GAP"]) == Decimal("1.19")
    state = json.loads((tmp_path / "observer.json").read_text())["state"]
    assert state["candidates"]["1"]["decision_at"] == stamp(0)
    assert state["candidates"]["1"]["result_first_received_at"] == stamp(4)


def test_known_result_at_first_capture_never_becomes_pre_result_candidate(tmp_path):
    assert cycle(tmp_path, 0, "1", "1")["candidates"] == 0


def test_altered_match_or_result_is_held_not_retrained(tmp_path):
    cycle(tmp_path)
    cycle(tmp_path, 4, "1", "1")
    report = cycle(tmp_path, 5, "2", "1")
    assert report["source_conflicts"] == 1
    assert len(ShadowStrategyMemory.load(tmp_path / "memory.json").payload()["records"]) == 1


def test_crash_after_memory_save_before_observer_publish_retries_without_double_learning(tmp_path, monkeypatch):
    import autosport.prospective_shadow_observer as module
    cycle(tmp_path)
    write = module.atomic_write_json
    def fail_state(path, data):
        if path.name == "observer.json":
            raise OSError("simulated loss before observer state publication")
        return write(path, data)
    monkeypatch.setattr(module, "atomic_write_json", fail_state)
    with pytest.raises(OSError):
        cycle(tmp_path, 4, "1", "1")
    monkeypatch.setattr(module, "atomic_write_json", write)
    assert cycle(tmp_path, 5, "1", "1")["results_recorded"] == 1
    records = ShadowStrategyMemory.load(tmp_path / "memory.json").payload()["records"]
    assert len(records) == 1
    assert records[0]["result_available_at"] == stamp(4)


def test_protocol_change_or_missing_memory_cannot_silently_reset(tmp_path):
    cycle(tmp_path)
    with pytest.raises(ValueError, match="protocol"):
        cycle(tmp_path, 1, plan=ObserverPlan(daily_cap=6))
    (tmp_path / "memory.json").unlink()
    with pytest.raises(ValueError, match="missing"):
        cycle(tmp_path, 1)


def test_ambiguous_empty_result_stays_unresolved(tmp_path):
    cycle(tmp_path)
    assert cycle(tmp_path, 4, "", "1")["results_recorded"] == 0
    assert ShadowStrategyMemory.load(tmp_path / "memory.json").payload()["records"] == []


def test_quotes_captured_after_kickoff_are_not_admitted(tmp_path):
    assert cycle(tmp_path, 3)["candidates"] == 0

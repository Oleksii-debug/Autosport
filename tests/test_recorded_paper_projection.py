import json
import importlib.util
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io
from pathlib import Path

import pytest

from autosport.recorded_paper_projection import project, _fee_reserve_allows
from autosport.paper import PaperBook
from autosport.risk import PaperRiskPolicy
from autosport.prospective_shadow_observer import observe_cycle
from autosport.shadow_strategy_memory import ShadowStrategyMemory, digest
from test_prospective_shadow_observer import cycle


def test_wait_loss_trains_shadow_but_opens_no_paper_ticket(tmp_path):
    cycle(tmp_path)
    cycle(tmp_path, 4, "2", "1")
    result = project(tmp_path)
    assert result["opened_tickets"] == 0
    assert result["cost_stressed_net_units"] == "0.00"
    assert result["profit_earned"] is None
    assert len(ShadowStrategyMemory.load(tmp_path / "memory.json").payload()["records"]) == 1


@pytest.mark.parametrize("outcome,expected,wins,losses", [("1", "1.19", 1, 0), ("2", "-1.01", 0, 1)])
def test_assumed_fill_paper_uses_actual_outcome_and_fee_once(tmp_path, monkeypatch, outcome, expected, wins, losses):
    monkeypatch.setattr(ShadowStrategyMemory, "choose", lambda *args, **kwargs: "PRICE_GAP")
    cycle(tmp_path)
    before = project(tmp_path)
    assert before["opened_tickets"] == 1
    assert before["pending_tickets"] == 1
    assert Decimal(before["open_stake"]) == Decimal(1)
    assert Decimal(before["cost_stressed_net_units"]) == Decimal("-.01")
    cycle(tmp_path, 4, outcome, "1")
    first = project(tmp_path)
    assert first == project(tmp_path)
    assert Decimal(first["cost_stressed_net_units"]) == Decimal(expected)
    assert (first["won"], first["lost"], first["pending_tickets"]) == (wins, losses, 0)
    assert first["assumed_fill_not_accepted_quote"] is True
    assert first["real_money_execution"] is False


def test_changed_candidate_or_source_cannot_fabricate_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(ShadowStrategyMemory, "choose", lambda *args, **kwargs: "PRICE_GAP")
    cycle(tmp_path)
    cycle(tmp_path, 4, "2", "1")
    path = tmp_path / "observer.json"
    wrapper = json.loads(path.read_text())
    wrapper["state"]["candidates"]["1"]["observed_odds"] = "100"
    wrapper["sha256"] = digest(wrapper["state"])
    path.write_text(json.dumps(wrapper))
    with pytest.raises(ValueError, match="differs from recorded forecast"):
        project(tmp_path)


def test_corrected_result_holds_portfolio_instead_of_rewriting_history(tmp_path):
    cycle(tmp_path)
    cycle(tmp_path, 4, "1", "1")
    cycle(tmp_path, 5, "2", "1")
    with pytest.raises(ValueError, match="correction"):
        project(tmp_path)


def test_missing_or_changed_source_stops_projection(tmp_path):
    cycle(tmp_path)
    state = json.loads((tmp_path / "observer.json").read_text())["state"]
    source = tmp_path / "raw" / (state["candidates"]["1"]["forecast_sha256"] + ".json")
    source.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="digest"):
        project(tmp_path)


def test_forged_preference_is_not_treated_as_an_agent_decision(tmp_path):
    cycle(tmp_path)
    path = tmp_path / "observer.json"
    wrapper = json.loads(path.read_text())
    wrapper["state"]["candidates"]["1"]["memory_preference"] = "PRICE_GAP"
    wrapper["sha256"] = digest(wrapper["state"])
    path.write_text(json.dumps(wrapper))
    with pytest.raises(ValueError, match="prior feedback"):
        project(tmp_path)


def test_fee_stress_does_not_allow_a_paper_position_below_the_reserve():
    book = PaperBook("100")
    book.balance = Decimal("21.20")
    risk = PaperRiskPolicy()
    assert _fee_reserve_allows(book, risk, 19)
    assert not _fee_reserve_allows(book, risk, 20)


def test_projection_error_holds_display_without_stopping_observation(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("observer_cli", Path(__file__).parents[1] / "scripts/run_prospective_shadow_observer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    def fail(_):
        raise ValueError("bad source")
    monkeypatch.setattr(module, "project", fail)
    result = module.save_paper_projection(tmp_path)
    assert result["status"] == "HELD"
    assert result["profit_earned"] is None
    assert json.loads((tmp_path / "paper-projection.json").read_text()) == result


def test_full_learning_loop_uses_declined_outcomes_before_opening_a_paper_ticket(tmp_path):
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    def capture(index, after=False, outcome="1"):
        initial = start + timedelta(days=index)
        now = initial + timedelta(hours=4 if after else 0)
        kickoff = initial + timedelta(hours=2)
        item = {"id": index, "home": f"Home {index}", "away": f"Away {index}", "league": "League",
                "kickoff_utc": kickoff.isoformat(), "status": "actionable", "fair_source": "live",
                "benchmark_read_at": now.isoformat(), "fair_read_at": now.isoformat(),
                "fair_probabilities": {"home": 0.5, "draw": 0.3, "away": 0.2},
                "value": {"selection": "1", "bookmaker": "book", "best_soft_odds": 2.2}}
        forecast = json.dumps({"generated_at": now.isoformat(), "predictions": [item]}).encode()
        result = {"id": str(index), "home": item["home"], "away": item["away"], "league": "League",
                  "kickoff_utc": kickoff.isoformat(), "result": outcome if after else "", "settled": "1" if after else "0"}
        stream = io.StringIO()
        writer = csv.DictWriter(stream, fieldnames=result)
        writer.writeheader()
        writer.writerow(result)
        return observe_cycle(tmp_path, predictions=forecast, results=stream.getvalue().encode(), received_at=now.isoformat())

    for index in range(30):
        capture(index)
        capture(index, after=True)
    assert len(ShadowStrategyMemory.load(tmp_path / "memory.json").payload()["records"]) == 30
    assert project(tmp_path)["opened_tickets"] == 0
    capture(30)
    pending = project(tmp_path)
    assert pending["opened_tickets"] == pending["pending_tickets"] == 1
    capture(30, after=True, outcome="2")
    final = project(tmp_path)
    assert final["won"] == 0 and final["lost"] == 1
    assert Decimal(final["cost_stressed_net_units"]) == Decimal("-1.01")
    assert len(ShadowStrategyMemory.load(tmp_path / "memory.json").payload()["records"]) == 31

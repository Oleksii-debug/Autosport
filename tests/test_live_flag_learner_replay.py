from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from replay_live_flag_learner import replay


def dataset():
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    decisions = []
    for i in range(36):
        when = start + timedelta(days=i)
        decisions.append({"id": str(i), "current_rule_selected": True, "recorded_at": when.isoformat(),
                          "kickoff": (when+timedelta(hours=1)).isoformat(), "state": "SETTLED", "reward": "1.20"})
    return {"source_sha256": "a"*64, "decisions": decisions}


def test_waited_outcomes_learn_only_after_delay_and_current_result_does_not_leak():
    source = dataset()
    first = replay(source)
    assert first["decisions"][0]["choice"] == "WAIT"
    assert first["decisions"][32]["previous_outcomes"] == 30
    assert first["decisions"][32]["choice"] == "PRICE_GAP"
    source["decisions"][32]["reward"] = "-1"
    changed = replay(source)
    assert changed["decisions"][32]["choice"] == first["decisions"][32]["choice"]
    assert Decimal(changed["learner_selected"]["one_percent_cost"]["net_units"]) < Decimal(first["learner_selected"]["one_percent_cost"]["net_units"])
    assert first["profitability_claim"] is False


def test_unresolved_and_conflicting_results_do_not_train():
    source = dataset()
    source["decisions"][0].update(state="PENDING", reward=None)
    source["decisions"][1].update(state="CONFLICT", reward=None)
    result = replay(source)
    assert result["decisions"][32]["previous_outcomes"] == 28
    assert result["decisions"][32]["choice"] == "WAIT"

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from compare_account_c_recovery_families import compare  # noqa: E402


def test_exploration_reaches_both_families_and_retains_loss_tradeoff():
    report = compare()
    cases = {c["scenario"]: c["arms"] for c in report["cases"]}
    loss = cases["loss"]
    assert loss["baseline"]["ticket_count"] == 32
    assert loss["probe_37"]["ticket_count"] == 35
    estimates = loss["probe_37"]["estimates"]
    assert estimates["PARLAY_2_HIGH_ODDS"]["observations"] > 8
    assert estimates["PARLAY_2_LOW_ODDS"]["observations"] > 8
    assert estimates["SINGLE_HIGH_ODDS"]["observations"] > 8
    assert float(loss["probe_37"]["net_reward"]) < float(loss["baseline"]["net_reward"])
    recovery = cases["recovery"]
    assert float(recovery["probe_37"]["net_reward"]) > float(recovery["baseline"]["net_reward"])
    assert cases["favorable"]["baseline"] == cases["favorable"]["probe_37"]
    assert report["profitability_claim"] is False
    assert report["promotion_authorized"] is False

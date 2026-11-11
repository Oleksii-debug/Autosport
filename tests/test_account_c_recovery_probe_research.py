from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from compare_account_c_recovery_probe import compare  # noqa: E402


def test_probe_limits_losses_and_can_observe_recovery_in_synthetic_fixture():
    report = compare()
    cases = {case["scenario"]: case["results"] for case in report["cases"]}
    loss = cases["loss"]
    assert loss["baseline"]["ticket_count"] == 32
    assert loss["probe"]["ticket_count"] == 36  # four extra probes, not unlimited betting
    assert float(loss["probe"]["net_reward"]) < float(loss["baseline"]["net_reward"])
    recovery = cases["recovery"]
    assert recovery["probe"]["ticket_count"] > recovery["baseline"]["ticket_count"]
    assert float(recovery["probe"]["net_reward"]) > float(recovery["baseline"]["net_reward"])
    assert cases["favorable"]["baseline"] == cases["favorable"]["probe"]
    assert report["profitability_claim"] is False
    assert report["promotion_authorized"] is False

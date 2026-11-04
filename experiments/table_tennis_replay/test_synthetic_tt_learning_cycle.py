from decimal import Decimal

import pytest

from synthetic_tt_learning_cycle import run_demo


def test_synthetic_overlap_then_one_resolved_policy_update():
    trace = run_demo(first_outcome=0)
    one, two, three = trace["decisions"]
    assert [one["policy_generation"], two["policy_generation"], three["policy_generation"]] == [0, 0, 1]
    assert [one["action"], two["action"], three["action"]] == ["FORM_H2H", "FORM_H2H", "MARKET"]
    assert [one["prior_h2h"], two["prior_h2h"], three["prior_h2h"]] == [1, 1, 2]
    assert abs(Decimal(one["form_h2h_probability"]) - Decimal(59) / 84) < Decimal("1e-26")
    assert abs(Decimal(three["form_h2h_probability"]) - Decimal(59) / 98) < Decimal("1e-26")
    assert trace["prior_h2h_exactly_at_reveal"] == 1
    assert trace["policy_updates"] == 1 and trace["update_binds_first_action"]
    assert trace["early_resolution_rejected"] and trace["duplicate_update_rejected"]
    assert trace["learning_evidence_truth"] == "simulated"
    assert trace["fixture_only"]
    assert all(trace[key] is False for key in (
        "physical_people_verified", "physical_results_verified", "real_market_data_used",
        "independent_holdout", "calibration_verified", "portfolio_executed",
        "real_money_execution", "profitability_claim"))


def test_future_outcome_cannot_change_prior_decisions_but_changes_later_history():
    lost, won = run_demo(first_outcome=0), run_demo(first_outcome=1)
    assert lost["decisions"][:2] == won["decisions"][:2]
    assert lost["prior_h2h_exactly_at_reveal"] == won["prior_h2h_exactly_at_reveal"] == 1
    after_loss, after_win = lost["decisions"][2], won["decisions"][2]
    assert after_loss["prior_h2h"] == after_win["prior_h2h"] == 2
    assert Decimal(after_loss["form_h2h_probability"]) < Decimal(after_win["form_h2h_probability"])
    assert lost["first_reward_negative_brier"] != won["first_reward_negative_brier"]


def test_demo_is_deterministic():
    assert run_demo() == run_demo()


@pytest.mark.parametrize("bad", [True, False, -1, 2, "1", None])
def test_synthetic_outcome_is_exact_binary_integer(bad):
    with pytest.raises(ValueError, match="synthetic outcome"):
        run_demo(first_outcome=bad)

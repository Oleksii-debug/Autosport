from decimal import Decimal, localcontext

import pytest

from autosport.shadow_action_selection import ShadowSelectionConfig, choose_shadow_action


def config(**changes):
    return ShadowSelectionConfig(**{"window": 3, "minimum_observations": 3,
                                    "minimum_net_return": "0", "cost_bps_per_stake": 0,
                                    **changes})


def test_abstention_can_recover_without_fabricated_executed_rewards():
    executed_rewards = []
    hypothetical = {"strategy": [Decimal(-1)] * 3}
    assert choose_shadow_action(hypothetical, config()) == "WAIT"
    hypothetical["strategy"].extend([Decimal(1)] * 3)
    assert choose_shadow_action(hypothetical, config()) == "strategy"
    assert executed_rewards == []
    hypothetical["strategy"].extend([Decimal(-1)] * 3)
    assert choose_shadow_action(hypothetical, config()) == "WAIT"


def test_sparse_memory_zero_and_fee_drag_abstain():
    assert choose_shadow_action({}, config()) == "WAIT"
    assert choose_shadow_action({"a": [Decimal(1)] * 2}, config()) == "WAIT"
    assert choose_shadow_action({"a": [Decimal(0)] * 3}, config()) == "WAIT"
    assert choose_shadow_action({"a": [Decimal("0.01")] * 3}, config(cost_bps_per_stake=100)) == "WAIT"
    assert choose_shadow_action({"a": [Decimal("0.021")] * 3}, config(minimum_net_return="0.02")) == "a"


def test_ranking_is_deterministic_and_independent_of_decimal_context():
    history = {"z": [Decimal("0.123456789")] * 3,
               "a": [Decimal("0.123456788")] * 3}
    with localcontext() as ctx:
        ctx.prec = 3
        assert choose_shadow_action(history, config()) == "z"
    history["a"] = history["z"]
    assert choose_shadow_action(history, config()) == "a"


@pytest.mark.parametrize("changes", [{"window": True}, {"window": 0},
    {"minimum_observations": 4}, {"cost_bps_per_stake": -1},
    {"minimum_net_return": "NaN"}, {"minimum_net_return": "-0.1"}])
def test_invalid_config(changes):
    with pytest.raises(ValueError):
        config(**changes)


@pytest.mark.parametrize("reward", [True, 1.0, Decimal("NaN"), Decimal("Infinity"), Decimal(-2)])
def test_invalid_unit_returns(reward):
    with pytest.raises(ValueError):
        choose_shadow_action({"a": [reward] * 3}, config())


def test_wait_cannot_be_treated_as_a_profitable_hypothetical_bet():
    with pytest.raises(ValueError, match="implicit"):
        choose_shadow_action({"WAIT": [Decimal(5)] * 3}, config())

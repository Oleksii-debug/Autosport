from __future__ import annotations

import pytest

from autosport.betdaq_rate_governor import (
    BetdaqMethodRatePolicy,
    BetdaqRateGovernorError,
    default_betdaq_rate_policy,
)


def test_default_policy_models_listselectiontrades_at_provider_default_limit() -> None:
    """Known explicit provider limits must not fall through to UNKNOWN_UNMODELED."""

    policy = default_betdaq_rate_policy()
    method = policy.by_method()["ListSelectionTrades"]

    assert method.method == "ListSelectionTrades"
    assert method.rate_policy_key == "ListSelectionTrades"
    assert method.capacity == 1
    assert method.safety_reserve == 0


def test_listselectiontrades_cannot_be_configured_above_documented_default() -> None:
    exact = BetdaqMethodRatePolicy(
        method="ListSelectionTrades",
        capacity=1,
    )
    assert exact.rate_policy_key == "ListSelectionTrades"

    with pytest.raises(BetdaqRateGovernorError, match=r"within 1\.\.1"):
        BetdaqMethodRatePolicy(
            method="ListSelectionTrades",
            capacity=2,
        )

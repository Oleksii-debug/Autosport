from __future__ import annotations

from datetime import datetime, timezone

import pytest

from autosport.betdaq_rate_governor import (
    BetdaqBlacklistObservation,
    BetdaqBlacklistStatus,
    BetdaqMethodRatePolicy,
    BetdaqRateGovernorError,
    default_betdaq_rate_policy,
    resolve_betdaq_rate_governor,
)


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class _WallClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value


def test_default_policy_models_listselectiontrades_at_provider_default_limit() -> None:
    policy = default_betdaq_rate_policy()
    method = policy.by_method()["ListSelectionTrades"]

    assert method.method == "ListSelectionTrades"
    assert method.rate_policy_key == "ListSelectionTrades"
    assert method.capacity == 1
    assert method.safety_reserve == 0


def test_listselectiontrades_cannot_widen_provider_default_limit() -> None:
    exact = BetdaqMethodRatePolicy(method="ListSelectionTrades", capacity=1)
    assert exact.rate_policy_key == "ListSelectionTrades"

    with pytest.raises(BetdaqRateGovernorError, match=r"within 1\.\.1"):
        BetdaqMethodRatePolicy(method="ListSelectionTrades", capacity=2)


def test_listselectiontrades_blacklist_identity_binds_same_operation() -> None:
    observation = BetdaqBlacklistObservation(
        api_name="ListSelectionTrades",
        operation_id="ListSelectionTrades",
        observed_at="2026-09-23T09:00:00Z",
        blocked_until="2026-09-23T09:01:00Z",
        provider_observation_sha256="a" * 64,
    )

    assert observation.operation_id == "ListSelectionTrades"


def test_blacklist_remaining_ms_rejects_values_outside_provider_int_domain(
    tmp_path,
) -> None:
    clock = _Clock()
    wall_clock = _WallClock()
    governor = resolve_betdaq_rate_governor(
        tmp_path.resolve(),
        default_betdaq_rate_policy(),
        clock=clock,
        wall_clock=wall_clock,
    )

    for malformed in (2_147_483_648, 10**30):
        with pytest.raises(
            BetdaqRateGovernorError,
            match="remaining_ms must be within BETDAQ provider int domain",
        ):
            governor.observe_blacklist(
                api_name="GetPrices",
                remaining_ms=malformed,
                provider_observation_sha256="b" * 64,
            )

    assert governor.blacklist_status("GetPrices") is BetdaqBlacklistStatus.UNKNOWN


def test_blacklist_remaining_ms_accepts_max_provider_int(tmp_path) -> None:
    clock = _Clock()
    wall_clock = _WallClock()
    governor = resolve_betdaq_rate_governor(
        tmp_path.resolve(),
        default_betdaq_rate_policy(),
        clock=clock,
        wall_clock=wall_clock,
    )

    observation = governor.observe_blacklist(
        api_name="GetPrices",
        remaining_ms=2_147_483_647,
        provider_observation_sha256="c" * 64,
    )

    assert observation.operation_id == "GetPrices"
    assert governor.blacklist_status("GetPrices") is BetdaqBlacklistStatus.BLACKLISTED

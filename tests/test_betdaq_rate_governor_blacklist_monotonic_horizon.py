"""Falsify wall-clock shortening of BETDAQ RemainingMS provider fences."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from autosport.betdaq_rate_governor import (
    BetdaqBlacklistStatus,
    BetdaqRateDeferred,
    default_betdaq_rate_policy,
    resolve_betdaq_rate_governor,
)


class FakeMonotonicClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeWallClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 23, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def test_forward_wall_clock_jump_cannot_shorten_provider_remaining_ms(
    tmp_path,
) -> None:
    policy = default_betdaq_rate_policy()
    monotonic = FakeMonotonicClock()
    wall = FakeWallClock()
    governor = resolve_betdaq_rate_governor(
        tmp_path / "workspace",
        policy,
        clock=monotonic,
        wall_clock=wall,
        authority_root=tmp_path / "machine-authority",
    )

    # Finish only the process-local cold-start rate window.  No real sleeps.
    monotonic.advance(float(policy.cold_start_seconds))
    governor.observe_blacklist(
        api_name="GetPrices",
        remaining_ms=60_000,
        provider_observation_sha256="a" * 64,
    )

    # Simulate an operator/NTP/system wall-clock correction forward while no
    # monotonic time elapses.  A known provider RemainingMS fence must not be
    # weakened by that unrelated wall-clock adjustment.
    wall.advance(61.0)

    assert (
        governor.blacklist_status("GetPrices")
        is BetdaqBlacklistStatus.BLACKLISTED
    )
    with pytest.raises(BetdaqRateDeferred) as denied:
        governor.admit("GetPrices")
    assert denied.value.reason == "provider_blacklist_active"
    assert denied.value.retry_after_seconds is not None
    assert denied.value.retry_after_seconds >= 60.0

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from autosport.betdaq_account_readonly import BetdaqCredentials
from autosport.betdaq_readonly_live_provider import BetdaqLiveReadOnlyProvider
from autosport.betdaq_rate_governor import (
    default_betdaq_rate_policy,
    resolve_betdaq_rate_governor,
)
from autosport.betdaq_readonly_provider import BetdaqMarketBinding


class _CallerDecimal(Decimal):
    pass


class _RateClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def _credentials() -> BetdaqCredentials:
    return BetdaqCredentials(
        username="fixture-user",
        password="fixture-password",
        application_identifier="fixture-app",
    )


def _bindings() -> list[BetdaqMarketBinding]:
    return [BetdaqMarketBinding(9001, "event-1", "football")]


def _governor(tmp_path):
    clock = _RateClock()
    governor = resolve_betdaq_rate_governor(
        tmp_path,
        default_betdaq_rate_policy(),
        clock=clock,
        wall_clock=lambda: datetime(2026, 9, 23, tzinfo=timezone.utc),
    )
    clock.value = 61.0
    return governor


def test_live_provider_rejects_decimal_subclass_before_composition(tmp_path) -> None:
    with pytest.raises(TypeError, match="threshold_amount must be exact Decimal"):
        BetdaqLiveReadOnlyProvider(
            credentials=_credentials(),
            rate_governor=_governor(tmp_path),
            market_bindings=_bindings(),
            threshold_amount=_CallerDecimal("1.50"),
        )


def test_live_provider_accepts_exact_decimal_threshold(tmp_path) -> None:
    provider = BetdaqLiveReadOnlyProvider(
        credentials=_credentials(),
        rate_governor=_governor(tmp_path),
        market_bindings=_bindings(),
        threshold_amount=Decimal("1.50"),
    )
    assert type(provider.threshold_amount) is Decimal
    assert provider.last_request_evidence is None

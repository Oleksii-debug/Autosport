from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.betdaq_account_readonly import BetdaqCredentials
from autosport.betdaq_readonly_live_provider import BetdaqLiveReadOnlyProvider
from autosport.betdaq_readonly_provider import BetdaqMarketBinding


class _CallerDecimal(Decimal):
    pass


def _credentials() -> BetdaqCredentials:
    return BetdaqCredentials(
        username="fixture-user",
        password="fixture-password",
        application_identifier="fixture-app",
    )


def _bindings() -> list[BetdaqMarketBinding]:
    return [BetdaqMarketBinding(9001, "event-1", "football")]


def test_live_provider_rejects_decimal_subclass_before_composition() -> None:
    with pytest.raises(TypeError, match="threshold_amount must be exact Decimal"):
        BetdaqLiveReadOnlyProvider(
            credentials=_credentials(),
            market_bindings=_bindings(),
            threshold_amount=_CallerDecimal("1.50"),
        )


def test_live_provider_accepts_exact_decimal_threshold() -> None:
    provider = BetdaqLiveReadOnlyProvider(
        credentials=_credentials(),
        market_bindings=_bindings(),
        threshold_amount=Decimal("1.50"),
    )
    assert type(provider.threshold_amount) is Decimal
    assert provider.last_request_evidence is None

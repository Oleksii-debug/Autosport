from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.betdaq_readonly_market_wire import BetdaqSoapProtocolError
from autosport.betdaq_readonly_provider import (
    BetdaqMarketBinding,
    BetdaqReadOnlyProvider,
)
from autosport.domain import MarketType


API = "http://www.GlobalBettingExchange.com/ExternalAPI/"
SOAP11 = "http://schemas.xmlsoap.org/soap/envelope/"


def _payload(*, unavailable_market_id: int | None = 9002) -> str:
    unavailable = (
        ""
        if unavailable_market_id is None
        else (
            f'<MarketPrices xmlns="{API}" Id="{unavailable_market_id}" '
            'ReturnCode="16" />'
        )
    )
    return f'''<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="{SOAP11}">
  <soap:Body>
    <GetPricesResponse xmlns="{API}">
      <GetPricesResult>
        <ReturnStatus Code="0" Description="Success" CallId="call-rc016" />
        <MarketPrices
          Id="9001"
          Name="Match Winner"
          Type="1"
          IsPlayMarket="false"
          Status="2"
          NumberOfWinningSelections="1"
          StartTime="2026-09-23T12:00:00Z"
          WithdrawalSequenceNumber="7"
          DisplayOrder="1"
          IsEnabledForMultiples="true"
          IsInRunningAllowed="true"
          IsManagedWhenInRunning="true"
          IsCurrentlyInRunning="false"
          InRunningDelaySeconds="5">
          <Selections
            Id="501"
            Name="Alpha"
            Status="3"
            ResetCount="4"
            DeductionFactor="0.1250">
            <ForSidePrices Price="2.00" Stake="12.34" />
            <AgainstSidePrices Price="2.10" Stake="9.50" />
          </Selections>
        </MarketPrices>
        {unavailable}
      </GetPricesResult>
    </GetPricesResponse>
  </soap:Body>
</soap:Envelope>'''


class _StaticTransport:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.requests = []

    def get_prices(self, request, *, timeout_seconds: float):
        self.requests.append((request, timeout_seconds))
        return self.payload


def _provider(payload: str, *, include_second_binding: bool = True) -> BetdaqReadOnlyProvider:
    bindings = [
        BetdaqMarketBinding(
            9001,
            "event-9001",
            "soccer_epl",
            MarketType.WINNER,
        )
    ]
    if include_second_binding:
        bindings.append(
            BetdaqMarketBinding(
                9002,
                "event-9002",
                "soccer_epl",
                MarketType.WINNER,
            )
        )
    return BetdaqReadOnlyProvider(
        transport=_StaticTransport(payload),
        market_bindings=bindings,
        threshold_amount=Decimal("1.00"),
        max_attempts=1,
        clock=lambda: "2026-09-23T10:00:00Z",
    )


def test_rc016_preserves_valid_sibling_and_records_unavailable_scope() -> None:
    provider = _provider(_payload())

    batch = provider.read_batch()

    assert {quote.provider_market_id for quote in batch.quotes} == {"9001"}
    assert {quote.exchange_side for quote in batch.quotes} == {"back", "lay"}
    assert "FULL_MARKET_SCOPE" not in batch.quality_flags
    assert "PARTIAL_MARKET_SCOPE" in batch.quality_flags
    assert "BETDAQ_RC016_MARKET_UNAVAILABLE" in batch.quality_flags

    evidence = provider.last_request_evidence
    assert evidence is not None
    assert evidence.unavailable_market_ids == (9002,)
    assert len(evidence.requests) == 1
    assert evidence.requests[0].market_ids == (9001, 9002)
    assert evidence.requests[0].unavailable_market_ids == (9002,)


def test_rc016_identity_must_complete_exact_requested_scope() -> None:
    provider = _provider(_payload(unavailable_market_id=9003))

    with pytest.raises(BetdaqSoapProtocolError, match="market set mismatch"):
        provider.read_batch()


def test_no_rc016_retains_full_market_scope_truth() -> None:
    provider = _provider(_payload(unavailable_market_id=None), include_second_binding=False)

    batch = provider.read_batch()

    assert "FULL_MARKET_SCOPE" in batch.quality_flags
    assert "PARTIAL_MARKET_SCOPE" not in batch.quality_flags
    assert "BETDAQ_RC016_MARKET_UNAVAILABLE" not in batch.quality_flags
    evidence = provider.last_request_evidence
    assert evidence is not None
    assert evidence.unavailable_market_ids == ()
    assert evidence.requests[0].unavailable_market_ids == ()

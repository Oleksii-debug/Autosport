from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.betdaq_readonly_market_wire import BetdaqSoapProtocolError
from autosport.betdaq_readonly_provider import (
    BetdaqGetPricesRequest,
    BetdaqMarketBinding,
    BetdaqReadOnlyProvider,
)


API = "http://www.GlobalBettingExchange.com/ExternalAPI/"
SOAP = "http://schemas.xmlsoap.org/soap/envelope/"


def _response(
    *,
    price: str = "2.00",
    stake: str = "5.00",
    deduction: str = "0.125",
) -> str:
    return f"""<soap:Envelope xmlns:soap="{SOAP}">
      <soap:Body>
        <GetPricesResponse xmlns="{API}">
          <GetPricesResult>
            <ReturnStatus Code="0" Description="Success" />
            <MarketPrices
              Id="9001"
              Name="Match Odds"
              Type="1"
              IsPlayMarket="false"
              Status="2"
              StartTime="2026-09-23T18:00:00Z"
              WithdrawalSequenceNumber="7"
              IsInRunningAllowed="true"
              IsManagedWhenInRunning="true"
              IsCurrentlyInRunning="false"
              InRunningDelaySeconds="5"
            >
              <Selections
                Id="501"
                Name="Player A"
                Status="3"
                ResetCount="4"
                DeductionFactor="{deduction}"
              >
                <ForSidePrices Price="{price}" Stake="{stake}" />
                <AgainstSidePrices Price="2.10" Stake="9.50" />
              </Selections>
            </MarketPrices>
          </GetPricesResult>
        </GetPricesResponse>
      </soap:Body>
    </soap:Envelope>"""


class _Transport:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0

    def get_prices(self, request, *, timeout_seconds):
        self.calls += 1
        return self.payload


def _provider(payload: str, *, threshold: Decimal = Decimal("1")):
    transport = _Transport(payload)
    provider = BetdaqReadOnlyProvider(
        transport=transport,
        market_bindings=[BetdaqMarketBinding(9001, "event-1", "football")],
        threshold_amount=threshold,
        clock=lambda: "2026-09-23T00:00:01Z",
    )
    return provider, transport


def test_huge_positive_threshold_exponent_fails_before_transport() -> None:
    transport = _Transport(_response())
    with pytest.raises(ValueError, match="fixed-point representation exceeds"):
        BetdaqReadOnlyProvider(
            transport=transport,
            market_bindings=[BetdaqMarketBinding(9001, "event-1", "football")],
            threshold_amount=Decimal("1E+1000000"),
        )
    assert transport.calls == 0


def test_huge_negative_threshold_exponent_fails_on_public_request_contract() -> None:
    with pytest.raises(ValueError, match="fixed-point representation exceeds"):
        BetdaqGetPricesRequest(
            request_id=1,
            market_ids=(9001,),
            threshold_amount=Decimal("1E-1000000"),
        )


@pytest.mark.parametrize(
    ("field", "payload"),
    [
        ("BETDAQ available amount", _response(stake="1E+1000000")),
        ("BETDAQ decimal odds", _response(price="1E+1000000")),
        ("BETDAQ deduction factor", _response(deduction="1E-1000000")),
    ],
)
def test_provider_compact_exponent_cannot_expand_unbounded_metadata_or_odds(
    field: str,
    payload: str,
) -> None:
    provider, transport = _provider(payload)
    with pytest.raises(
        BetdaqSoapProtocolError,
        match=rf"{field} fixed-point representation exceeds",
    ):
        provider.read_batch()
    assert transport.calls == 1
    assert provider.last_request_evidence is None


def test_exact_512_character_fixed_point_boundary_remains_bounded() -> None:
    provider, _ = _provider(
        _response(
            stake="1E+511",
            deduction="0E-510",
        )
    )
    quote = provider.read_batch().quotes[0]
    assert len(quote.metadata["betdaq_available_amount"]) == 512
    assert len(quote.metadata["betdaq_deduction_factor"]) == 512

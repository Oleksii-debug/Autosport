from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib

import pytest

from autosport.betdaq_account_readonly import BetdaqCredentials
from autosport.betdaq_readonly_live_provider import BetdaqLiveReadOnlyProvider
from autosport.betdaq_readonly_market_wire import (
    EXTERNAL_API_NS,
    SOAP11_NS,
    BetdaqSoapProtocolError,
)
from autosport.betdaq_rate_governor import (
    default_betdaq_rate_policy,
    resolve_betdaq_rate_governor,
)
from autosport.betdaq_readonly_provider import BetdaqMarketBinding
from autosport.domain import MarketType
from autosport.providers import CanonicalNormalizer


def _credentials() -> BetdaqCredentials:
    return BetdaqCredentials(
        username="fixture-user",
        password="fixture-password",
        application_identifier="fixture-app",
    )


class _RateClock:
    def __init__(self) -> None:
        self.value = 61.0

    def __call__(self) -> float:
        return self.value


def _governor(tmp_path):
    rate_clock = _RateClock()
    return resolve_betdaq_rate_governor(
        tmp_path,
        default_betdaq_rate_policy(),
        clock=rate_clock,
        wall_clock=lambda: datetime(2026, 9, 29, tzinfo=timezone.utc),
    )


def _event_response(
    *,
    root_id: int = 100,
    event_id: int = 101,
    market_id: int = 9001,
    market_type: int = 1,
    event_name: str = "Fixture A",
) -> bytes:
    api = EXTERNAL_API_NS
    soap = SOAP11_NS
    xsi = "http://www.w3.org/2001/XMLSchema-instance"
    return f"""<soap:Envelope xmlns:soap="{soap}" xmlns:xsi="{xsi}">
      <soap:Body>
        <GetEventSubTreeNoSelectionsResponse xmlns="{api}">
          <GetEventSubTreeNoSelectionsResult>
            <EventClassifiers
              Id="{root_id}"
              Name="Football"
              DisplayOrder="1"
              IsEnabledForMultiples="true"
              ParentId="0">
              <EventClassifiers
                Id="{event_id}"
                Name="{event_name}"
                DisplayOrder="1"
                IsEnabledForMultiples="true"
                ParentId="{root_id}">
                <Markets
                  Id="{market_id}"
                  Name="Match Odds"
                  Type="{market_type}"
                  IsPlayMarket="false"
                  Status="2"
                  NumberOfWinningSelections="1"
                  StartTime="2026-09-29T18:00:00Z"
                  WithdrawalSequenceNumber="7"
                  DisplayOrder="1"
                  IsEnabledForMultiples="true"
                  IsInRunningAllowed="true"
                  RaceGrade=""
                  IsManagedWhenInRunning="true"
                  IsCurrentlyInRunning="false"
                  InRunningDelaySeconds="5"
                  EventClassifierId="{event_id}"
                  PlacePayout="0">
                  <Selections xsi:nil="true" />
                </Markets>
              </EventClassifiers>
            </EventClassifiers>
          </GetEventSubTreeNoSelectionsResult>
        </GetEventSubTreeNoSelectionsResponse>
      </soap:Body>
    </soap:Envelope>""".encode()


def _prices_response(*, market_id: int = 9001, market_type: int = 1) -> bytes:
    api = EXTERNAL_API_NS
    soap = SOAP11_NS
    return f"""<soap:Envelope xmlns:soap="{soap}">
      <soap:Body>
        <GetPricesResponse xmlns="{api}">
          <GetPricesResult>
            <ReturnStatus Code="0" Description="Success" />
            <MarketPrices
              Id="{market_id}"
              Name="Match Odds"
              Type="{market_type}"
              IsPlayMarket="false"
              Status="2"
              StartTime="2026-09-29T18:00:00Z"
              WithdrawalSequenceNumber="7"
              IsInRunningAllowed="true"
              IsManagedWhenInRunning="true"
              IsCurrentlyInRunning="false"
              InRunningDelaySeconds="5">
              <Selections
                Id="501"
                Name="Selection A"
                Status="3"
                ResetCount="4">
                <ForSidePrices Price="2.00" Stake="5.00" />
                <AgainstSidePrices Price="2.10" Stake="6.00" />
              </Selections>
            </MarketPrices>
          </GetPricesResult>
        </GetPricesResponse>
      </soap:Body>
    </soap:Envelope>""".encode()


class _ActionTransport:
    def __init__(
        self,
        *,
        event_payload: bytes | None = None,
        prices_payload: bytes | None = None,
    ) -> None:
        self.event_payload = _event_response() if event_payload is None else event_payload
        self.prices_payload = _prices_response() if prices_payload is None else prices_payload
        self.calls: list[str] = []

    def post(self, url, *, headers, body, timeout_seconds):
        action = headers.get("SOAPAction", "")
        self.calls.append(action)
        if "GetEventSubTreeNoSelections" in action:
            return self.event_payload
        if "GetPrices" in action:
            return self.prices_payload
        raise AssertionError("unexpected BETDAQ operation")


def _provider(
    tmp_path,
    transport: _ActionTransport,
    *,
    root: str = "100",
    sport: str = "football",
    market_type: MarketType = MarketType.WINNER,
) -> BetdaqLiveReadOnlyProvider:
    return BetdaqLiveReadOnlyProvider(
        credentials=_credentials(),
        rate_governor=_governor(tmp_path),
        transport=transport,
        market_bindings=[
            BetdaqMarketBinding(
                9001,
                root,
                sport,
                market_type,
            )
        ],
        threshold_amount=Decimal("1"),
        max_attempts=1,
        clock=lambda: "2026-09-29T19:00:00Z",
    )


def test_live_canonical_identity_comes_from_catalogue_not_caller_semantics(tmp_path) -> None:
    transport = _ActionTransport()
    provider = _provider(
        tmp_path,
        transport,
        sport="tennis",
        market_type=MarketType.TOTAL,
    )

    batch = provider.read_batch()
    quote = batch.quotes[0]
    normalized = CanonicalNormalizer().normalize(batch.source_id, quote)

    assert quote.provider_event_id == "101"
    assert quote.sport is None
    assert quote.market_type is MarketType.OTHER
    assert normalized.event_id == "betdaq-readonly:101"
    assert normalized.sport is None
    assert normalized.market_type is MarketType.OTHER
    assert quote.metadata["betdaq_catalogue_event_path_ids"] == [100, 101]
    assert quote.metadata["betdaq_catalogue_event_path_names"] == [
        "Football",
        "Fixture A",
    ]
    assert "CATALOGUE_BOUND_EVENT_IDENTITY" in batch.quality_flags
    assert len(transport.calls) == 2


def test_same_prices_with_different_provider_event_tree_changes_causal_identity(tmp_path) -> None:
    first = _provider(
        tmp_path / "first",
        _ActionTransport(event_payload=_event_response(event_id=101)),
    )
    second = _provider(
        tmp_path / "second",
        _ActionTransport(event_payload=_event_response(event_id=102)),
    )

    first_batch = first.read_batch()
    second_batch = second.read_batch()
    first_evidence = first.last_request_evidence
    second_evidence = second.last_request_evidence

    assert first_batch.quotes[0].provider_event_id == "101"
    assert second_batch.quotes[0].provider_event_id == "102"
    assert first_evidence is not None
    assert second_evidence is not None
    assert (
        first_evidence.requests[0].response_sha256
        == second_evidence.requests[0].response_sha256
    )
    assert first_evidence.catalogue_response_sha256 != second_evidence.catalogue_response_sha256
    assert first_evidence.aggregate_sha256 != second_evidence.aggregate_sha256


def test_catalogue_market_type_must_match_getprices_provider_type(tmp_path) -> None:
    provider = _provider(
        tmp_path,
        _ActionTransport(
            event_payload=_event_response(market_type=2),
            prices_payload=_prices_response(market_type=1),
        ),
    )

    with pytest.raises(
        BetdaqSoapProtocolError,
        match="conflicts with catalogue-bound market Type",
    ):
        provider.read_batch()

    assert provider.last_request_evidence is None


def test_caller_event_scope_must_contain_returned_market(tmp_path) -> None:
    provider = _provider(
        tmp_path,
        _ActionTransport(event_payload=_event_response(root_id=100)),
        root="999",
    )

    with pytest.raises(ValueError, match="outside caller-requested event scope"):
        provider.read_batch()

    assert provider.last_request_evidence is None


@pytest.mark.parametrize("scope", ["event-100", "0100", "+100", " 100", ""])
def test_live_scope_assertion_requires_provider_decimal_event_id(tmp_path, scope) -> None:
    provider = _provider(
        tmp_path,
        _ActionTransport(),
        root=scope,
    )

    with pytest.raises(ValueError, match="event-classifier id"):
        provider.read_batch()


def test_catalogue_acquisition_is_bound_into_snapshot_evidence(tmp_path) -> None:
    event_payload = _event_response()
    provider = _provider(
        tmp_path,
        _ActionTransport(event_payload=event_payload),
    )

    provider.read_batch()

    evidence = provider.last_request_evidence
    catalogue = provider.last_catalogue_evidence
    assert evidence is not None
    assert catalogue is not None
    assert catalogue.response_sha256 == hashlib.sha256(event_payload).hexdigest()
    assert evidence.catalogue_response_sha256 == catalogue.response_sha256
    assert evidence.catalogue_event_classifier_ids == (100,)
    assert evidence.catalogue_rate_admission_receipt == catalogue.rate_admission_receipt
    assert catalogue.provider_origin_verified is False
    assert catalogue.grants_execution_authority is False
    assert catalogue.grants_write_permission is False

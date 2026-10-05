from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib

import pytest

import autosport.betdaq_account_readonly as betdaq_account_readonly_module
from autosport.betdaq_account_readonly import BetdaqCredentials
from autosport.betdaq_catalogue_binding import BetdaqLiveCatalogueResolver
from autosport.betdaq_readonly_live_provider import BetdaqLiveReadOnlyProvider
from autosport.betdaq_readonly_live_transport import BetdaqReadOnlyLiveTransport
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
from autosport.providers import CanonicalNormalizer, ProviderUnavailableError


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
    created_at: str | None = None,
) -> bytes:
    api = EXTERNAL_API_NS
    soap = SOAP11_NS
    xsi = "http://www.w3.org/2001/XMLSchema-instance"
    wsse = (
        "http://docs.oasis-open.org/wss/2004/01/"
        "oasis-200401-wss-wssecurity-secext-1.0.xsd"
    )
    wsu = (
        "http://docs.oasis-open.org/wss/2004/01/"
        "oasis-200401-wss-wssecurity-utility-1.0.xsd"
    )
    header = ""
    if created_at is not None:
        header = f"""<soap:Header>
          <wsse:Security xmlns:wsse="{wsse}">
            <wsu:Timestamp xmlns:wsu="{wsu}">
              <wsu:Created>{created_at}</wsu:Created>
            </wsu:Timestamp>
          </wsse:Security>
        </soap:Header>"""
    return f"""<soap:Envelope xmlns:soap="{soap}" xmlns:xsi="{xsi}">
      {header}
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


class _FakeHttpResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def read(self) -> bytes:
        return self._payload


class _CanonicalUrlopenRouter:
    def __init__(
        self,
        *,
        event_payload: bytes | None = None,
        prices_payload: bytes | None = None,
    ) -> None:
        self.event_payload = _event_response() if event_payload is None else event_payload
        self.prices_payload = _prices_response() if prices_payload is None else prices_payload
        self.calls: list[bytes] = []

    def __call__(self, request, *, timeout):
        body = request.data
        assert type(body) is bytes
        self.calls.append(body)
        if b"GetEventSubTreeNoSelections" in body:
            return _FakeHttpResponse(self.event_payload)
        if b"GetPrices" in body:
            return _FakeHttpResponse(self.prices_payload)
        raise AssertionError("unexpected BETDAQ operation")


def _provider(
    tmp_path,
    monkeypatch,
    router: _CanonicalUrlopenRouter,
    *,
    root: str = "100",
    sport: str = "football",
    market_type: MarketType = MarketType.WINNER,
    max_message_age_seconds: int | None = None,
) -> BetdaqLiveReadOnlyProvider:
    monkeypatch.setattr(betdaq_account_readonly_module, "urlopen", router)
    return BetdaqLiveReadOnlyProvider(
        credentials=_credentials(),
        rate_governor=_governor(tmp_path),
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
        max_message_age_seconds=max_message_age_seconds,
        clock=lambda: "2026-09-29T19:00:00Z",
    )


def test_live_canonical_identity_comes_from_catalogue_not_caller_semantics(
    tmp_path,
    monkeypatch,
) -> None:
    router = _CanonicalUrlopenRouter()
    provider = _provider(
        tmp_path,
        monkeypatch,
        router,
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
    assert len(router.calls) == 2


def test_same_prices_with_different_provider_event_tree_changes_causal_identity(
    tmp_path,
    monkeypatch,
) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()

    first = _provider(
        first_root,
        monkeypatch,
        _CanonicalUrlopenRouter(event_payload=_event_response(event_id=101)),
    )
    first_batch = first.read_batch()
    first_evidence = first.last_request_evidence

    second = _provider(
        second_root,
        monkeypatch,
        _CanonicalUrlopenRouter(event_payload=_event_response(event_id=102)),
    )
    second_batch = second.read_batch()
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


def test_catalogue_market_type_must_match_getprices_provider_type(
    tmp_path,
    monkeypatch,
) -> None:
    provider = _provider(
        tmp_path,
        monkeypatch,
        _CanonicalUrlopenRouter(
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
    assert provider.last_catalogue_evidence is None


def test_failed_successor_keeps_last_successful_evidence_pair(
    tmp_path,
    monkeypatch,
) -> None:
    router = _CanonicalUrlopenRouter()
    provider = _provider(tmp_path, monkeypatch, router)

    provider.read_batch()
    first_request_evidence = provider.last_request_evidence
    first_catalogue_evidence = provider.last_catalogue_evidence
    assert first_request_evidence is not None
    assert first_catalogue_evidence is not None

    router.event_payload = _event_response(event_name="Fixture B")
    router.prices_payload = b"not xml"

    with pytest.raises(BetdaqSoapProtocolError):
        provider.read_batch()

    assert provider.last_request_evidence is first_request_evidence
    assert provider.last_catalogue_evidence is first_catalogue_evidence
    assert (
        provider.last_request_evidence.catalogue_response_sha256
        == provider.last_catalogue_evidence.response_sha256
    )


def test_caller_event_scope_must_contain_returned_market(
    tmp_path,
    monkeypatch,
) -> None:
    provider = _provider(
        tmp_path,
        monkeypatch,
        _CanonicalUrlopenRouter(event_payload=_event_response(root_id=100)),
        root="999",
    )

    with pytest.raises(ValueError, match="outside caller-requested event scope"):
        provider.read_batch()

    assert provider.last_request_evidence is None


@pytest.mark.parametrize(
    "scope",
    ["event-100", "0100", "+100", " 100", "", str(1 << 63)],
)
def test_live_scope_assertion_requires_provider_decimal_event_id(
    tmp_path,
    monkeypatch,
    scope,
) -> None:
    with pytest.raises(ValueError, match="event-classifier id"):
        _provider(
            tmp_path,
            monkeypatch,
            _CanonicalUrlopenRouter(),
            root=scope,
        )


def test_provider_catalogue_rejects_event_id_outside_xml_long_domain(
    tmp_path,
    monkeypatch,
) -> None:
    provider = _provider(
        tmp_path,
        monkeypatch,
        _CanonicalUrlopenRouter(
            event_payload=_event_response(event_id=(1 << 63))
        ),
    )

    with pytest.raises(
        BetdaqSoapProtocolError,
        match="outside provider long domain",
    ):
        provider.read_batch()

    assert provider.last_request_evidence is None


@pytest.mark.parametrize(
    ("created_at", "max_age", "message"),
    [
        ("2026-09-29T19:00:01Z", None, "future"),
        ("2026-09-29T18:59:00Z", 30, "stale"),
    ],
)
def test_catalogue_message_time_must_be_causally_valid(
    tmp_path,
    monkeypatch,
    created_at,
    max_age,
    message,
) -> None:
    provider = _provider(
        tmp_path,
        monkeypatch,
        _CanonicalUrlopenRouter(
            event_payload=_event_response(created_at=created_at)
        ),
        max_message_age_seconds=max_age,
    )

    with pytest.raises(BetdaqSoapProtocolError, match=message):
        provider.read_batch()

    assert provider.last_request_evidence is None
    assert provider.last_catalogue_evidence is None


class _TransientCataloguePost:
    def __init__(self, *, opaque: bool = False) -> None:
        self.calls = 0
        self.opaque = opaque

    def post(self, url, *, headers, body, timeout_seconds):
        self.calls += 1
        if self.opaque:
            raise RuntimeError("opaque catalogue failure")
        if self.calls == 1:
            raise TimeoutError("explicit catalogue transient")
        return _event_response()


def test_catalogue_retry_consumes_one_rate_admission_per_attempt(tmp_path) -> None:
    post = _TransientCataloguePost()
    bridge = BetdaqReadOnlyLiveTransport(
        credentials=_credentials(),
        rate_governor=_governor(tmp_path),
        transport=post,
    )
    resolver = BetdaqLiveCatalogueResolver(
        transport=bridge,
        timeout_seconds=1.0,
        clock=lambda: "2026-09-29T19:00:00Z",
        max_attempts=2,
    )

    resolved, evidence = resolver.resolve(
        [BetdaqMarketBinding(9001, "100", "football", MarketType.WINNER)]
    )

    assert resolved[9001].provider_event_id == "101"
    assert post.calls == 2
    assert len(evidence.rate_admission_receipts) == 2
    assert evidence.rate_admission_receipts[0] != evidence.rate_admission_receipts[1]
    admission = bridge.last_rate_admission
    assert admission is not None
    assert admission.sequence == 2
    assert evidence.rate_admission_receipts[-1] == admission.receipt_sha256


def test_catalogue_opaque_transport_failure_is_not_retried(tmp_path) -> None:
    post = _TransientCataloguePost(opaque=True)
    bridge = BetdaqReadOnlyLiveTransport(
        credentials=_credentials(),
        rate_governor=_governor(tmp_path),
        transport=post,
    )
    resolver = BetdaqLiveCatalogueResolver(
        transport=bridge,
        timeout_seconds=1.0,
        clock=lambda: "2026-09-29T19:00:00Z",
        max_attempts=4,
    )

    with pytest.raises(
        ProviderUnavailableError,
        match="without retryable classification",
    ):
        resolver.resolve(
            [BetdaqMarketBinding(9001, "100", "football", MarketType.WINNER)]
        )

    assert post.calls == 1


def test_catalogue_acquisition_is_bound_into_snapshot_evidence(
    tmp_path,
    monkeypatch,
) -> None:
    event_payload = _event_response()
    provider = _provider(
        tmp_path,
        monkeypatch,
        _CanonicalUrlopenRouter(event_payload=event_payload),
    )

    provider.read_batch()

    evidence = provider.last_request_evidence
    catalogue = provider.last_catalogue_evidence
    assert evidence is not None
    assert catalogue is not None
    assert catalogue.response_sha256 == hashlib.sha256(event_payload).hexdigest()
    assert evidence.catalogue_request_fingerprint == catalogue.request_fingerprint
    assert evidence.catalogue_response_sha256 == catalogue.response_sha256
    assert evidence.catalogue_event_classifier_ids == (100,)
    assert (
        evidence.catalogue_rate_admission_receipts
        == catalogue.rate_admission_receipts
    )
    assert len(catalogue.rate_admission_receipts) == 1
    assert catalogue.provider_origin_verified is False
    assert catalogue.grants_execution_authority is False
    assert catalogue.grants_write_permission is False

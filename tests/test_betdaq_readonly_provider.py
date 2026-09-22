from __future__ import annotations

from decimal import Decimal
import pytest

from autosport.betdaq_readonly_market_wire import BetdaqSoapProtocolError
from autosport.betdaq_readonly_provider import (
    BetdaqMarketBinding,
    BetdaqReadOnlyProvider,
    BetdaqTransientTransportError,
)
from autosport.domain import MarketType
from autosport.providers import ProviderUnavailableError

API = "http://www.GlobalBettingExchange.com/ExternalAPI/"
SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"

def response(*, created="2026-09-23T00:00:00Z", market_id=9001, for_price="2.000", against_price="2.100", extra_for="", return_code=0):
    header = ""
    if created is not None:
        header = f'''<soap:Header xmlns:wsse="{WSSE}" xmlns:wsu="{WSU}">
        <wsse:Security><wsu:Timestamp><wsu:Created>{created}</wsu:Created></wsu:Timestamp></wsse:Security>
        </soap:Header>'''
    return f'''<soap:Envelope xmlns:soap="{SOAP}">
    {header}
    <soap:Body>
      <GetPricesResponse xmlns="{API}">
        <GetPricesResult>
          <ReturnStatus Code="{return_code}" Description="Success" CallId="call-123" />
          <MarketPrices Id="{market_id}" Name="Match Odds" Type="1" IsPlayMarket="false"
            Status="2" StartTime="2026-09-23T18:00:00Z" WithdrawalSequenceNumber="7"
            IsInRunningAllowed="true" IsManagedWhenInRunning="true"
            IsCurrentlyInRunning="false" InRunningDelaySeconds="5">
            <Selections Id="501" Name="Player A" Status="3" ResetCount="4" DeductionFactor="0.1250">
              <ForSidePrices Price="{for_price}" Stake="12.3400" />{extra_for}
              <AgainstSidePrices Price="{against_price}" Stake="9.50" />
            </Selections>
          </MarketPrices>
        </GetPricesResult>
      </GetPricesResponse>
    </soap:Body>
    </soap:Envelope>'''

class Transport:
    def __init__(self, outcomes):
        self.outcomes=list(outcomes)
        self.calls=[]
    def get_prices(self, request, *, timeout_seconds):
        self.calls.append((request, timeout_seconds))
        value=self.outcomes.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value

def provider(transport, **kwargs):
    return BetdaqReadOnlyProvider(
        transport=transport,
        market_bindings=[
            BetdaqMarketBinding(
                market_id=9001,
                provider_event_id="event-77",
                sport="football",
                market_type=MarketType.WINNER,
            )
        ],
        threshold_amount=Decimal("5.00"),
        clock=lambda: "2026-09-23T00:00:01Z",
        request_id_factory=lambda: 42,
        **kwargs,
    )

def test_maps_for_and_against_to_distinct_canonical_exchange_sides_exactly():
    p=provider(Transport([response()]))
    batch=p.read_batch()
    assert [q.exchange_side for q in batch.quotes] == ["back","lay"]
    assert [q.decimal_odds for q in batch.quotes] == [Decimal("2.000"), Decimal("2.100")]
    assert batch.quotes[0].provider_selection_id == batch.quotes[1].provider_selection_id == "501"
    assert batch.quotes[0].provider_event_id == "event-77"
    assert batch.quotes[0].market_type is MarketType.WINNER
    assert batch.quotes[0].sport == "football"

def test_never_mints_quote_source_time_from_market_start_or_message_created():
    p=provider(Transport([response(created="2026-09-23T00:00:00.250Z")]))
    batch=p.read_batch()
    assert all(q.source_ts is None for q in batch.quotes)
    assert all(q.observed_ts == "2026-09-23T00:00:01Z" for q in batch.quotes)
    assert all(q.metadata["betdaq_market_start_time"] == "2026-09-23T18:00:00Z" for q in batch.quotes)
    assert all(q.metadata["betdaq_message_created_at"] == "2026-09-23T00:00:00.250Z" for q in batch.quotes)
    assert "QUOTE_SOURCE_TIMESTAMP_UNAVAILABLE" in batch.quality_flags
    assert p.last_request_evidence.quote_source_timestamp_available is False

def test_receive_time_is_sampled_after_transport_return():
    events=[]
    class Ordered:
        def get_prices(self, request, *, timeout_seconds):
            events.append("transport")
            return response()
    def clock():
        events.append("clock")
        return "2026-09-23T00:00:01Z"
    p=BetdaqReadOnlyProvider(
        transport=Ordered(),
        market_bindings=[BetdaqMarketBinding(9001,"event-77","football")],
        threshold_amount=Decimal("1"),
        clock=clock,
    )
    p.read_batch()
    assert events == ["transport","clock","clock"]

def test_future_message_envelope_fails_closed_without_partial_state():
    t=Transport([
        response(created="2026-09-23T00:00:02Z"),
        response(created="2026-09-23T00:00:00Z"),
    ])
    p=provider(t)
    with pytest.raises(BetdaqSoapProtocolError, match="future"):
        p.read_batch()
    batch=p.read_batch()
    assert len(t.calls) == 2
    assert len(batch.quotes) == 2

def test_stale_message_envelope_is_policy_checked_but_not_promoted_to_source_ts():
    p=provider(
        Transport([response(created="2026-09-22T23:59:00Z")]),
        max_message_age_seconds=10,
    )
    with pytest.raises(BetdaqSoapProtocolError, match="stale"):
        p.read_batch()

def test_missing_message_timestamp_is_allowed_and_explicitly_flagged():
    p=provider(Transport([response(created=None)]))
    batch=p.read_batch()
    assert "MESSAGE_TIMESTAMP_UNAVAILABLE" in batch.quality_flags
    assert all(q.source_ts is None for q in batch.quotes)

def test_market_set_mismatch_fails_whole_snapshot():
    p=provider(Transport([response(market_id=9002)]))
    with pytest.raises(BetdaqSoapProtocolError, match="market set mismatch"):
        p.read_batch()

def test_unexpected_ladder_depth_fails_closed_instead_of_dropping_levels():
    p=provider(Transport([
        response(extra_for='<ForSidePrices Price="2.020" Stake="1.00" />')
    ]))
    with pytest.raises(BetdaqSoapProtocolError, match="deeper ladder"):
        p.read_batch()

def test_transport_request_is_fixed_read_only_bounded_one_level_per_side():
    t=Transport([response()])
    p=provider(t)
    first=p.read_batch(max_items=1)
    req, timeout=t.calls[0]
    assert req.market_ids == (9001,)
    assert req.threshold_amount == Decimal("5.00")
    assert req.number_for_prices_required == 1
    assert req.number_against_prices_required == 1
    assert req.want_market_matched_amount is False
    assert req.want_selections_matched_amounts is False
    assert req.want_selection_matched_details is False
    assert req.request_id == 42
    assert timeout == 10.0
    assert "TRUNCATED_BATCH" in first.quality_flags
    assert not hasattr(p, "place_order")
    assert not hasattr(p, "cancel_order")

def test_max_items_pages_local_snapshot_without_extra_provider_call():
    t=Transport([response()])
    p=provider(t)
    first=p.read_batch(max_items=1)
    second=p.read_batch(max_items=1)
    assert len(t.calls) == 1
    assert len(first.quotes) == len(second.quotes) == 1
    assert first.quotes[0].exchange_side == "back"
    assert second.quotes[0].exchange_side == "lay"
    assert "TRUNCATED_BATCH" in first.quality_flags
    assert "TRUNCATED_BATCH" not in second.quality_flags

def test_retries_only_typed_transient_failures_and_caps_attempts():
    t=Transport([BetdaqTransientTransportError("temporary"), response()])
    p=provider(t, max_attempts=2)
    batch=p.read_batch()
    assert len(batch.quotes) == 2
    assert len(t.calls) == 2
    assert p.last_request_evidence.requests[0].attempts == 2

    t2=Transport([TimeoutError(), TimeoutError(), response()])
    p2=provider(t2, max_attempts=2)
    with pytest.raises(ProviderUnavailableError, match="bounded attempts"):
        p2.read_batch()
    assert len(t2.calls) == 2

def test_schema_and_provider_errors_are_terminal_not_retried():
    t=Transport(["not xml", response()])
    p=provider(t, max_attempts=2)
    with pytest.raises(BetdaqSoapProtocolError):
        p.read_batch()
    assert len(t.calls) == 1

def test_odds_at_or_below_one_fail_canonicalization():
    p=provider(Transport([response(for_price="1.000")]))
    with pytest.raises(BetdaqSoapProtocolError, match="greater than 1"):
        p.read_batch()

def test_response_hash_and_message_evidence_are_durable_strings():
    payload=response()
    p=provider(Transport([payload]))
    batch=p.read_batch()
    ev=p.last_request_evidence
    assert ev is not None
    assert ev.requests[0].request_id == 42
    assert ev.requests[0].market_ids == (9001,)
    assert ev.requests[0].call_id == "call-123"
    assert len(ev.requests[0].request_fingerprint) == 64
    assert len(ev.requests[0].response_sha256) == 64
    assert len(ev.aggregate_sha256) == 64
    assert batch.cursor == ev.aggregate_sha256

def test_status_codes_are_preserved_not_relabelled_as_open():
    p=provider(Transport([response()]))
    q=p.read_batch().quotes[0]
    assert q.status == "betdaq:3"
    assert q.metadata["betdaq_market_status_code"] == 2
    assert q.metadata["betdaq_selection_status_code"] == 3


def _market_xml(market_id: int) -> str:
    return f'''<MarketPrices Id="{market_id}" Name="Match Odds {market_id}" Type="1" IsPlayMarket="false"
      Status="2" StartTime="2026-09-23T18:00:00Z" WithdrawalSequenceNumber="7"
      IsInRunningAllowed="true" IsManagedWhenInRunning="true"
      IsCurrentlyInRunning="false" InRunningDelaySeconds="5">
      <Selections Id="{market_id + 10000}" Name="Selection {market_id}" Status="3" ResetCount="4">
        <ForSidePrices Price="2.00" Stake="12.00" />
        <AgainstSidePrices Price="2.10" Stake="9.00" />
      </Selections>
    </MarketPrices>'''

def _response_for_markets(market_ids: tuple[int, ...]) -> str:
    markets = "".join(_market_xml(value) for value in market_ids)
    return f'''<soap:Envelope xmlns:soap="{SOAP}">
      <soap:Body><GetPricesResponse xmlns="{API}"><GetPricesResult>
        <ReturnStatus Code="0" Description="Success" />
        {markets}
      </GetPricesResult></GetPricesResponse></soap:Body>
    </soap:Envelope>'''

def test_51_markets_are_partitioned_50_plus_1_without_scope_expansion():
    bindings=[
        BetdaqMarketBinding(i, f"event-{i}", "football", MarketType.WINNER)
        for i in range(1, 52)
    ]
    request_ids=iter((100, 101))
    class DynamicTransport:
        def __init__(self):
            self.calls=[]
        def get_prices(self, request, *, timeout_seconds):
            self.calls.append(request)
            return _response_for_markets(request.market_ids)
    t=DynamicTransport()
    p=BetdaqReadOnlyProvider(
        transport=t,
        market_bindings=bindings,
        threshold_amount=Decimal("1"),
        clock=lambda: "2026-09-23T00:00:01Z",
        request_id_factory=lambda: next(request_ids),
    )
    batch=p.read_batch(max_items=1000)
    assert [len(call.market_ids) for call in t.calls] == [50, 1]
    assert t.calls[0].market_ids == tuple(range(1, 51))
    assert t.calls[1].market_ids == (51,)
    assert len(batch.quotes) == 102
    assert len(p.last_request_evidence.requests) == 2

def test_second_chunk_failure_leaks_no_partial_snapshot():
    bindings=[
        BetdaqMarketBinding(i, f"event-{i}", "football", MarketType.WINNER)
        for i in range(1, 52)
    ]
    class FailingSecondChunk:
        def __init__(self):
            self.calls=[]
        def get_prices(self, request, *, timeout_seconds):
            self.calls.append(request)
            if len(self.calls) == 2:
                return "not xml"
            return _response_for_markets(request.market_ids)
    t=FailingSecondChunk()
    p=BetdaqReadOnlyProvider(
        transport=t,
        market_bindings=bindings,
        threshold_amount=Decimal("1"),
        clock=lambda: "2026-09-23T00:00:01Z",
    )
    with pytest.raises(BetdaqSoapProtocolError):
        p.read_batch(max_items=1000)
    assert p.last_request_evidence is None
    batch=p.read_batch(max_items=1000)
    assert [len(call.market_ids) for call in t.calls] == [50, 1, 50, 1]
    assert len(batch.quotes) == 102

def test_live_entitlement_is_never_inferred_from_successful_public_response():
    p=provider(Transport([response()]))
    batch=p.read_batch()
    assert "LIVE_ENTITLEMENT_UNVERIFIED" in batch.quality_flags
    assert p.last_request_evidence.live_entitlement_verified is False


def test_top_of_book_depth_is_explicit_and_never_relabelled_full_ladder():
    p=provider(Transport([response()]))
    batch=p.read_batch()
    assert "FULL_MARKET_SCOPE" in batch.quality_flags
    assert "TOP_OF_BOOK_ONLY" in batch.quality_flags
    assert "FULL_SNAPSHOT" not in batch.quality_flags


def test_injected_transport_never_claims_provider_origin_and_custom_clock_is_flagged():
    p=provider(Transport([response()]))
    batch=p.read_batch()
    ev=p.last_request_evidence
    assert ev.provider_origin_verified is False
    assert ev.receipt_clock_verified is False
    assert "UNVERIFIED_PROVIDER_ORIGIN" in batch.quality_flags
    assert "UNVERIFIED_RECEIPT_CLOCK" in batch.quality_flags


def test_default_product_clock_is_not_marked_unverified_but_origin_still_is():
    # Construction is enough to prove the identity-based clock authority contract;
    # live wall-clock XML is deliberately not fabricated in this deterministic test.
    p=BetdaqReadOnlyProvider(
        transport=Transport([response()]),
        market_bindings=[BetdaqMarketBinding(9001,"event-77","football")],
        threshold_amount=Decimal("1"),
    )
    assert p._receipt_clock_verified is True

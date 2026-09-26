from datetime import datetime, timedelta, timezone

import pytest

from autosport.betfair_multisport_catalog import (
    BetfairEventType,
    BetfairMarketType,
    build_list_event_types_request,
    build_list_market_types_request,
)
from autosport.betfair_discovery_provenance import (
    BetfairDiscoveryProvenanceError,
    BetfairDiscoveryVisibilityScope,
    build_betfair_discovery_acquisition_evidence,
)
import autosport.betfair_discovery_transport_origin as origin


T0 = datetime(2026, 9, 23, 10, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(seconds=1)


def _evidence(*, market_response: bytes = b"market-types"):
    return build_betfair_discovery_acquisition_evidence(
        discovery_run_id="run-transport-origin",
        visibility_scope=BetfairDiscoveryVisibilityScope(
            account_scope_ref="account-A",
            application_scope_ref="application-A",
            key_class="LIVE",
            jurisdiction="UK",
        ),
        event_type_request=build_list_event_types_request(),
        event_type_raw_response=b"event-types",
        event_type_observed_at=T0,
        event_types=(BetfairEventType("1", "Soccer", 1),),
        selected_event_type_id="1",
        market_type_request=build_list_market_types_request(event_type_ids=("1",)),
        market_type_raw_response=market_response,
        market_type_observed_at=T1,
        market_types=(BetfairMarketType("MATCH_ODDS", 1),),
        max_age_seconds=60,
    )


def _receipt(exchange):
    return origin.BetfairDiscoveryTransportOriginReceipt(
        transport_authority_ref="betfair-readonly-session-A",
        method=exchange.method,
        request_sha256=exchange.request_sha256,
        raw_response_sha256=exchange.raw_response_sha256,
        observed_at_utc=exchange.observed_at_utc,
        _issuer_token=origin._TRANSPORT_RECEIPT_ISSUER_TOKEN,
    )


def test_caller_supplied_acquisition_does_not_mint_authenticated_transport_origin():
    evidence = _evidence()

    assessment = origin.assess_betfair_discovery_transport_origin(evidence)

    assert assessment.grants_authenticated_transport_origin_authority is False
    assert assessment.reason == "NO_AUTHENTICATED_TRANSPORT_RECEIPTS"
    assert assessment.exchange_count == 2
    assert assessment.bound_exchange_count == 0
    assert assessment.exchange_raw_response_sha256 == (
        evidence.event_type_exchange.raw_response_sha256,
        evidence.market_type_exchange.raw_response_sha256,
    )
    assert assessment.projection()[
        "grants_authenticated_transport_origin_authority"
    ] is False


def test_authenticated_origin_requirement_fails_closed_without_transport_receipts():
    with pytest.raises(
        BetfairDiscoveryProvenanceError,
        match="authenticated Betfair transport origin is not proven",
    ):
        origin.require_betfair_authenticated_transport_origin(_evidence())


def test_public_receipt_construction_cannot_self_issue_authority():
    exchange = _evidence().event_type_exchange

    with pytest.raises(
        BetfairDiscoveryProvenanceError,
        match="product-issued only",
    ):
        origin.BetfairDiscoveryTransportOriginReceipt(
            transport_authority_ref="caller-asserted-session",
            method=exchange.method,
            request_sha256=exchange.request_sha256,
            raw_response_sha256=exchange.raw_response_sha256,
            observed_at_utc=exchange.observed_at_utc,
            _issuer_token=object(),
        )


def test_internal_receipt_marker_cannot_mint_authenticated_origin():
    evidence = _evidence()
    receipts = (
        _receipt(evidence.event_type_exchange),
        _receipt(evidence.market_type_exchange),
    )

    assessment = origin.assess_betfair_discovery_transport_origin(
        evidence,
        receipts=receipts,
    )

    assert assessment.grants_authenticated_transport_origin_authority is False
    assert assessment.reason == "NO_PRODUCT_OWNED_TRANSPORT_RECEIPT_ISSUER"
    assert assessment.bound_exchange_count == assessment.exchange_count == 2
    with pytest.raises(
        BetfairDiscoveryProvenanceError,
        match="authenticated Betfair transport origin is not proven",
    ):
        origin.require_betfair_authenticated_transport_origin(
            evidence,
            receipts=receipts,
        )

def test_receipt_for_different_raw_response_cannot_authorize_acquisition():
    evidence = _evidence()
    other = _evidence(market_response=b"caller-replaced-market-types")
    receipts = (
        _receipt(evidence.event_type_exchange),
        _receipt(other.market_type_exchange),
    )

    assessment = origin.assess_betfair_discovery_transport_origin(
        evidence,
        receipts=receipts,
    )

    assert assessment.grants_authenticated_transport_origin_authority is False
    assert assessment.reason == "TRANSPORT_RECEIPT_DOES_NOT_BIND_EXACT_EXCHANGE"
    assert assessment.bound_exchange_count == 1


def test_missing_one_transport_receipt_cannot_authorize_partial_chain():
    evidence = _evidence()

    assessment = origin.assess_betfair_discovery_transport_origin(
        evidence,
        receipts=(_receipt(evidence.event_type_exchange),),
    )

    assert assessment.grants_authenticated_transport_origin_authority is False
    assert assessment.reason == "MISSING_AUTHENTICATED_TRANSPORT_RECEIPT"
    assert assessment.bound_exchange_count == 1

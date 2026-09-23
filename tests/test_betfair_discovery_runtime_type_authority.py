from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from autosport.betfair_multisport_catalog import (
    LIST_MARKET_CATALOGUE,
    BetfairCatalogError,
    BetfairCatalogRequest,
    BetfairEventType,
    BetfairMarketType,
    build_list_event_types_request,
    build_list_market_types_request,
    parse_market_catalogue_result_for_request,
)
from autosport.betfair_discovery_provenance import (
    BetfairDiscoveryAcquisitionEvidence,
    BetfairDiscoveryExchange,
    BetfairDiscoveryProvenanceError,
    BetfairDiscoveryVisibilityScope,
    build_betfair_discovery_acquisition_evidence,
)


T0 = datetime(2026, 9, 23, 1, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(seconds=1)


class MutableRpcRequest(BetfairCatalogRequest):
    event_type_id = "1"

    def rpc_params(self) -> dict[str, object]:
        return {"filter": {"eventTypeIds": [type(self).event_type_id]}}


class VisibilityScopeSubclass(BetfairDiscoveryVisibilityScope):
    pass


class ExchangeSubclass(BetfairDiscoveryExchange):
    pass


class EventTypeSubclass(BetfairEventType):
    pass


class MarketTypeSubclass(BetfairMarketType):
    pass


class DateTimeSubclass(datetime):
    pass


class TupleSubclass(tuple):
    pass


def _scope() -> BetfairDiscoveryVisibilityScope:
    return BetfairDiscoveryVisibilityScope(
        account_scope_ref="account-A",
        application_scope_ref="application-A",
        key_class="LIVE",
        jurisdiction="UK",
    )


def _build_evidence(
    *,
    visibility_scope: BetfairDiscoveryVisibilityScope | None = None,
    event_type: BetfairEventType | None = None,
    market_type: BetfairMarketType | None = None,
) -> BetfairDiscoveryAcquisitionEvidence:
    return build_betfair_discovery_acquisition_evidence(
        discovery_run_id="run-runtime-type",
        visibility_scope=visibility_scope or _scope(),
        event_type_request=build_list_event_types_request(),
        event_type_raw_response=b"event-types",
        event_type_observed_at=T0,
        event_types=(event_type or BetfairEventType("1", "Soccer", 1),),
        selected_event_type_id="1",
        market_type_request=build_list_market_types_request(event_type_ids=("1",)),
        market_type_raw_response=b"market-types",
        market_type_observed_at=T1,
        market_types=(market_type or BetfairMarketType("MATCH_ODDS", 1),),
        max_age_seconds=60,
    )


def test_exchange_rejects_request_subclass_before_virtual_dispatch() -> None:
    request = MutableRpcRequest(
        "SportsAPING/v1.0/listMarketTypes",
        {"filter": {"eventTypeIds": ["1"]}},
    )

    with pytest.raises(
        BetfairDiscoveryProvenanceError,
        match="exact canonical BetfairCatalogRequest",
    ):
        BetfairDiscoveryExchange(request, b"[]", T0)


def test_request_bound_catalogue_parse_rejects_request_subclass() -> None:
    request = MutableRpcRequest(
        LIST_MARKET_CATALOGUE,
        {
            "filter": {"eventTypeIds": ["1"]},
            "maxResults": 1,
        },
    )

    with pytest.raises(BetfairCatalogError, match="exact BetfairCatalogRequest"):
        parse_market_catalogue_result_for_request([], request=request)


def test_exchange_snapshots_request_authority_once() -> None:
    request = build_list_market_types_request(event_type_ids=("1",))
    exchange = BetfairDiscoveryExchange(request, b"[]", T0)
    before = (
        exchange.method,
        exchange.canonical_request_json,
        exchange.canonical_filter_json,
        exchange.request_sha256,
        exchange.filter_sha256,
    )

    # Even an explicit frozen-dataclass bypass cannot make already-issued
    # acquisition evidence re-dispatch through a later request projection.
    object.__setattr__(
        request,
        "params",
        MappingProxyType(
            {
                "filter": MappingProxyType(
                    {"eventTypeIds": ("7",)}
                )
            }
        ),
    )

    assert (
        exchange.method,
        exchange.canonical_request_json,
        exchange.canonical_filter_json,
        exchange.request_sha256,
        exchange.filter_sha256,
    ) == before


def test_acquisition_rejects_visibility_scope_subclass() -> None:
    scope = VisibilityScopeSubclass(
        account_scope_ref="account-A",
        application_scope_ref="application-A",
        key_class="LIVE",
        jurisdiction="UK",
    )

    with pytest.raises(
        BetfairDiscoveryProvenanceError,
        match="visibility_scope must be BetfairDiscoveryVisibilityScope",
    ):
        _build_evidence(visibility_scope=scope)


def test_acquisition_rejects_exchange_subclass() -> None:
    event_exchange = ExchangeSubclass(
        build_list_event_types_request(),
        b"event-types",
        T0,
    )
    market_exchange = BetfairDiscoveryExchange(
        build_list_market_types_request(event_type_ids=("1",)),
        b"market-types",
        T1,
    )

    with pytest.raises(
        BetfairDiscoveryProvenanceError,
        match="event_type_exchange must be BetfairDiscoveryExchange",
    ):
        BetfairDiscoveryAcquisitionEvidence(
            discovery_run_id="run-runtime-type",
            visibility_scope=_scope(),
            event_type_exchange=event_exchange,
            event_types=(BetfairEventType("1", "Soccer", 1),),
            selected_event_type_id="1",
            market_type_exchange=market_exchange,
            market_types=(BetfairMarketType("MATCH_ODDS", 1),),
            max_age_seconds=60,
        )


@pytest.mark.parametrize(
    ("event_type", "market_type", "message"),
    [
        (
            EventTypeSubclass("1", "Soccer", 1),
            BetfairMarketType("MATCH_ODDS", 1),
            "event_types must contain canonical BetfairEventType values",
        ),
        (
            BetfairEventType("1", "Soccer", 1),
            MarketTypeSubclass("MATCH_ODDS", 1),
            "market_types must contain canonical BetfairMarketType values",
        ),
    ],
)
def test_acquisition_rejects_provider_identity_subclasses(
    event_type: BetfairEventType,
    market_type: BetfairMarketType,
    message: str,
) -> None:
    with pytest.raises(BetfairDiscoveryProvenanceError, match=message):
        _build_evidence(event_type=event_type, market_type=market_type)

def test_exchange_rejects_datetime_subclass() -> None:
    observed_at = DateTimeSubclass(
        2026,
        9,
        23,
        1,
        0,
        tzinfo=timezone.utc,
    )

    with pytest.raises(
        BetfairDiscoveryProvenanceError,
        match="observed_at must be an exact datetime",
    ):
        BetfairDiscoveryExchange(
            build_list_event_types_request(),
            b"event-types",
            observed_at,
        )


def test_acquisition_rejects_tuple_subclass() -> None:
    event_exchange = BetfairDiscoveryExchange(
        build_list_event_types_request(),
        b"event-types",
        T0,
    )
    market_exchange = BetfairDiscoveryExchange(
        build_list_market_types_request(event_type_ids=("1",)),
        b"market-types",
        T1,
    )

    with pytest.raises(
        BetfairDiscoveryProvenanceError,
        match="event_types must be an exact tuple",
    ):
        BetfairDiscoveryAcquisitionEvidence(
            discovery_run_id="run-runtime-type",
            visibility_scope=_scope(),
            event_type_exchange=event_exchange,
            event_types=TupleSubclass((BetfairEventType("1", "Soccer", 1),)),
            selected_event_type_id="1",
            market_type_exchange=market_exchange,
            market_types=(BetfairMarketType("MATCH_ODDS", 1),),
            max_age_seconds=60,
        )


"""Transport-free, provider-native Betfair multi-sport catalogue discovery."""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Sequence


class BetfairCatalogError(ValueError):
    """Discovery request/result is not safe to accept."""


LIST_EVENT_TYPES = "SportsAPING/v1.0/listEventTypes"
LIST_EVENTS = "SportsAPING/v1.0/listEvents"
LIST_MARKET_TYPES = "SportsAPING/v1.0/listMarketTypes"
LIST_MARKET_CATALOGUE = "SportsAPING/v1.0/listMarketCatalogue"
DISCOVERY_METHODS = frozenset(
    {LIST_EVENT_TYPES, LIST_EVENTS, LIST_MARKET_TYPES, LIST_MARKET_CATALOGUE}
)


@dataclass(frozen=True, slots=True)
class BetfairCatalogRequest:
    method: str
    params: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.method not in DISCOVERY_METHODS:
            raise BetfairCatalogError("method is outside the read-only discovery allowlist")
        if not isinstance(self.params, Mapping):
            raise BetfairCatalogError("params must be a mapping")
        object.__setattr__(self, "params", MappingProxyType(dict(self.params)))


@dataclass(frozen=True, slots=True)
class BetfairEventType:
    event_type_id: str
    display_name: str
    market_count: int

    def __post_init__(self) -> None:
        _provider_id(self.event_type_id, "event_type_id")
        _text(self.display_name, "display_name")
        _nonnegative_int(self.market_count, "market_count")


@dataclass(frozen=True, slots=True)
class BetfairEvent:
    event_id: str
    display_name: str
    market_count: int
    country_code: str | None = None
    timezone_name: str | None = None
    open_date: str | None = None

    def __post_init__(self) -> None:
        _provider_id(self.event_id, "event_id")
        _text(self.display_name, "display_name")
        _nonnegative_int(self.market_count, "market_count")
        for value, field in (
            (self.country_code, "country_code"),
            (self.timezone_name, "timezone_name"),
            (self.open_date, "open_date"),
        ):
            _optional_text(value, field)


@dataclass(frozen=True, slots=True)
class BetfairMarketType:
    market_type_code: str
    market_count: int

    def __post_init__(self) -> None:
        _provider_code(self.market_type_code, "market_type_code")
        _nonnegative_int(self.market_count, "market_count")


@dataclass(frozen=True, slots=True)
class BetfairCatalogMarket:
    market_id: str
    event_type_id: str
    event_id: str
    market_name: str
    market_start_time: str
    market_type_code: str | None = None

    def __post_init__(self) -> None:
        for value, field in (
            (self.market_id, "market_id"),
            (self.event_type_id, "event_type_id"),
            (self.event_id, "event_id"),
        ):
            _provider_id(value, field)
        _text(self.market_name, "market_name")
        _text(self.market_start_time, "market_start_time")
        if self.market_type_code is not None:
            _provider_code(self.market_type_code, "market_type_code")


@dataclass(frozen=True, slots=True)
class BetfairMarketCatalogueBatch:
    markets: tuple[BetfairCatalogMarket, ...]
    requested_max_results: int
    continuation_required: bool

    def __post_init__(self) -> None:
        _max_results(self.requested_max_results)
        if not isinstance(self.markets, tuple):
            raise BetfairCatalogError("markets must be a tuple")
        if not isinstance(self.continuation_required, bool):
            raise BetfairCatalogError("continuation_required must be boolean")
        if len(self.markets) > self.requested_max_results:
            raise BetfairCatalogError("catalogue returned more rows than requested")
        if self.continuation_required != (len(self.markets) == self.requested_max_results):
            raise BetfairCatalogError("continuation flag does not match batch cardinality")

    @property
    def completeness_proven(self) -> bool:
        """True only when this exact filter slice returned fewer rows than its limit."""
        return not self.continuation_required


def build_list_event_types_request(
    *, market_filter: Mapping[str, object] | None = None
) -> BetfairCatalogRequest:
    if market_filter is not None and not isinstance(market_filter, Mapping):
        raise BetfairCatalogError("market_filter must be a mapping")
    return BetfairCatalogRequest(LIST_EVENT_TYPES, {"filter": dict(market_filter or {})})


def _scoped_r
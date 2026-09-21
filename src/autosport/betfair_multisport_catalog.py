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


def _scoped_request(method: str, event_type_ids: Sequence[str]) -> BetfairCatalogRequest:
    ids = _unique_ids(event_type_ids, "event_type_ids")
    return BetfairCatalogRequest(method, {"filter": {"eventTypeIds": list(ids)}})


def build_list_events_request(*, event_type_ids: Sequence[str]) -> BetfairCatalogRequest:
    return _scoped_request(LIST_EVENTS, event_type_ids)


def build_list_market_types_request(*, event_type_ids: Sequence[str]) -> BetfairCatalogRequest:
    return _scoped_request(LIST_MARKET_TYPES, event_type_ids)


def build_list_market_catalogue_request(
    *,
    event_type_ids: Sequence[str],
    market_type_codes: Sequence[str] = (),
    max_results: int = 1000,
    market_start_from: str | None = None,
    market_start_to: str | None = None,
) -> BetfairCatalogRequest:
    ids = _unique_ids(event_type_ids, "event_type_ids")
    codes = _unique_codes(market_type_codes, "market_type_codes")
    market_filter: dict[str, object] = {"eventTypeIds": list(ids)}
    if codes:
        market_filter["marketTypeCodes"] = list(codes)
    if market_start_from is not None or market_start_to is not None:
        time_range: dict[str, str] = {}
        if market_start_from is not None:
            time_range["from"] = _text(market_start_from, "market_start_from")
        if market_start_to is not None:
            time_range["to"] = _text(market_start_to, "market_start_to")
        market_filter["marketStartTime"] = time_range
    return BetfairCatalogRequest(
        LIST_MARKET_CATALOGUE,
        {
            "filter": market_filter,
            "marketProjection": [
                "EVENT",
                "EVENT_TYPE",
                "MARKET_DESCRIPTION",
                "MARKET_START_TIME",
            ],
            "sort": "FIRST_TO_START",
            "maxResults": _max_results(max_results),
        },
    )


def parse_event_types_result(result: object) -> tuple[BetfairEventType, ...]:
    items = []
    for index, raw in enumerate(_rows(result, "listEventTypes result")):
        row = _mapping(raw, f"listEventTypes[{index}]")
        event_type = _mapping(row.get("eventType"), f"listEventTypes[{index}].eventType")
        items.append(
            BetfairEventType(
                _provider_text(event_type, "id", "event_type_id"),
                _provider_text(event_type, "name", "event_type_name"),
                _provider_count(row, "marketCount", "market_count"),
            )
        )
    _reject_duplicate((item.event_type_id for item in items), "eventType id", provider_result=True)
    return tuple(items)


def parse_events_result(result: object) -> tuple[BetfairEvent, ...]:
    items = []
    for index, raw in enumerate(_rows(result, "listEvents result")):
        row = _mapping(raw, f"listEvents[{index}]")
        event = _mapping(row.get("event"), f"listEvents[{index}].event")
        items.append(
            BetfairEvent(
                _provider_text(event, "id", "event_id"),
                _provider_text(event, "name", "event_name"),
                _provider_count(row, "marketCount", "market_count"),
                _optional_provider_text(event, "countryCode", "country_code"),
                _optional_provider_text(event, "timezone", "timezone_name"),
                _optional_provider_text(event, "openDate", "open_date"),
            )
        )
    _reject_duplicate((item.event_id for item in items), "event id", provider_result=True)
    return tuple(items)


def parse_market_types_result(result: object) -> tuple[BetfairMarketType, ...]:
    items = []
    for index, raw in enumerate(_rows(result, "listMarketTypes result")):
        row = _mapping(raw, f"listMarketTypes[{index}]")
        items.append(
            BetfairMarketType(
                _provider_text(row, "marketType", "market_type_code"),
                _provider_count(row, "marketCount", "market_count"),
            )
        )
    _reject_duplicate((item.market_type_code for item in items), "marketType code", provider_result=True)
    return tuple(items)


def parse_market_catalogue_result(
    result: object,
    *,
    requested_event_type_ids: Sequence[str],
    requested_max_results: int,
) -> BetfairMarketCatalogueBatch:
    allowed = frozenset(_unique_ids(requested_event_type_ids, "requested_event_type_ids"))
    limit = _max_results(requested_max_results)
    rows = _rows(result, "listMarketCatalogue result")
    if len(rows) > limit:
        raise BetfairCatalogError("listMarketCatalogue returned more rows than requested")
    items = []
    for index, raw in enumerate(rows):
        row = _mapping(raw, f"listMarketCatalogue[{index}]")
        event_type = _mapping(row.get("eventType"), f"listMarketCatalogue[{index}].eventType")
        event_type_id = _provider_text(event_type, "id", "event_type_id")
        if event_type_id not in allowed:
            raise BetfairCatalogError("catalogue row escaped the requested eventType scope")
        event = _mapping(row.get("event"), f"listMarketCatalogue[{index}].event")
        description = row.get("description")
        market_type = None
        if description is not None:
            market_type = _optional_provider_text(
                _mapping(description, f"listMarketCatalogue[{index}].description"),
                "marketType",
                "market_type_code",
            )
        items.append(
            BetfairCatalogMarket(
                _provider_text(row, "marketId", "market_id"),
                event_type_id,
                _provider_text(event, "id", "event_id"),
                _provider_text(row, "marketName", "market_name"),
                _provider_text(row, "marketStartTime", "market_start_time"),
                market_type,
            )
        )
    _reject_duplicate((item.market_id for item in items), "marketId", provider_result=True)
    return BetfairMarketCatalogueBatch(tuple(items), limit, len(items) == limit)


def _rows(value: object, field: str) -> list[object]:
    if not isinstance(value, list):
        raise BetfairCatalogError(f"{field} must be a list")
    return value


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise BetfairCatalogError(f"{field} must be an object")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise BetfairCatalogError(f"{field} must be a non-empty trimmed string")
    return value


def _optional_text(value: object, field: str) -> str | None:
    return None if value is None else _text(value, field)


def _provider_text(row: Mapping[str, object], key: str, field: str) -> str:
    return _text(row.get(key), field)


def _optional_provider_text(row: Mapping[str, object], key: str, field: str) -> str | None:
    return _optional_text(row.get(key), field)


def _provider_id(value: object, field: str) -> str:
    value = _text(value, field)
    if any(ord(ch) < 33 or ord(ch) > 126 for ch in value):
        raise BetfairCatalogError(f"{field} must be printable ASCII provider identity")
    return value


def _provider_code(value: object, field: str) -> str:
    value = _provider_id(value, field)
    if value != value.upper():
        raise BetfairCatalogError(f"{field} must be an uppercase provider code")
    return value


def _unique_ids(values: Sequence[str], field: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise BetfairCatalogError(f"{field} must be a sequence")
    values = tuple(_provider_id(value, field) for value in values)
    if not values:
        raise BetfairCatalogError(f"{field} must not be empty")
    _reject_duplicate(iter(values), field)
    return values


def _unique_codes(values: Sequence[str], field: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise BetfairCatalogError(f"{field} must be a sequence")
    values = tuple(_provider_code(value, field) for value in values)
    _reject_duplicate(iter(values), field)
    return values


def _reject_duplicate(values, field: str, *, provider_result: bool = False) -> None:
    seen = set()
    for value in values:
        if value in seen:
            if provider_result:
                raise BetfairCatalogError(f"duplicate {field} in provider result")
            raise BetfairCatalogError(f"{field} must not contain duplicates")
        seen.add(value)


def _nonnegative_int(value: object, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise BetfairCatalogError(f"{field} must be a non-negative integer")
    return value


def _provider_count(row: Mapping[str, object], key: str, field: str) -> int:
    return _nonnegative_int(row.get(key), field)


def _max_results(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 1000:
        raise BetfairCatalogError("max_results must be an integer in 1..1000")
    return value

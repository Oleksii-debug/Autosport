"""Transport-free, provider-native Betfair multi-sport catalogue discovery."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from types import MappingProxyType
from typing import Mapping, Sequence


class BetfairCatalogError(ValueError):
    """Discovery request/result is not safe to accept."""


LIST_EVENT_TYPES = "SportsAPING/v1.0/listEventTypes"
LIST_COMPETITIONS = "SportsAPING/v1.0/listCompetitions"
LIST_EVENTS = "SportsAPING/v1.0/listEvents"
LIST_MARKET_TYPES = "SportsAPING/v1.0/listMarketTypes"
LIST_MARKET_CATALOGUE = "SportsAPING/v1.0/listMarketCatalogue"
DISCOVERY_METHODS = frozenset(
    {
        LIST_EVENT_TYPES,
        LIST_COMPETITIONS,
        LIST_EVENTS,
        LIST_MARKET_TYPES,
        LIST_MARKET_CATALOGUE,
    }
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
        frozen = _deep_freeze_mapping(self.params, "params")
        market_filter = frozen.get("filter")
        if not isinstance(market_filter, Mapping):
            raise BetfairCatalogError("params.filter must be a mapping")
        if self.method == LIST_MARKET_CATALOGUE:
            if "maxResults" not in frozen:
                raise BetfairCatalogError(
                    "listMarketCatalogue params must include maxResults"
                )
            _max_results(frozen["maxResults"])
        object.__setattr__(self, "params", frozen)

    def rpc_params(self) -> dict[str, object]:
        """Return a detached JSON-compatible copy for the authenticated RPC boundary."""
        return _deep_thaw_mapping(self.params)


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
class BetfairCompetition:
    competition_id: str
    display_name: str
    market_count: int

    def __post_init__(self) -> None:
        _provider_id(self.competition_id, "competition_id")
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
    competition_id: str | None = None

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
        if self.competition_id is not None:
            _provider_id(self.competition_id, "competition_id")


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
    def response_not_limit_saturated(self) -> bool:
        """Whether the response returned fewer rows than the requested maxResults."""
        return not self.continuation_required

    @property
    def completeness_proven(self) -> bool:
        """Never upgrade one catalogue response to proof of the full market universe."""
        return False


def build_list_event_types_request(
    *, market_filter: Mapping[str, object] | None = None
) -> BetfairCatalogRequest:
    if market_filter is not None and not isinstance(market_filter, Mapping):
        raise BetfairCatalogError("market_filter must be a mapping")
    return BetfairCatalogRequest(LIST_EVENT_TYPES, {"filter": dict(market_filter or {})})


def _scoped_request(
    method: str,
    event_type_ids: Sequence[str],
    competition_ids: Sequence[str] = (),
) -> BetfairCatalogRequest:
    ids = _unique_ids(event_type_ids, "event_type_ids")
    competitions = _optional_unique_ids(competition_ids, "competition_ids")
    market_filter: dict[str, object] = {"eventTypeIds": list(ids)}
    if competitions:
        market_filter["competitionIds"] = list(competitions)
    return BetfairCatalogRequest(method, {"filter": market_filter})


def build_list_competitions_request(
    *, event_type_ids: Sequence[str]
) -> BetfairCatalogRequest:
    return _scoped_request(LIST_COMPETITIONS, event_type_ids)


def build_list_events_request(
    *,
    event_type_ids: Sequence[str],
    competition_ids: Sequence[str] = (),
) -> BetfairCatalogRequest:
    return _scoped_request(LIST_EVENTS, event_type_ids, competition_ids)


def build_list_market_types_request(
    *,
    event_type_ids: Sequence[str],
    competition_ids: Sequence[str] = (),
) -> BetfairCatalogRequest:
    return _scoped_request(LIST_MARKET_TYPES, event_type_ids, competition_ids)


def build_list_market_catalogue_request(
    *,
    event_type_ids: Sequence[str],
    competition_ids: Sequence[str] = (),
    event_ids: Sequence[str] = (),
    market_type_codes: Sequence[str] = (),
    max_results: int = 1000,
    market_start_from: str | None = None,
    market_start_to: str | None = None,
) -> BetfairCatalogRequest:
    ids = _unique_ids(event_type_ids, "event_type_ids")
    scoped_competition_ids = _optional_unique_ids(competition_ids, "competition_ids")
    scoped_event_ids = _optional_unique_ids(event_ids, "event_ids")
    codes = _unique_codes(market_type_codes, "market_type_codes")
    market_filter: dict[str, object] = {"eventTypeIds": list(ids)}
    if scoped_competition_ids:
        market_filter["competitionIds"] = list(scoped_competition_ids)
    if scoped_event_ids:
        market_filter["eventIds"] = list(scoped_event_ids)
    if codes:
        market_filter["marketTypeCodes"] = list(codes)
    _time_range(
        market_start_from,
        market_start_to,
        from_field="market_start_from",
        to_field="market_start_to",
    )
    if market_start_from is not None or market_start_to is not None:
        time_range: dict[str, str] = {}
        if market_start_from is not None:
            time_range["from"] = market_start_from
        if market_start_to is not None:
            time_range["to"] = market_start_to
        market_filter["marketStartTime"] = time_range
    market_projection = [
        "EVENT",
        "EVENT_TYPE",
        "MARKET_DESCRIPTION",
        "MARKET_START_TIME",
    ]
    if scoped_competition_ids:
        market_projection.insert(0, "COMPETITION")
    return BetfairCatalogRequest(
        LIST_MARKET_CATALOGUE,
        {
            "filter": market_filter,
            "marketProjection": market_projection,
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
    _reject_duplicate(
        (item.event_type_id for item in items), "eventType id", provider_result=True
    )
    return tuple(items)


def parse_competitions_result(result: object) -> tuple[BetfairCompetition, ...]:
    items = []
    for index, raw in enumerate(_rows(result, "listCompetitions result")):
        row = _mapping(raw, f"listCompetitions[{index}]")
        competition = _mapping(
            row.get("competition"), f"listCompetitions[{index}].competition"
        )
        items.append(
            BetfairCompetition(
                _provider_text(competition, "id", "competition_id"),
                _provider_text(competition, "name", "competition_name"),
                _provider_count(row, "marketCount", "market_count"),
            )
        )
    _reject_duplicate(
        (item.competition_id for item in items),
        "competition id",
        provider_result=True,
    )
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
    _reject_duplicate(
        (item.market_type_code for item in items), "marketType code", provider_result=True
    )
    return tuple(items)


def parse_market_catalogue_result(
    result: object,
    *,
    requested_event_type_ids: Sequence[str],
    requested_max_results: int,
    requested_competition_ids: Sequence[str] = (),
    requested_event_ids: Sequence[str] = (),
    requested_market_type_codes: Sequence[str] = (),
    requested_market_start_from: str | None = None,
    requested_market_start_to: str | None = None,
) -> BetfairMarketCatalogueBatch:
    allowed = frozenset(_unique_ids(requested_event_type_ids, "requested_event_type_ids"))
    allowed_competitions = frozenset(
        _optional_unique_ids(requested_competition_ids, "requested_competition_ids")
    )
    allowed_events = frozenset(
        _optional_unique_ids(requested_event_ids, "requested_event_ids")
    )
    allowed_market_types = frozenset(
        _unique_codes(requested_market_type_codes, "requested_market_type_codes")
    )
    start_from, start_to = _time_range(
        requested_market_start_from,
        requested_market_start_to,
        from_field="requested_market_start_from",
        to_field="requested_market_start_to",
    )
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
        competition_raw = row.get("competition")
        competition_id = None
        if competition_raw is not None:
            competition = _mapping(
                competition_raw, f"listMarketCatalogue[{index}].competition"
            )
            competition_id = _provider_text(competition, "id", "competition_id")
        if allowed_competitions and competition_id not in allowed_competitions:
            raise BetfairCatalogError(
                "catalogue row escaped the requested competition scope"
            )
        event = _mapping(row.get("event"), f"listMarketCatalogue[{index}].event")
        event_id = _provider_text(event, "id", "event_id")
        if allowed_events and event_id not in allowed_events:
            raise BetfairCatalogError("catalogue row escaped the requested event scope")
        description = row.get("description")
        market_type = None
        if description is not None:
            market_type = _optional_provider_text(
                _mapping(description, f"listMarketCatalogue[{index}].description"),
                "marketType",
                "market_type_code",
            )
        if allowed_market_types and market_type not in allowed_market_types:
            raise BetfairCatalogError(
                "catalogue row escaped the requested marketType scope"
            )
        market_start_time = _provider_text(
            row, "marketStartTime", "market_start_time"
        )
        market_start = _provider_datetime(market_start_time, "market_start_time")
        if start_from is not None and market_start < start_from:
            raise BetfairCatalogError(
                "catalogue row escaped the requested market start time window"
            )
        if start_to is not None and market_start > start_to:
            raise BetfairCatalogError(
                "catalogue row escaped the requested market start time window"
            )
        items.append(
            BetfairCatalogMarket(
                _provider_text(row, "marketId", "market_id"),
                event_type_id,
                event_id,
                _provider_text(row, "marketName", "market_name"),
                market_start_time,
                market_type,
                competition_id,
            )
        )
    _reject_duplicate((item.market_id for item in items), "marketId", provider_result=True)
    return BetfairMarketCatalogueBatch(tuple(items), limit, len(items) == limit)


def parse_market_catalogue_result_for_request(
    result: object,
    *,
    request: BetfairCatalogRequest,
) -> BetfairMarketCatalogueBatch:
    """Parse one catalogue response against the exact issued request scope.

    This is the positive composition seam for downstream catalogue-coverage
    authority. Scope is derived from the immutable request object rather than
    repeated caller-supplied requested-scope assertions.
    """

    if not isinstance(request, BetfairCatalogRequest):
        raise BetfairCatalogError("request must be BetfairCatalogRequest")
    if request.method != LIST_MARKET_CATALOGUE:
        raise BetfairCatalogError("request must target listMarketCatalogue")

    params = request.rpc_params()
    market_filter = _mapping(
        params.get("filter"),
        "listMarketCatalogue request.params.filter",
    )

    event_type_ids = market_filter.get("eventTypeIds", ())
    competition_ids = market_filter.get("competitionIds", ())
    event_ids = market_filter.get("eventIds", ())
    market_type_codes = market_filter.get("marketTypeCodes", ())

    market_start_from = None
    market_start_to = None
    time_range_raw = market_filter.get("marketStartTime")
    if time_range_raw is not None:
        time_range = _mapping(
            time_range_raw,
            "listMarketCatalogue request.params.filter.marketStartTime",
        )
        unexpected = set(time_range) - {"from", "to"}
        if unexpected:
            raise BetfairCatalogError(
                "marketStartTime request contains unsupported fields"
            )
        market_start_from = time_range.get("from")
        market_start_to = time_range.get("to")

    return parse_market_catalogue_result(
        result,
        requested_event_type_ids=event_type_ids,
        requested_max_results=params.get("maxResults"),
        requested_competition_ids=competition_ids,
        requested_event_ids=event_ids,
        requested_market_type_codes=market_type_codes,
        requested_market_start_from=market_start_from,
        requested_market_start_to=market_start_to,
    )


def _deep_freeze_mapping(value: Mapping[str, object], field: str) -> Mapping[str, object]:
    frozen: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key:
            raise BetfairCatalogError(f"{field} keys must be non-empty strings")
        frozen[key] = _deep_freeze_value(item, f"{field}.{key}")
    return MappingProxyType(frozen)


def _deep_freeze_value(value: object, field: str) -> object:
    if isinstance(value, Mapping):
        return _deep_freeze_mapping(value, field)
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze_value(item, field) for item in value)
    if isinstance(value, float):
        if not isfinite(value):
            raise BetfairCatalogError(f"{field} contains a non-finite JSON number")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise BetfairCatalogError(f"{field} contains a non-JSON-compatible value")


def _deep_thaw_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return {key: _deep_thaw_value(item) for key, item in value.items()}


def _deep_thaw_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _deep_thaw_mapping(value)
    if isinstance(value, tuple):
        return [_deep_thaw_value(item) for item in value]
    return value


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


def _provider_datetime(value: object, field: str) -> datetime:
    text = _text(value, field)
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise BetfairCatalogError(f"{field} must be an ISO-8601 date-time") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairCatalogError(f"{field} must include a timezone offset")
    return parsed


def _time_range(
    start: str | None,
    end: str | None,
    *,
    from_field: str,
    to_field: str,
) -> tuple[datetime | None, datetime | None]:
    parsed_start = None if start is None else _provider_datetime(start, from_field)
    parsed_end = None if end is None else _provider_datetime(end, to_field)
    if parsed_start is not None and parsed_end is not None and parsed_start > parsed_end:
        raise BetfairCatalogError(f"{from_field} must not be after {to_field}")
    return parsed_start, parsed_end


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


def _optional_unique_ids(values: Sequence[str], field: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise BetfairCatalogError(f"{field} must be a sequence")
    if not values:
        return ()
    return _unique_ids(values, field)


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

from __future__ import annotations

"""Transport-agnostic, read-only Betfair multi-sport catalogue discovery."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
import hashlib
import json
from typing import Callable, Mapping

OPS = {"listEventTypes", "listEvents", "listMarketTypes", "listMarketCatalogue"}
RESERVED = {"eventTypeIds", "eventIds", "marketTypeCodes"}
PROJECTION = ["EVENT", "EVENT_TYPE", "MARKET_START_TIME"]


def _text(v: object, name: str) -> str:
    if type(v) is not str or not v or v != v.strip():
        raise ValueError(f"{name} must be a non-empty canonical string")
    return v


def _time(v: object, name: str) -> datetime:
    s = _text(v, name)
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be ISO-8601") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{name} must include timezone")
    return dt


def _json(v: object) -> str:
    try:
        return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("value must be JSON data") from exc


def _digest(v: object) -> str:
    return hashlib.sha256(_json(v).encode()).hexdigest()


def _object(v: Mapping[str, object] | None, name: str) -> dict[str, object]:
    if v is None:
        return {}
    if not isinstance(v, Mapping):
        raise TypeError(f"{name} must be a mapping")
    out = json.loads(_json(dict(v)))
    if type(out) is not dict:
        raise ValueError(f"{name} must be an object")
    return out


def _rows(v: object, op: str) -> tuple[dict[str, object], ...]:
    if type(v) not in (list, tuple):
        raise ValueError(f"{op} result must be a list")
    out = []
    for row in v:
        if type(row) is not dict:
            raise ValueError(f"{op} rows must be objects")
        out.append(json.loads(_json(row)))
    return tuple(out)


@dataclass(frozen=True, slots=True)
class BetfairCatalogueResponse:
    payload: object
    request_id: str
    response_sha256: str
    requested_at: str
    received_at: str

    def __post_init__(self) -> None:
        _text(self.request_id, "request_id")
        h = _text(self.response_sha256, "response_sha256").lower()
        if len(h) != 64 or any(c not in "0123456789abcdef" for c in h):
            raise ValueError("response_sha256 must be SHA-256 hex")
        if _time(self.received_at, "received_at") < _time(self.requested_at, "requested_at"):
            raise ValueError("received_at precedes requested_at")
        object.__setattr__(self, "payload", _rows(self.payload, "provider"))


@dataclass(frozen=True, slots=True)
class MarketIdentity:
    event_type_id: str
    event_id: str
    market_id: str
    market_type_code: str
    event_type_name: str
    event_name: str
    market_name: str
    market_start_time: str | None

    def __post_init__(self) -> None:
        for n in ("event_type_id", "event_id", "market_id", "market_type_code", "event_type_name", "event_name", "market_name"):
            _text(getattr(self, n), n)
        if not self.market_id.startswith("1."):
            raise ValueError("invalid Betfair market_id")
        if self.market_start_time is not None:
            _time(self.market_start_time, "market_start_time")

    @property
    def canonical(self) -> dict[str, str]:
        return {"provider": "BETFAIR_EXCHANGE", "event_type_id": self.event_type_id,
                "event_id": self.event_id, "market_id": self.market_id,
                "market_type_code": self.market_type_code}

    @property
    def identity_sha256(self) -> str:
        return _digest(self.canonical)


class Completeness(StrEnum):
    QUERY_SCOPE_COMPLETE = "QUERY_SCOPE_COMPLETE"
    TRUNCATED_OR_UNKNOWN = "TRUNCATED_OR_UNKNOWN"


@dataclass(frozen=True, slots=True)
class CatalogueInventory:
    markets: tuple[MarketIdentity, ...]
    observations: tuple[dict[str, object], ...]
    completeness: Completeness
    incomplete_partitions: tuple[str, ...]

    @property
    def semantic_market_set_sha256(self) -> str:
        return _digest([m.canonical for m in self.markets])

    @property
    def truth_boundary(self) -> dict[str, bool]:
        return {"global_market_absence_authority": False, "closed_market_authority": False,
                "settlement_authority": False, "execution_authority": False,
                "accepted_price_authority": False, "real_money_execution": False}


Fetch = Callable[[str, Mapping[str, object]], BetfairCatalogueResponse]


class BetfairCatalogueDiscovery:
    def __init__(self, fetch: Fetch, *, locale: str | None = None,
                 base_filter: Mapping[str, object] | None = None, max_results: int = 1000) -> None:
        if not callable(fetch):
            raise TypeError("fetch must be callable")
        if locale is not None:
            _text(locale, "locale")
        if isinstance(max_results, bool) or type(max_results) is not int or not 1 <= max_results <= 1000:
            raise ValueError("max_results must be 1..1000")
        self.fetch, self.locale, self.max_results = fetch, locale, max_results
        self.base_filter = _object(base_filter, "base_filter")
        collision = RESERVED & self.base_filter.keys()
        if collision:
            raise ValueError("base_filter overrides discovery hierarchy: " + ",".join(sorted(collision)))
        self.obs: list[dict[str, object]] = []
        self.request_ids: dict[str, str] = {}
        self.latest_received: datetime | None = None

    def _params(self, filt: Mapping[str, object], **extra: object) -> dict[str, object]:
        p: dict[str, object] = {"filter": _object(filt, "filter"), **extra}
        if self.locale is not None:
            p["locale"] = self.locale
        return p

    def _call(self, op: str, params: Mapping[str, object]) -> tuple[dict[str, object], ...]:
        if op not in OPS:
            raise ValueError("unsupported catalogue operation")
        r = self.fetch(op, _object(params, "params"))
        if type(r) is not BetfairCatalogueResponse:
            raise TypeError("fetch must return BetfairCatalogueResponse")
        received = _time(r.received_at, "received_at")
        if self.latest_received is not None and received < self.latest_received:
            raise ValueError("catalogue evidence time moved backwards")
        self.latest_received = received
        h = r.response_sha256.lower()
        if r.request_id in self.request_ids:
            if self.request_ids[r.request_id] != h:
                raise ValueError("request_id reused for conflicting response bytes")
            raise ValueError("request_id reused within inventory")
        self.request_ids[r.request_id] = h
        rows = _rows(r.payload, op)
        self.obs.append({"operation": op, "params": _object(params, "params"),
                         "request_id": r.request_id, "response_sha256": h,
                         "requested_at": r.requested_at, "received_at": r.received_at,
                         "result_count": len(rows)})
        return rows

    def discover(self) -> CatalogueInventory:
        if self.obs:
            raise RuntimeError("discovery instance is single-use")
        types: dict[str, str] = {}
        for row in self._call("listEventTypes", self._params(self.base_filter)):
            x = row.get("eventType")
            if type(x) is not dict:
                raise ValueError("listEventTypes row lacks eventType")
            i, n = _text(x.get("id"), "eventType.id"), _text(x.get("name"), "eventType.name")
            if i in types and types[i] != n:
                raise ValueError("conflicting eventType id")
            types[i] = n

        markets: dict[str, MarketIdentity] = {}
        event_owner: dict[str, str] = {}
        incomplete: set[str] = set()
        for tid, tname in sorted(types.items()):
            events: dict[str, str] = {}
            ef = {**self.base_filter, "eventTypeIds": [tid]}
            for row in self._call("listEvents", self._params(ef)):
                x = row.get("event")
                if type(x) is not dict:
                    raise ValueError("listEvents row lacks event")
                eid, ename = _text(x.get("id"), "event.id"), _text(x.get("name"), "event.name")
                if eid in event_owner and event_owner[eid] != tid:
                    raise ValueError("event id crossed event types")
                event_owner[eid] = tid
                if eid in events and events[eid] != ename:
                    raise ValueError("conflicting event id")
                events[eid] = ename
            for eid, ename in sorted(events.items()):
                tf = {**self.base_filter, "eventTypeIds": [tid], "eventIds": [eid]}
                codes = set()
                for row in self._call("listMarketTypes", self._params(tf)):
                    codes.add(_text(row.get("marketType"), "marketType"))
                for code in sorted(codes):
                    cf = {**tf, "marketTypeCodes": [code]}
                    params = self._params(cf, marketProjection=PROJECTION,
                                          sort="FIRST_TO_START", maxResults=self.max_results)
                    rows = self._call("listMarketCatalogue", params)
                    if len(rows) >= self.max_results:
                        incomplete.add(_digest({"event_type_id": tid, "event_id": eid,
                                               "market_type_code": code, "filter": cf}))
                    for row in rows:
                        et, ev = row.get("eventType"), row.get("event")
                        if type(et) is not dict or type(ev) is not dict:
                            raise ValueError("MarketCatalogue lacks EVENT/EVENT_TYPE projection")
                        if _text(et.get("id"), "catalogue.eventType.id") != tid or _text(ev.get("id"), "catalogue.event.id") != eid:
                            raise ValueError("MarketCatalogue identity mismatches partition")
                        start = row.get("marketStartTime")
                        if start is not None:
                            start = _text(start, "marketStartTime"); _time(start, "marketStartTime")
                        m = MarketIdentity(tid, eid, _text(row.get("marketId"), "marketId"), code,
                                           _text(et.get("name", tname), "eventType.name"),
                                           _text(ev.get("name", ename), "event.name"),
                                           _text(row.get("marketName"), "marketName"), start)
                        old = markets.get(m.market_id)
                        if old is not None and old != m:
                            raise ValueError("conflicting duplicate marketId")
                        markets[m.market_id] = m
        ordered = tuple(sorted(markets.values(), key=lambda m: m.identity_sha256))
        parts = tuple(sorted(incomplete))
        return CatalogueInventory(ordered, tuple(self.obs),
            Completeness.TRUNCATED_OR_UNKNOWN if parts else Completeness.QUERY_SCOPE_COMPLETE, parts)


__all__ = ["BetfairCatalogueDiscovery", "BetfairCatalogueResponse", "CatalogueInventory",
           "Completeness", "MarketIdentity", "Fetch"]

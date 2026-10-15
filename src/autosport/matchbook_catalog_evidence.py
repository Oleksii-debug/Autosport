"""Fail-closed evidence for Matchbook's live paged sports/events/markets catalog.

No network I/O or provider-write authority lives here. A contiguous crawl remains a
series of live observations, never an atomic/complete catalog snapshot.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
from typing import Any, Mapping, Sequence

_I64_MAX = (1 << 63) - 1
_MAX_PAGE = 1000
_MAX_RAW = 16 * 1024 * 1024
_HEX = frozenset("0123456789abcdef")


class MatchbookCatalogEvidenceError(ValueError):
    pass


class CatalogResource(StrEnum):
    SPORTS = "sports"
    EVENTS = "events"
    MARKETS = "markets"


_ALLOWED_FILTERS = {
    CatalogResource.SPORTS: frozenset({"order", "status"}),
    CatalogResource.EVENTS: frozenset(
        {"after", "before", "category-ids", "ids", "sport-ids", "states", "tag-url-names"}
    ),
    CatalogResource.MARKETS: frozenset({"names", "states", "types"}),
}


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def _digest(value: object) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or any(ord(c) < 32 for c in value):
        raise MatchbookCatalogEvidenceError(f"{field} must be non-empty trimmed text")
    if len(value.encode("utf-8")) > 4096:
        raise MatchbookCatalogEvidenceError(f"{field} is too large")
    return value


def _id(value: object, field: str) -> str:
    if type(value) is int:
        parsed = value
    elif type(value) is str and value and value.isascii() and value.isdigit() and (len(value) == 1 or value[0] != "0"):
        parsed = int(value)
    else:
        raise MatchbookCatalogEvidenceError(f"{field} must be a canonical positive signed-64 integer")
    if not 0 < parsed <= _I64_MAX:
        raise MatchbookCatalogEvidenceError(f"{field} must be a canonical positive signed-64 integer")
    return str(parsed)


def _iso(value: object) -> str:
    text = _text(value, "observed_at")
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MatchbookCatalogEvidenceError("observed_at must be ISO-8601") from exc
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise MatchbookCatalogEvidenceError("observed_at must include timezone")
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decode(raw: bytes) -> object:
    if type(raw) is not bytes or not raw or len(raw) > _MAX_RAW:
        raise MatchbookCatalogEvidenceError("raw_response must be bounded non-empty bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MatchbookCatalogEvidenceError("provider response is not UTF-8") from exc

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in items:
            if key in out:
                raise MatchbookCatalogEvidenceError(f"duplicate JSON key {key!r}")
            out[key] = value
        return out

    def nonfinite(value: str) -> None:
        raise MatchbookCatalogEvidenceError(f"non-standard JSON constant {value!r}")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=nonfinite)
    except json.JSONDecodeError as exc:
        raise MatchbookCatalogEvidenceError("provider response is invalid JSON") from exc


@dataclass(frozen=True, slots=True)
class MatchbookCatalogRequest:
    resource: CatalogResource
    offset: int = 0
    per_page: int = 20
    event_id: str | int | None = None
    filters: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if type(self.resource) is not CatalogResource:
            raise MatchbookCatalogEvidenceError("resource must be CatalogResource")
        if type(self.offset) is not int or not 0 <= self.offset <= _I64_MAX:
            raise MatchbookCatalogEvidenceError("offset must be a non-negative signed-64 integer")
        if type(self.per_page) is not int or not 1 <= self.per_page <= _MAX_PAGE:
            raise MatchbookCatalogEvidenceError("per_page must be in 1..1000")
        if self.resource is CatalogResource.MARKETS:
            if self.event_id is None:
                raise MatchbookCatalogEvidenceError("markets request requires event_id")
            object.__setattr__(self, "event_id", _id(self.event_id, "event_id"))
        elif self.event_id is not None:
            raise MatchbookCatalogEvidenceError("event_id is only valid for markets")
        if type(self.filters) is not tuple or len(self.filters) > 16:
            raise MatchbookCatalogEvidenceError("filters must be a bounded tuple")
        allowed, seen, normalized = _ALLOWED_FILTERS[self.resource], set(), []
        for item in self.filters:
            if type(item) is not tuple or len(item) != 2:
                raise MatchbookCatalogEvidenceError("filter must be a (key, value) tuple")
            key, value = _text(item[0], "filter key"), _text(item[1], "filter value")
            if key not in allowed:
                raise MatchbookCatalogEvidenceError(f"filter {key!r} is not allowed for {self.resource.value}")
            if key in seen:
                raise MatchbookCatalogEvidenceError(f"duplicate filter {key!r}")
            seen.add(key)
            normalized.append((key, value))
        object.__setattr__(self, "filters", tuple(sorted(normalized)))

    @property
    def path(self) -> str:
        if self.resource is CatalogResource.SPORTS:
            return "/edge/rest/lookups/sports"
        if self.resource is CatalogResource.EVENTS:
            return "/edge/rest/events"
        return f"/edge/rest/events/{self.event_id}/markets"

    @property
    def query(self) -> tuple[tuple[str, str], ...]:
        values = [("offset", str(self.offset)), ("per-page", str(self.per_page)), *self.filters]
        if self.resource is not CatalogResource.SPORTS:
            values.append(("include-prices", "false"))
        return tuple(sorted(values))

    @property
    def request_sha256(self) -> str:
        return _digest({"v": 1, "provider": "matchbook", "method": "GET", "path": self.path, "query": self.query})

    @property
    def scope_sha256(self) -> str:
        return _digest({"v": 1, "provider": "matchbook", "resource": self.resource.value, "path": self.path,
                        "per_page": self.per_page, "filters": self.filters, "include_prices": False})


@dataclass(frozen=True, slots=True)
class MatchbookCatalogEntity:
    resource: CatalogResource
    native_id: str
    name: str | None = None
    status: str | None = None
    parent_sport_id: str | None = None
    parent_event_id: str | None = None
    market_type: str | None = None

    def __post_init__(self) -> None:
        if type(self.resource) is not CatalogResource:
            raise MatchbookCatalogEvidenceError("entity resource is invalid")
        object.__setattr__(self, "native_id", _id(self.native_id, "entity id"))
        for field in ("name", "status", "market_type"):
            if (value := getattr(self, field)) is not None:
                object.__setattr__(self, field, _text(value, field))
        for field in ("parent_sport_id", "parent_event_id"):
            if (value := getattr(self, field)) is not None:
                object.__setattr__(self, field, _id(value, field))
        if self.resource is CatalogResource.SPORTS and (self.parent_sport_id or self.parent_event_id or self.market_type):
            raise MatchbookCatalogEvidenceError("sport entity has incompatible parent/type fields")
        if self.resource is CatalogResource.EVENTS and (self.parent_event_id or self.market_type):
            raise MatchbookCatalogEvidenceError("event entity has incompatible parent/type fields")
        if self.resource is CatalogResource.MARKETS and self.parent_event_id is None:
            raise MatchbookCatalogEvidenceError("market entity requires parent event")

    def payload(self) -> dict[str, object]:
        return {"resource": self.resource.value, "id": self.native_id, "name": self.name, "status": self.status,
                "sport_id": self.parent_sport_id, "event_id": self.parent_event_id, "market_type": self.market_type}


@dataclass(frozen=True, slots=True)
class MatchbookCatalogPageEvidence:
    request: MatchbookCatalogRequest
    observed_at: str
    raw_response_sha256: str
    raw_response_size_bytes: int
    entities: tuple[MatchbookCatalogEntity, ...]

    def __post_init__(self) -> None:
        if type(self.request) is not MatchbookCatalogRequest:
            raise MatchbookCatalogEvidenceError("request is invalid")
        object.__setattr__(self, "observed_at", _iso(self.observed_at))
        if type(self.raw_response_sha256) is not str or len(self.raw_response_sha256) != 64 or any(c not in _HEX for c in self.raw_response_sha256):
            raise MatchbookCatalogEvidenceError("raw_response_sha256 is invalid")
        if type(self.raw_response_size_bytes) is not int or not 0 < self.raw_response_size_bytes <= _MAX_RAW:
            raise MatchbookCatalogEvidenceError("raw_response_size_bytes is invalid")
        if type(self.entities) is not tuple:
            raise MatchbookCatalogEvidenceError("entities must be tuple")
        seen: set[str] = set()
        for entity in self.entities:
            if type(entity) is not MatchbookCatalogEntity or entity.resource is not self.request.resource:
                raise MatchbookCatalogEvidenceError("entity does not match request resource")
            if entity.native_id in seen:
                raise MatchbookCatalogEvidenceError("duplicate provider identity within page")
            seen.add(entity.native_id)
            if self.request.resource is CatalogResource.MARKETS and entity.parent_event_id != self.request.event_id:
                raise MatchbookCatalogEvidenceError("market parent does not match request event")

    @property
    def page_sha256(self) -> str:
        return _digest({"v": 1, "provider": "matchbook", "request": self.request.request_sha256,
                        "observed_at": self.observed_at, "raw": self.raw_response_sha256,
                        "raw_size": self.raw_response_size_bytes, "entities": [e.payload() for e in self.entities]})


@dataclass(frozen=True, slots=True)
class MatchbookCatalogObservation:
    resource: CatalogResource
    scope_sha256: str
    pages: tuple[MatchbookCatalogPageEvidence, ...]
    entity_count: int
    observation_sha256: str

    snapshot_atomic = False
    catalog_complete = False
    authoritative_absence = False


def _rows(payload: object, resource: CatalogResource) -> list[object]:
    if resource is CatalogResource.SPORTS and isinstance(payload, list):
        return payload
    if not isinstance(payload, dict) or not isinstance(payload.get(resource.value), list):
        raise MatchbookCatalogEvidenceError(f"provider response requires {resource.value}[]")
    return payload[resource.value]


def _entity(row: object, request: MatchbookCatalogRequest) -> MatchbookCatalogEntity:
    if not isinstance(row, Mapping):
        raise MatchbookCatalogEvidenceError("catalog entity must be object")
    native = _id(row.get("id"), f"{request.resource.value}.id")
    opt = lambda key: None if row.get(key) is None else _text(row.get(key), key)
    if request.resource is CatalogResource.SPORTS:
        return MatchbookCatalogEntity(request.resource, native, name=opt("name"), status=opt("status"))
    if request.resource is CatalogResource.EVENTS:
        sport = None if row.get("sport-id") is None else _id(row.get("sport-id"), "events.sport-id")
        return MatchbookCatalogEntity(request.resource, native, name=opt("name"), status=opt("status"), parent_sport_id=sport)
    if row.get("event-id") is not None and _id(row.get("event-id"), "markets.event-id") != request.event_id:
        raise MatchbookCatalogEvidenceError("markets.event-id does not match request event_id")
    return MatchbookCatalogEntity(request.resource, native, name=opt("name"), status=opt("status"),
                                  parent_event_id=str(request.event_id), market_type=opt("market-type"))


def parse_matchbook_catalog_page(request: MatchbookCatalogRequest, raw_response: bytes, *, observed_at: str) -> MatchbookCatalogPageEvidence:
    if type(request) is not MatchbookCatalogRequest:
        raise MatchbookCatalogEvidenceError("request is invalid")
    rows = _rows(_decode(raw_response), request.resource)
    if len(rows) > request.per_page:
        raise MatchbookCatalogEvidenceError("provider page contains more rows than requested")
    return MatchbookCatalogPageEvidence(request, observed_at, hashlib.sha256(raw_response).hexdigest(), len(raw_response),
                                        tuple(_entity(row, request) for row in rows))


def compose_matchbook_catalog_observation(pages: Sequence[MatchbookCatalogPageEvidence]) -> MatchbookCatalogObservation:
    if isinstance(pages, (str, bytes)) or not isinstance(pages, Sequence) or not pages:
        raise MatchbookCatalogEvidenceError("pages must be a non-empty sequence")
    pages = tuple(pages)
    if any(type(p) is not MatchbookCatalogPageEvidence for p in pages):
        raise MatchbookCatalogEvidenceError("pages contain invalid evidence")
    first = pages[0]
    if first.request.offset != 0:
        raise MatchbookCatalogEvidenceError("catalog observation must begin at offset 0")
    resource, scope, expected, last, seen, count = first.request.resource, first.request.scope_sha256, 0, None, set(), 0
    for page in pages:
        if page.request.resource is not resource or page.request.scope_sha256 != scope:
            raise MatchbookCatalogEvidenceError("catalog pages do not share one frozen scope")
        if page.request.offset != expected:
            raise MatchbookCatalogEvidenceError("catalog page offsets must be contiguous")
        current = datetime.fromisoformat(page.observed_at.replace("Z", "+00:00"))
        if last is not None and current < last:
            raise MatchbookCatalogEvidenceError("catalog page observation time regressed")
        last, expected = current, expected + page.request.per_page
        for entity in page.entities:
            if entity.native_id in seen:
                raise MatchbookCatalogEvidenceError("provider identity repeated across catalog pages")
            seen.add(entity.native_id); count += 1
    truth = {"snapshot_atomic": False, "catalog_complete": False, "authoritative_absence": False}
    sha = _digest({"v": 1, "provider": "matchbook", "resource": resource.value, "scope": scope,
                   "pages": [p.page_sha256 for p in pages], "truth": truth})
    return MatchbookCatalogObservation(resource, scope, pages, count, sha)


__all__ = ["CatalogResource", "MatchbookCatalogEntity", "MatchbookCatalogEvidenceError",
           "MatchbookCatalogObservation", "MatchbookCatalogPageEvidence", "MatchbookCatalogRequest",
           "compose_matchbook_catalog_observation", "parse_matchbook_catalog_page"]

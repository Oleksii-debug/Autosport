from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import re
from typing import Iterable

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_KEY_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")


class SurfaceCapabilityError(ValueError):
    """Raised when capability evidence is structurally ambiguous or malformed."""


class Surface(str, Enum):
    EVENTS = "events"
    ODDS = "odds"
    PROPS = "props"
    LIVE = "live"
    LIVE_POINTS = "live_points"
    LIVE_PERIOD_MARKETS = "live_period_markets"
    HISTORICAL_ODDS = "historical_odds"
    HISTORICAL_CLOSING_ODDS = "historical_closing_odds"
    HISTORICAL_MATCHES = "historical_matches"


class TechnicalSupport(str, Enum):
    OBSERVED_SUPPORTED = "OBSERVED_SUPPORTED"
    ROUTE_ELSEWHERE = "ROUTE_ELSEWHERE"
    TRANSIENT_UNKNOWN = "TRANSIENT_UNKNOWN"
    UNKNOWN = "UNKNOWN"


class DataUsability(str, Enum):
    USABLE = "USABLE"
    EMPTY = "EMPTY"
    INCOMPLETE = "INCOMPLETE"
    STALE = "STALE"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


def _require_exact_bool(name: str, value: object) -> bool:
    if type(value) is not bool:
        raise SurfaceCapabilityError(f"{name} must be an exact bool")
    return value


def _require_exact_int(name: str, value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise SurfaceCapabilityError(f"{name} must be an exact int >= {minimum}")
    return value


def _require_key(name: str, value: object) -> str:
    if type(value) is not str or not _KEY_RE.fullmatch(value):
        raise SurfaceCapabilityError(f"{name} must be a canonical lowercase underscore key")
    return value


def _require_utc(name: str, value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise SurfaceCapabilityError(f"{name} must be timezone-aware")
    normalized = value.astimezone(timezone.utc)
    if normalized.utcoffset() != timezone.utc.utcoffset(normalized):
        raise SurfaceCapabilityError(f"{name} must normalize to UTC")
    return normalized


def _require_digest(name: str, value: object) -> str:
    if type(value) is not str or not _SHA256_RE.fullmatch(value):
        raise SurfaceCapabilityError(f"{name} must be lowercase SHA-256 hex")
    return value


def _canonical_tuple(name: str, values: Iterable[str]) -> tuple[str, ...]:
    out = tuple(values)
    for value in out:
        _require_key(name, value)
    if len(out) != len(set(out)):
        raise SurfaceCapabilityError(f"{name} must not contain duplicates")
    if out != tuple(sorted(out)):
        raise SurfaceCapabilityError(f"{name} must be canonically sorted")
    return out


def _canonical_routes(values: Iterable[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    out = tuple(values)
    normalized: list[tuple[str, str]] = []
    for item in out:
        if type(item) is not tuple or len(item) != 2:
            raise SurfaceCapabilityError("served_elsewhere entries must be exact 2-tuples")
        market, route = item
        _require_key("served_elsewhere market", market)
        if type(route) is not str or not route.startswith("/v1/") or " " in route:
            raise SurfaceCapabilityError("served_elsewhere route must be a canonical /v1/ path")
        normalized.append((market, route))
    if len(normalized) != len(set(normalized)):
        raise SurfaceCapabilityError("served_elsewhere must not contain duplicates")
    if tuple(normalized) != tuple(sorted(normalized)):
        raise SurfaceCapabilityError("served_elsewhere must be canonically sorted")
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class ParlaySurfaceObservation:
    """Read-only provider evidence for one exact sport/surface request.

    This object does not grant provider-write, execution, settlement, or real-money authority.
    """

    sport_key: str
    surface: Surface
    observed_at: datetime
    available_at: datetime
    status_code: int
    provider_origin_verified: bool = field(default=False, init=False)
    response_sha256: str
    row_count: int
    pagination_complete: bool
    requested_market: str | None = None
    served_markets: tuple[str, ...] = ()
    unservable_markets: tuple[str, ...] = ()
    served_elsewhere: tuple[tuple[str, str], ...] = ()
    error_code: str | None = None
    oldest_row_age_seconds: int | None = None
    provider_id: str = "parlayapi"

    def __post_init__(self) -> None:
        if self.provider_id != "parlayapi":
            raise SurfaceCapabilityError("provider_id must be exactly parlayapi")
        object.__setattr__(self, "sport_key", _require_key("sport_key", self.sport_key))
        if type(self.surface) is not Surface:
            raise SurfaceCapabilityError("surface must be an exact Surface")
        observed = _require_utc("observed_at", self.observed_at)
        available = _require_utc("available_at", self.available_at)
        if available < observed:
            raise SurfaceCapabilityError("available_at cannot precede observed_at")
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "available_at", available)
        _require_exact_int("status_code", self.status_code, minimum=100)
        if self.status_code > 599:
            raise SurfaceCapabilityError("status_code must be <= 599")
        object.__setattr__(self, "response_sha256", _require_digest("response_sha256", self.response_sha256))
        _require_exact_int("row_count", self.row_count)
        _require_exact_bool("pagination_complete", self.pagination_complete)

        if self.requested_market is not None:
            object.__setattr__(self, "requested_market", _require_key("requested_market", self.requested_market))
        object.__setattr__(self, "served_markets", _canonical_tuple("served_markets", self.served_markets))
        object.__setattr__(self, "unservable_markets", _canonical_tuple("unservable_markets", self.unservable_markets))
        object.__setattr__(self, "served_elsewhere", _canonical_routes(self.served_elsewhere))

        overlap = set(self.served_markets) & set(self.unservable_markets)
        if overlap:
            raise SurfaceCapabilityError("a market cannot be both served and unservable")
        route_markets = [market for market, _ in self.served_elsewhere]
        if len(route_markets) != len(set(route_markets)):
            raise SurfaceCapabilityError("a market cannot have multiple served_elsewhere routes")
        if not set(route_markets).issubset(set(self.unservable_markets)):
            raise SurfaceCapabilityError("served_elsewhere requires matching unservable market evidence")

        if self.error_code is not None:
            if type(self.error_code) is not str or not self.error_code or self.error_code != self.error_code.upper():
                raise SurfaceCapabilityError("error_code must be non-empty uppercase text")
        if self.oldest_row_age_seconds is not None:
            _require_exact_int("oldest_row_age_seconds", self.oldest_row_age_seconds)
        if self.status_code == 200 and self.error_code is not None:
            raise SurfaceCapabilityError("HTTP 200 cannot carry an error_code")
        if self.status_code != 200 and self.row_count != 0:
            raise SurfaceCapabilityError("non-200 evidence cannot claim usable response rows")

    @property
    def evidence_id(self) -> str:
        payload = {
            "provider_id": self.provider_id,
            "sport_key": self.sport_key,
            "surface": self.surface.value,
            "observed_at": self.observed_at.isoformat().replace("+00:00", "Z"),
            "available_at": self.available_at.isoformat().replace("+00:00", "Z"),
            "status_code": self.status_code,
            "provider_origin_verified": self.provider_origin_verified,
            "response_sha256": self.response_sha256,
            "row_count": self.row_count,
            "pagination_complete": self.pagination_complete,
            "requested_market": self.requested_market,
            "served_markets": list(self.served_markets),
            "unservable_markets": list(self.unservable_markets),
            "served_elsewhere": [list(item) for item in self.served_elsewhere],
            "error_code": self.error_code,
            "oldest_row_age_seconds": self.oldest_row_age_seconds,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class SurfaceCapabilityDecision:
    evidence_id: str
    technical_support: TechnicalSupport
    data_usability: DataUsability
    route_elsewhere: str | None
    reason: str
    provider_write_authorized: bool = False
    execution_authorized: bool = False
    real_money_execution: bool = False
    source_authority_resolved: bool = False

    def __post_init__(self) -> None:
        _require_digest("evidence_id", self.evidence_id)
        if type(self.technical_support) is not TechnicalSupport:
            raise SurfaceCapabilityError("technical_support must be exact TechnicalSupport")
        if type(self.data_usability) is not DataUsability:
            raise SurfaceCapabilityError("data_usability must be exact DataUsability")
        for field_name in ("provider_write_authorized", "execution_authorized", "real_money_execution", "source_authority_resolved"):
            if getattr(self, field_name) is not False:
                raise SurfaceCapabilityError(f"{field_name} is structurally false")
        if type(self.reason) is not str or not self.reason:
            raise SurfaceCapabilityError("reason must be non-empty")
        if self.technical_support is TechnicalSupport.ROUTE_ELSEWHERE:
            if self.route_elsewhere is None:
                raise SurfaceCapabilityError("ROUTE_ELSEWHERE requires a route")
        elif self.route_elsewhere is not None:
            raise SurfaceCapabilityError("route_elsewhere is only valid for ROUTE_ELSEWHERE")


def evaluate_surface_capability(
    observation: ParlaySurfaceObservation,
    *,
    as_of: datetime,
    max_row_age_seconds: int,
) -> SurfaceCapabilityDecision:
    """Resolve bounded technical support and current-data usability.

    This is an observational prerequisite, not provider-origin authority. Positive
    observation support is never inferred from sport-catalog membership, a generic HTTP
    200, bookmaker registry activity, or an empty response. A requested market needs an
    explicit endpoint-service witness. source_authority_resolved remains structurally false.
    """

    if type(observation) is not ParlaySurfaceObservation:
        raise SurfaceCapabilityError("observation must be an exact ParlaySurfaceObservation")
    decision_time = _require_utc("as_of", as_of)
    max_age = _require_exact_int("max_row_age_seconds", max_row_age_seconds)

    def result(
        support: TechnicalSupport,
        usability: DataUsability,
        reason: str,
        route: str | None = None,
    ) -> SurfaceCapabilityDecision:
        return SurfaceCapabilityDecision(
            evidence_id=observation.evidence_id,
            technical_support=support,
            data_usability=usability,
            route_elsewhere=route,
            reason=reason,
        )

    if observation.available_at > decision_time:
        return result(TechnicalSupport.UNKNOWN, DataUsability.UNKNOWN, "EVIDENCE_NOT_CAUSALLY_AVAILABLE")
    if observation.status_code == 429 or observation.status_code >= 500:
        return result(TechnicalSupport.TRANSIENT_UNKNOWN, DataUsability.UNAVAILABLE, "TRANSIENT_PROVIDER_FAILURE")
    if observation.status_code == 401:
        return result(TechnicalSupport.UNKNOWN, DataUsability.UNAVAILABLE, "AUTHENTICATION_UNRESOLVED")
    if observation.status_code == 403:
        return result(TechnicalSupport.UNKNOWN, DataUsability.UNAVAILABLE, "FORBIDDEN_UNRESOLVED")
    if observation.status_code != 200:
        return result(TechnicalSupport.UNKNOWN, DataUsability.UNAVAILABLE, f"HTTP_{observation.status_code}")

    market = observation.requested_market
    if market is not None:
        routes = dict(observation.served_elsewhere)
        if market in observation.unservable_markets:
            route = routes.get(market)
            if route is not None:
                return result(
                    TechnicalSupport.ROUTE_ELSEWHERE,
                    DataUsability.UNAVAILABLE,
                    "MARKET_SERVED_ELSEWHERE",
                    route,
                )
            return result(TechnicalSupport.UNKNOWN, DataUsability.UNAVAILABLE, "MARKET_UNSERVABLE_HERE")
        if market not in observation.served_markets:
            return result(TechnicalSupport.UNKNOWN, DataUsability.UNKNOWN, "MISSING_MARKET_SERVICE_WITNESS")

    if not observation.pagination_complete:
        return result(TechnicalSupport.OBSERVED_SUPPORTED, DataUsability.INCOMPLETE, "RESPONSE_PAGINATION_INCOMPLETE")

    if observation.row_count == 0:
        return result(TechnicalSupport.OBSERVED_SUPPORTED, DataUsability.EMPTY, "SUPPORTED_BUT_NO_CURRENT_ROWS")

    if observation.oldest_row_age_seconds is None:
        return result(TechnicalSupport.OBSERVED_SUPPORTED, DataUsability.UNKNOWN, "ROW_FRESHNESS_UNPROVEN")
    if observation.oldest_row_age_seconds > max_age:
        return result(TechnicalSupport.OBSERVED_SUPPORTED, DataUsability.STALE, "OLDEST_ROW_EXCEEDS_FRESHNESS_LIMIT")

    return result(TechnicalSupport.OBSERVED_SUPPORTED, DataUsability.USABLE, "EXACT_SURFACE_EVIDENCE_USABLE")

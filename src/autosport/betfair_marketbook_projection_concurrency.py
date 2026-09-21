from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION = (
    "betfair.list-market-book.projection-concurrency.v2-no-auto-expiry"
)
_MAX_PROJECTION_REQUESTS_IN_FLIGHT = 3
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _validate_request_id(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("request_id must be str")
    if not value or value != value.strip():
        raise ValueError("request_id must be non-empty and trimmed")
    return value


def _utc_microseconds(value: object, *, name: str) -> int:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    utc_value = value.astimezone(timezone.utc)
    delta = utc_value - _EPOCH
    return (
        delta.days * 86_400 * 1_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


@dataclass(frozen=True, slots=True)
class MarketBookProjectionLease:
    request_id: str
    acquired_at_utc_us: int

    def __post_init__(self) -> None:
        _validate_request_id(self.request_id)
        if type(self.acquired_at_utc_us) is not int:
            raise TypeError("acquired_at_utc_us must be a non-boolean int")


@dataclass(frozen=True, slots=True)
class MarketBookProjectionConcurrencyState:
    policy_version: str
    last_observed_at_utc_us: int | None
    active: tuple[MarketBookProjectionLease, ...]

    def __post_init__(self) -> None:
        if self.policy_version != BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION:
            raise ValueError("unsupported Betfair MarketBook projection concurrency policy")
        if self.last_observed_at_utc_us is not None and type(
            self.last_observed_at_utc_us
        ) is not int:
            raise TypeError("last_observed_at_utc_us must be a non-boolean int or None")
        if type(self.active) is not tuple:
            raise TypeError("active must be a tuple")
        if len(self.active) > _MAX_PROJECTION_REQUESTS_IN_FLIGHT:
            raise ValueError("projection concurrency state exceeds provider maximum")
        ids: list[str] = []
        for lease in self.active:
            if type(lease) is not MarketBookProjectionLease:
                raise TypeError("active must contain MarketBookProjectionLease values")
            ids.append(lease.request_id)
            if (
                self.last_observed_at_utc_us is not None
                and lease.acquired_at_utc_us > self.last_observed_at_utc_us
            ):
                raise ValueError("lease was acquired after last observed time")
        if ids != sorted(ids):
            raise ValueError("active leases must be sorted by request_id")
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate active request_id")


@dataclass(frozen=True, slots=True)
class MarketBookProjectionConcurrencyDecision:
    request_id: str
    observed_at_utc_us: int
    projection_bearing: bool
    allowed: bool
    active_projection_requests: int


class BetfairMarketBookProjectionConcurrencyGate:
    """Pure fail-safe state kernel for Betfair projection request concurrency.

    Betfair documents a three-request concurrency limit for listMarketBook calls
    carrying OrderProjection and/or MatchProjection. This class models only local
    product admission state. Price-only reads bypass this bucket.

    Active projection leases NEVER expire merely because caller time advanced. A
    caller-chosen timeout cannot prove that the provider stopped counting a request
    as in-flight. Slots therefore release only through complete for the exact active
    request. On restart unresolved leases remain blocking; a separate product
    recovery authority must establish when they are safe to resolve. This kernel does
    no network I/O, scheduling, sleeping, retry or recovery, and its public decisions
    are not provider evidence or financial/execution permission.
    """

    def __init__(
        self,
        *,
        state: MarketBookProjectionConcurrencyState | None = None,
    ) -> None:
        self._active: dict[str, MarketBookProjectionLease] = {}
        self._last_observed_at_utc_us: int | None = None
        if state is not None:
            if type(state) is not MarketBookProjectionConcurrencyState:
                raise TypeError(
                    "state must be MarketBookProjectionConcurrencyState or None"
                )
            self._last_observed_at_utc_us = state.last_observed_at_utc_us
            self._active = {lease.request_id: lease for lease in state.active}

    @property
    def policy_version(self) -> str:
        return BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION

    def _advance(self, observed_at: datetime) -> int:
        observed_us = _utc_microseconds(observed_at, name="observed_at")
        if (
            self._last_observed_at_utc_us is not None
            and observed_us < self._last_observed_at_utc_us
        ):
            raise ValueError("observed_at must not move backwards")
        self._last_observed_at_utc_us = observed_us
        return observed_us

    def snapshot(self) -> MarketBookProjectionConcurrencyState:
        return MarketBookProjectionConcurrencyState(
            policy_version=BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION,
            last_observed_at_utc_us=self._last_observed_at_utc_us,
            active=tuple(
                self._active[request_id]
                for request_id in sorted(self._active)
            ),
        )

    def begin(
        self,
        request_id: str,
        *,
        observed_at: datetime,
        has_order_projection: bool,
        has_match_projection: bool,
    ) -> MarketBookProjectionConcurrencyDecision:
        request_id = _validate_request_id(request_id)
        if type(has_order_projection) is not bool:
            raise TypeError("has_order_projection must be bool")
        if type(has_match_projection) is not bool:
            raise TypeError("has_match_projection must be bool")
        observed_us = self._advance(observed_at)
        if request_id in self._active:
            raise ValueError("request_id is already active")

        projection_bearing = has_order_projection or has_match_projection
        if not projection_bearing:
            return MarketBookProjectionConcurrencyDecision(
                request_id=request_id,
                observed_at_utc_us=observed_us,
                projection_bearing=False,
                allowed=True,
                active_projection_requests=len(self._active),
            )

        if len(self._active) >= _MAX_PROJECTION_REQUESTS_IN_FLIGHT:
            return MarketBookProjectionConcurrencyDecision(
                request_id=request_id,
                observed_at_utc_us=observed_us,
                projection_bearing=True,
                allowed=False,
                active_projection_requests=len(self._active),
            )

        self._active[request_id] = MarketBookProjectionLease(
            request_id=request_id,
            acquired_at_utc_us=observed_us,
        )
        return MarketBookProjectionConcurrencyDecision(
            request_id=request_id,
            observed_at_utc_us=observed_us,
            projection_bearing=True,
            allowed=True,
            active_projection_requests=len(self._active),
        )

    def complete(self, request_id: str, *, observed_at: datetime) -> None:
        request_id = _validate_request_id(request_id)
        self._advance(observed_at)
        if request_id not in self._active:
            raise ValueError(
                "request_id is not an active projection-bearing request"
            )
        del self._active[request_id]

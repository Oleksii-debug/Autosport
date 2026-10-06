from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock
from typing import Iterable


BETFAIR_MARKETBOOK_RATE_POLICY_VERSION = "betfair.list-market-book.per-market-rate.v1"
_MAX_CALLS_PER_WINDOW = 5
_WINDOW_MICROSECONDS = 1_000_000
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _validate_market_id(value: object) -> str:
    if type(value) is not str:
        raise TypeError("market_id must be exact str")
    if not value or value != value.strip():
        raise ValueError("market_id must be non-empty and trimmed")
    return value


def _utc_microseconds(value: object) -> int:
    if type(value) is not datetime:
        raise TypeError("scheduled_at must be exact datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("scheduled_at must be timezone-aware")
    utc_value = value.astimezone(timezone.utc)
    delta = utc_value - _EPOCH
    return (
        delta.days * 86_400 * 1_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


def _datetime_from_utc_microseconds(value: int) -> datetime:
    return _EPOCH + timedelta(microseconds=value)


def _normalize_market_ids(market_ids: Iterable[str]) -> tuple[str, ...]:
    if isinstance(market_ids, (str, bytes)):
        raise TypeError("market_ids must be an iterable of market ids, not text")
    normalized = tuple(_validate_market_id(market_id) for market_id in market_ids)
    if not normalized:
        raise ValueError("market_ids must not be empty")
    if len(set(normalized)) != len(normalized):
        raise ValueError("duplicate market_id in one provider request")
    return normalized


@dataclass(frozen=True, slots=True)
class MarketBookRateWindowState:
    market_id: str
    accepted_at_utc_us: tuple[int, ...]

    def __post_init__(self) -> None:
        _validate_market_id(self.market_id)
        if type(self.accepted_at_utc_us) is not tuple:
            raise TypeError("accepted_at_utc_us must be a tuple")
        if not self.accepted_at_utc_us:
            raise ValueError("accepted_at_utc_us must not be empty")
        previous: int | None = None
        if len(self.accepted_at_utc_us) > _MAX_CALLS_PER_WINDOW:
            raise ValueError("rate state exceeds provider maximum calls per window")
        for value in self.accepted_at_utc_us:
            if type(value) is not int:
                raise TypeError("accepted rate timestamp must be a non-boolean int")
            if previous is not None and value < previous:
                raise ValueError("accepted rate timestamps must be monotonic")
            previous = value


@dataclass(frozen=True, slots=True)
class MarketBookRateGateState:
    policy_version: str
    last_scheduled_at_utc_us: int | None
    markets: tuple[MarketBookRateWindowState, ...]

    def __post_init__(self) -> None:
        if (
            type(self.policy_version) is not str
            or self.policy_version != BETFAIR_MARKETBOOK_RATE_POLICY_VERSION
        ):
            raise ValueError("unsupported Betfair MarketBook rate policy version")
        if self.last_scheduled_at_utc_us is not None and type(
            self.last_scheduled_at_utc_us
        ) is not int:
            raise TypeError("last_scheduled_at_utc_us must be a non-boolean int or None")
        if type(self.markets) is not tuple:
            raise TypeError("markets must be a tuple")
        ids: list[str] = []
        if self.markets and self.last_scheduled_at_utc_us is None:
            raise ValueError("market rate state requires last scheduled time")
        for market in self.markets:
            if type(market) is not MarketBookRateWindowState:
                raise TypeError("markets must contain MarketBookRateWindowState values")
            ids.append(market.market_id)
            if self.last_scheduled_at_utc_us is not None:
                if market.accepted_at_utc_us[-1] > self.last_scheduled_at_utc_us:
                    raise ValueError("rate state contains reservation after last scheduled time")
                cutoff = self.last_scheduled_at_utc_us - _WINDOW_MICROSECONDS
                if market.accepted_at_utc_us[0] <= cutoff:
                    raise ValueError("rate state contains reservation outside active window")
        if ids != sorted(ids):
            raise ValueError("market rate state must be sorted by market_id")
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate market rate state")


@dataclass(frozen=True, slots=True)
class MarketBookRateDecision:
    market_ids: tuple[str, ...]
    scheduled_at_utc_us: int
    allowed: bool
    blocked_market_ids: tuple[str, ...] = ()
    next_eligible_at_utc_us: int | None = None
    provider_limit_coverage_complete: bool = False
    provider_dispatch_authorized: bool = False

    def __post_init__(self) -> None:
        if type(self.market_ids) is not tuple:
            raise TypeError("market_ids must be a tuple")
        normalized_ids = _normalize_market_ids(self.market_ids)
        if normalized_ids != self.market_ids:
            raise ValueError("market_ids must be canonical")
        if type(self.scheduled_at_utc_us) is not int:
            raise TypeError("scheduled_at_utc_us must be a non-boolean int")
        if type(self.allowed) is not bool:
            raise TypeError("allowed must be bool")
        if type(self.blocked_market_ids) is not tuple:
            raise TypeError("blocked_market_ids must be a tuple")
        if self.blocked_market_ids:
            normalized_blocked = _normalize_market_ids(self.blocked_market_ids)
            if normalized_blocked != self.blocked_market_ids:
                raise ValueError("blocked_market_ids must be canonical")
            if any(market_id not in self.market_ids for market_id in self.blocked_market_ids):
                raise ValueError("blocked_market_ids must be a subset of market_ids")
        if self.next_eligible_at_utc_us is not None and type(
            self.next_eligible_at_utc_us
        ) is not int:
            raise TypeError("next_eligible_at_utc_us must be a non-boolean int or None")
        if self.allowed:
            if self.blocked_market_ids or self.next_eligible_at_utc_us is not None:
                raise ValueError("allowed decision cannot carry blocked/next-eligible state")
        else:
            if not self.blocked_market_ids or self.next_eligible_at_utc_us is None:
                raise ValueError("denied decision requires blocked markets and next-eligible time")
            if self.next_eligible_at_utc_us <= self.scheduled_at_utc_us:
                raise ValueError("denied decision next-eligible time must be in the future")
        if self.provider_limit_coverage_complete is not False:
            raise ValueError("local rate decision cannot claim complete provider-limit coverage")
        if self.provider_dispatch_authorized is not False:
            raise ValueError("local rate decision cannot authorize provider dispatch")

    @property
    def scheduled_at(self) -> datetime:
        return _datetime_from_utc_microseconds(self.scheduled_at_utc_us)

    @property
    def next_eligible_at(self) -> datetime | None:
        if self.next_eligible_at_utc_us is None:
            return None
        return _datetime_from_utc_microseconds(self.next_eligible_at_utc_us)


class BetfairMarketBookPerMarketRateGate:
    """Deterministic admission gate for Betfair's per-market MarketBook read ceiling.

    This type deliberately does not schedule work, sleep, perform network I/O, retry,
    or infer provider availability. The caller supplies the causal scheduled instant.
    A successful multi-market reservation consumes one call for every market id in
    the batch. A denied reservation consumes none.

    The provider phrase "up to a maximum of 5 times per second to a single marketId"
    is implemented conservatively as a rolling one-second window. The policy is
    versioned so a future, separately qualified interpretation cannot silently rewrite
    historical admission decisions.

    Passing this gate proves only that this one product-local per-market rolling-window
    budget has room. It does not model Betfair's complete account/provider request
    pressure, prove provider acceptance, or authorize provider dispatch.
    """

    def __init__(self, state: MarketBookRateGateState | None = None) -> None:
        self._lock = RLock()
        # Internal entries carry a process-local generation so an interrupted
        # admission can remove only its own capacity even if another thread
        # reserves the same market and timestamp before cleanup reacquires the
        # lock. Persisted state intentionally strips these generations.
        self._accepted: dict[str, list[tuple[int, int]]] = {}
        self._last_scheduled_at_utc_us: int | None = None
        self._next_reservation_generation = 1
        if state is not None:
            if type(state) is not MarketBookRateGateState:
                raise TypeError("state must be MarketBookRateGateState or None")
            self._last_scheduled_at_utc_us = state.last_scheduled_at_utc_us
            self._accepted = {
                market.market_id: [
                    (timestamp, 0) for timestamp in market.accepted_at_utc_us
                ]
                for market in state.markets
            }

    @property
    def policy_version(self) -> str:
        return BETFAIR_MARKETBOOK_RATE_POLICY_VERSION

    def snapshot(self) -> MarketBookRateGateState:
        with self._lock:
            return MarketBookRateGateState(
                policy_version=BETFAIR_MARKETBOOK_RATE_POLICY_VERSION,
                last_scheduled_at_utc_us=self._last_scheduled_at_utc_us,
                markets=tuple(
                    MarketBookRateWindowState(
                        market_id,
                        tuple(
                            timestamp
                            for timestamp, _generation in self._accepted[market_id]
                        ),
                    )
                    for market_id in sorted(self._accepted)
                    if self._accepted[market_id]
                ),
            )

    def reserve(
        self,
        market_ids: Iterable[str],
        *,
        scheduled_at: datetime,
    ) -> MarketBookRateDecision:
        # Normalize all caller-controlled inputs before taking the authority
        # lock. Custom iterables and tzinfo hooks must never execute while this
        # gate is blocking concurrent reserve/snapshot operations.
        normalized_ids = _normalize_market_ids(market_ids)
        scheduled_us = _utc_microseconds(scheduled_at)
        reservation_generation: int | None = None
        try:
            with self._lock:
                if (
                    self._last_scheduled_at_utc_us is not None
                    and scheduled_us < self._last_scheduled_at_utc_us
                ):
                    raise ValueError("scheduled_at must not move backwards")

                cutoff = scheduled_us - _WINDOW_MICROSECONDS
                working: dict[str, list[tuple[int, int]]] = {}
                for market_id, accepted in self._accepted.items():
                    retained = [
                        entry for entry in accepted if entry[0] > cutoff
                    ]
                    if retained:
                        working[market_id] = retained

                blocked: list[str] = []
                next_eligible: list[int] = []
                for market_id in normalized_ids:
                    accepted = working.get(market_id, [])
                    if len(accepted) >= _MAX_CALLS_PER_WINDOW:
                        blocked.append(market_id)
                        next_eligible.append(
                            accepted[0][0] + _WINDOW_MICROSECONDS
                        )

                self._accepted = working
                self._last_scheduled_at_utc_us = scheduled_us

                if blocked:
                    return MarketBookRateDecision(
                        market_ids=normalized_ids,
                        scheduled_at_utc_us=scheduled_us,
                        allowed=False,
                        blocked_market_ids=tuple(blocked),
                        next_eligible_at_utc_us=max(next_eligible),
                    )

                # Consume a unique generation before adding capacity. If process
                # control lands anywhere after this point, a successor cannot
                # reuse the generation while cleanup is pending.
                reservation_generation = self._next_reservation_generation
                self._next_reservation_generation += 1
                decision = MarketBookRateDecision(
                    market_ids=normalized_ids,
                    scheduled_at_utc_us=scheduled_us,
                    allowed=True,
                )
                for market_id in normalized_ids:
                    self._accepted.setdefault(market_id, []).append(
                        (scheduled_us, reservation_generation)
                    )
                return decision
        except BaseException as primary:
            if reservation_generation is not None:
                try:
                    with self._lock:
                        for market_id in normalized_ids:
                            accepted = self._accepted.get(market_id)
                            if not accepted:
                                continue
                            retained = [
                                entry
                                for entry in accepted
                                if entry[1] != reservation_generation
                            ]
                            if retained:
                                self._accepted[market_id] = retained
                            else:
                                self._accepted.pop(market_id, None)
                except BaseException as cleanup_exc:
                    if (
                        not isinstance(cleanup_exc, Exception)
                        and isinstance(primary, Exception)
                    ):
                        raise
                    add_note = getattr(primary, "add_note", None)
                    if callable(add_note):
                        add_note(
                            "rate-reservation cleanup also failed: "
                            f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                        )
            raise

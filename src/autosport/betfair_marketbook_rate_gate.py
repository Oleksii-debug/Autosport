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

def _install_rate_gate_authority() -> None:
    """Seal canonical gate operations against later module/class rebinding."""

    gate_type = BetfairMarketBookPerMarketRateGate
    state_type = MarketBookRateGateState
    window_type = MarketBookRateWindowState
    decision_type = MarketBookRateDecision
    lock_type = RLock
    datetime_type = datetime
    timedelta_type = timedelta
    utc_timezone = timezone.utc
    epoch = datetime_type(1970, 1, 1, tzinfo=utc_timezone)
    policy_version = BETFAIR_MARKETBOOK_RATE_POLICY_VERSION
    max_calls = _MAX_CALLS_PER_WINDOW
    window_us = _WINDOW_MICROSECONDS

    def validate_market_id(value: object) -> str:
        if type(value) is not str:
            raise TypeError("market_id must be exact str")
        if not value or value != value.strip():
            raise ValueError("market_id must be non-empty and trimmed")
        return value

    def normalize_market_ids(market_ids: Iterable[str]) -> tuple[str, ...]:
        if isinstance(market_ids, (str, bytes)):
            raise TypeError("market_ids must be an iterable of market ids, not text")
        normalized = tuple(validate_market_id(market_id) for market_id in market_ids)
        if not normalized:
            raise ValueError("market_ids must not be empty")
        if len(set(normalized)) != len(normalized):
            raise ValueError("duplicate market_id in one provider request")
        return normalized

    def utc_microseconds(value: object) -> int:
        if type(value) is not datetime_type:
            raise TypeError("scheduled_at must be exact datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("scheduled_at must be timezone-aware")
        utc_value = value.astimezone(utc_timezone)
        delta = utc_value - epoch
        return (
            delta.days * 86_400 * 1_000_000
            + delta.seconds * 1_000_000
            + delta.microseconds
        )

    def datetime_from_utc_microseconds(value: int) -> datetime:
        if type(value) is not int:
            raise TypeError("UTC microsecond timestamp must be a non-boolean int")
        return epoch + timedelta_type(microseconds=value)

    def validate_window(value: object) -> None:
        if type(value) is not window_type:
            raise TypeError("markets must contain MarketBookRateWindowState values")
        validate_market_id(value.market_id)
        if type(value.accepted_at_utc_us) is not tuple:
            raise TypeError("accepted_at_utc_us must be a tuple")
        if not value.accepted_at_utc_us:
            raise ValueError("accepted_at_utc_us must not be empty")
        if len(value.accepted_at_utc_us) > max_calls:
            raise ValueError("rate state exceeds provider maximum calls per window")
        previous: int | None = None
        for timestamp in value.accepted_at_utc_us:
            if type(timestamp) is not int:
                raise TypeError("accepted rate timestamp must be a non-boolean int")
            if previous is not None and timestamp < previous:
                raise ValueError("accepted rate timestamps must be monotonic")
            previous = timestamp

    def validate_state(value: object) -> None:
        if type(value) is not state_type:
            raise TypeError("state must be MarketBookRateGateState or None")
        if type(value.policy_version) is not str or value.policy_version != policy_version:
            raise ValueError("unsupported Betfair MarketBook rate policy version")
        if (
            value.last_scheduled_at_utc_us is not None
            and type(value.last_scheduled_at_utc_us) is not int
        ):
            raise TypeError(
                "last_scheduled_at_utc_us must be a non-boolean int or None"
            )
        if type(value.markets) is not tuple:
            raise TypeError("markets must be a tuple")
        if value.markets and value.last_scheduled_at_utc_us is None:
            raise ValueError("market rate state requires last scheduled time")
        ids: list[str] = []
        for market in value.markets:
            validate_window(market)
            ids.append(market.market_id)
            if value.last_scheduled_at_utc_us is not None:
                if market.accepted_at_utc_us[-1] > value.last_scheduled_at_utc_us:
                    raise ValueError(
                        "rate state contains reservation after last scheduled time"
                    )
                cutoff = value.last_scheduled_at_utc_us - window_us
                if market.accepted_at_utc_us[0] <= cutoff:
                    raise ValueError(
                        "rate state contains reservation outside active window"
                    )
        if ids != sorted(ids):
            raise ValueError("market rate state must be sorted by market_id")
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate market rate state")

    def make_window(
        market_id: str,
        accepted_at_utc_us: tuple[int, ...],
    ) -> MarketBookRateWindowState:
        value = object.__new__(window_type)
        object.__setattr__(value, "market_id", market_id)
        object.__setattr__(value, "accepted_at_utc_us", accepted_at_utc_us)
        validate_window(value)
        return value

    def make_state(
        last_scheduled_at_utc_us: int | None,
        markets: tuple[MarketBookRateWindowState, ...],
    ) -> MarketBookRateGateState:
        value = object.__new__(state_type)
        object.__setattr__(value, "policy_version", policy_version)
        object.__setattr__(
            value,
            "last_scheduled_at_utc_us",
            last_scheduled_at_utc_us,
        )
        object.__setattr__(value, "markets", markets)
        validate_state(value)
        return value

    def make_decision(
        *,
        market_ids: tuple[str, ...],
        scheduled_at_utc_us: int,
        allowed: bool,
        blocked_market_ids: tuple[str, ...] = (),
        next_eligible_at_utc_us: int | None = None,
    ) -> MarketBookRateDecision:
        if type(scheduled_at_utc_us) is not int:
            raise TypeError("scheduled_at_utc_us must be a non-boolean int")
        if type(allowed) is not bool:
            raise TypeError("allowed must be bool")
        normalized = normalize_market_ids(market_ids)
        if normalized != market_ids:
            raise ValueError("market_ids must be canonical")
        if type(blocked_market_ids) is not tuple:
            raise TypeError("blocked_market_ids must be a tuple")
        if blocked_market_ids:
            normalized_blocked = normalize_market_ids(blocked_market_ids)
            if normalized_blocked != blocked_market_ids:
                raise ValueError("blocked_market_ids must be canonical")
            if any(market_id not in market_ids for market_id in blocked_market_ids):
                raise ValueError("blocked_market_ids must be a subset of market_ids")
        if allowed:
            if blocked_market_ids or next_eligible_at_utc_us is not None:
                raise ValueError(
                    "allowed decision cannot carry blocked/next-eligible state"
                )
        else:
            if (
                not blocked_market_ids
                or type(next_eligible_at_utc_us) is not int
                or next_eligible_at_utc_us <= scheduled_at_utc_us
            ):
                raise ValueError(
                    "denied decision requires blocked markets and next-eligible time"
                )
        value = object.__new__(decision_type)
        object.__setattr__(value, "market_ids", market_ids)
        object.__setattr__(value, "scheduled_at_utc_us", scheduled_at_utc_us)
        object.__setattr__(value, "allowed", allowed)
        object.__setattr__(value, "blocked_market_ids", blocked_market_ids)
        object.__setattr__(
            value,
            "next_eligible_at_utc_us",
            next_eligible_at_utc_us,
        )
        object.__setattr__(value, "provider_limit_coverage_complete", False)
        object.__setattr__(value, "provider_dispatch_authorized", False)
        return value

    def gate_init(
        self: BetfairMarketBookPerMarketRateGate,
        state: MarketBookRateGateState | None = None,
    ) -> None:
        self._lock = lock_type()
        self._accepted = {}
        self._last_scheduled_at_utc_us = None
        self._next_reservation_generation = 1
        if state is not None:
            validate_state(state)
            detached_markets = tuple(
                make_window(
                    market.market_id,
                    tuple(market.accepted_at_utc_us),
                )
                for market in state.markets
            )
            detached_state = make_state(
                state.last_scheduled_at_utc_us,
                detached_markets,
            )
            self._last_scheduled_at_utc_us = (
                detached_state.last_scheduled_at_utc_us
            )
            self._accepted = {
                market.market_id: [
                    (timestamp, 0) for timestamp in market.accepted_at_utc_us
                ]
                for market in detached_state.markets
            }

    def snapshot(
        self: BetfairMarketBookPerMarketRateGate,
    ) -> MarketBookRateGateState:
        with self._lock:
            markets = tuple(
                make_window(
                    market_id,
                    tuple(
                        timestamp
                        for timestamp, _generation in self._accepted[market_id]
                    ),
                )
                for market_id in sorted(self._accepted)
                if self._accepted[market_id]
            )
            return make_state(self._last_scheduled_at_utc_us, markets)

    def reserve(
        self: BetfairMarketBookPerMarketRateGate,
        market_ids: Iterable[str],
        *,
        scheduled_at: datetime,
    ) -> MarketBookRateDecision:
        normalized_ids = normalize_market_ids(market_ids)
        scheduled_us = utc_microseconds(scheduled_at)
        reservation_generation: int | None = None
        try:
            with self._lock:
                if (
                    self._last_scheduled_at_utc_us is not None
                    and scheduled_us < self._last_scheduled_at_utc_us
                ):
                    raise ValueError("scheduled_at must not move backwards")

                cutoff = scheduled_us - window_us
                working: dict[str, list[tuple[int, int]]] = {}
                for market_id, accepted in self._accepted.items():
                    retained = [entry for entry in accepted if entry[0] > cutoff]
                    if retained:
                        working[market_id] = retained

                blocked: list[str] = []
                next_eligible: list[int] = []
                for market_id in normalized_ids:
                    accepted = working.get(market_id, [])
                    if len(accepted) >= max_calls:
                        blocked.append(market_id)
                        next_eligible.append(accepted[0][0] + window_us)

                self._accepted = working
                self._last_scheduled_at_utc_us = scheduled_us

                if blocked:
                    return make_decision(
                        market_ids=normalized_ids,
                        scheduled_at_utc_us=scheduled_us,
                        allowed=False,
                        blocked_market_ids=tuple(blocked),
                        next_eligible_at_utc_us=max(next_eligible),
                    )

                reservation_generation = self._next_reservation_generation
                self._next_reservation_generation += 1
                decision = make_decision(
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

    def scheduled_at_property(self: MarketBookRateDecision) -> datetime:
        return datetime_from_utc_microseconds(self.scheduled_at_utc_us)

    def next_eligible_at_property(
        self: MarketBookRateDecision,
    ) -> datetime | None:
        if self.next_eligible_at_utc_us is None:
            return None
        return datetime_from_utc_microseconds(self.next_eligible_at_utc_us)

    def policy_version_property(
        self: BetfairMarketBookPerMarketRateGate,
    ) -> str:
        return policy_version

    decision_type.scheduled_at = property(scheduled_at_property)
    decision_type.next_eligible_at = property(next_eligible_at_property)
    gate_type.__init__ = gate_init
    gate_type.snapshot = snapshot
    gate_type.reserve = reserve
    gate_type.policy_version = property(policy_version_property)


_install_rate_gate_authority()
del _install_rate_gate_authority

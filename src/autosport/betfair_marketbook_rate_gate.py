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
    epoch_naive = datetime_type(1970, 1, 1)
    policy_version = BETFAIR_MARKETBOOK_RATE_POLICY_VERSION
    max_calls = _MAX_CALLS_PER_WINDOW
    window_us = _WINDOW_MICROSECONDS
    object_getattribute = object.__getattribute__
    object_setattr = object.__setattr__
    object_new = object.__new__
    type_of = type
    isinstance_fn = isinstance
    tuple_type = tuple
    set_type = set
    str_type = str
    bytes_type = bytes
    int_type = int
    bool_type = bool
    len_fn = len
    sorted_fn = sorted
    max_fn = max
    any_fn = any
    type_error = TypeError
    value_error = ValueError
    base_exception_type = BaseException
    exception_type = Exception
    getattr_fn = getattr
    callable_fn = callable
    window_market_id_member = window_type.__dict__["market_id"]
    window_accepted_at_member = window_type.__dict__["accepted_at_utc_us"]
    state_policy_version_member = state_type.__dict__["policy_version"]
    state_last_scheduled_member = state_type.__dict__["last_scheduled_at_utc_us"]
    state_markets_member = state_type.__dict__["markets"]
    decision_market_ids_member = decision_type.__dict__["market_ids"]
    decision_scheduled_at_member = decision_type.__dict__["scheduled_at_utc_us"]
    decision_allowed_member = decision_type.__dict__["allowed"]
    decision_blocked_ids_member = decision_type.__dict__["blocked_market_ids"]
    decision_next_eligible_member = decision_type.__dict__["next_eligible_at_utc_us"]
    decision_limit_coverage_member = decision_type.__dict__["provider_limit_coverage_complete"]
    decision_dispatch_authorized_member = decision_type.__dict__["provider_dispatch_authorized"]

    def validate_market_id(value: object) -> str:
        if type_of(value) is not str_type:
            raise type_error("market_id must be exact str")
        if not value or value != value.strip():
            raise value_error("market_id must be non-empty and trimmed")
        return value

    def normalize_market_ids(market_ids: Iterable[str]) -> tuple[str, ...]:
        if isinstance_fn(market_ids, (str_type, bytes_type)):
            raise type_error("market_ids must be an iterable of market ids, not text")
        normalized = tuple_type(validate_market_id(market_id) for market_id in market_ids)
        if not normalized:
            raise value_error("market_ids must not be empty")
        if len_fn(set_type(normalized)) != len_fn(normalized):
            raise value_error("duplicate market_id in one provider request")
        return normalized

    def utc_microseconds(value: object) -> int:
        if type_of(value) is not datetime_type:
            raise type_error("scheduled_at must be exact datetime")
        if value.tzinfo is None:
            raise value_error("scheduled_at must be timezone-aware")
        offset = value.utcoffset()
        if offset is None:
            raise value_error("scheduled_at must be timezone-aware")
        if type_of(offset) is not timedelta_type:
            raise type_error("scheduled_at UTC offset must be exact timedelta")
        local_naive = value.replace(tzinfo=None)
        utc_naive = local_naive - offset
        delta = utc_naive - epoch_naive
        return (
            delta.days * 86_400 * 1_000_000
            + delta.seconds * 1_000_000
            + delta.microseconds
        )

    def datetime_from_utc_microseconds(value: int) -> datetime:
        if type_of(value) is not int_type:
            raise type_error("UTC microsecond timestamp must be a non-boolean int")
        return epoch + timedelta_type(microseconds=value)

    def validate_window(value: object) -> None:
        if type_of(value) is not window_type:
            raise type_error("markets must contain MarketBookRateWindowState values")
        market_id = window_market_id_member.__get__(value, window_type)
        accepted_at_utc_us = window_accepted_at_member.__get__(value, window_type)
        validate_market_id(market_id)
        if type_of(accepted_at_utc_us) is not tuple_type:
            raise type_error("accepted_at_utc_us must be a tuple")
        if not accepted_at_utc_us:
            raise value_error("accepted_at_utc_us must not be empty")
        if len_fn(accepted_at_utc_us) > max_calls:
            raise value_error("rate state exceeds provider maximum calls per window")
        previous: int | None = None
        for timestamp in accepted_at_utc_us:
            if type_of(timestamp) is not int_type:
                raise type_error("accepted rate timestamp must be a non-boolean int")
            if previous is not None and timestamp < previous:
                raise value_error("accepted rate timestamps must be monotonic")
            previous = timestamp

    def validate_state(value: object) -> None:
        if type_of(value) is not state_type:
            raise type_error("state must be MarketBookRateGateState or None")
        policy = state_policy_version_member.__get__(value, state_type)
        last_scheduled_at_utc_us = state_last_scheduled_member.__get__(
            value,
            state_type,
        )
        markets = state_markets_member.__get__(value, state_type)
        if type_of(policy) is not str_type or policy != policy_version:
            raise value_error("unsupported Betfair MarketBook rate policy version")
        if (
            last_scheduled_at_utc_us is not None
            and type_of(last_scheduled_at_utc_us) is not int_type
        ):
            raise type_error(
                "last_scheduled_at_utc_us must be a non-boolean int or None"
            )
        if type_of(markets) is not tuple_type:
            raise type_error("markets must be a tuple")
        if markets and last_scheduled_at_utc_us is None:
            raise value_error("market rate state requires last scheduled time")
        ids: list[str] = []
        for market in markets:
            validate_window(market)
            market_id = window_market_id_member.__get__(market, window_type)
            accepted_at_utc_us = window_accepted_at_member.__get__(
                market,
                window_type,
            )
            ids.append(market_id)
            if last_scheduled_at_utc_us is not None:
                if accepted_at_utc_us[-1] > last_scheduled_at_utc_us:
                    raise value_error(
                        "rate state contains reservation after last scheduled time"
                    )
                cutoff = last_scheduled_at_utc_us - window_us
                if accepted_at_utc_us[0] <= cutoff:
                    raise value_error(
                        "rate state contains reservation outside active window"
                    )
        if ids != sorted_fn(ids):
            raise value_error("market rate state must be sorted by market_id")
        if len_fn(set_type(ids)) != len_fn(ids):
            raise value_error("duplicate market rate state")

    def make_window(
        market_id: str,
        accepted_at_utc_us: tuple[int, ...],
    ) -> MarketBookRateWindowState:
        value = object_new(window_type)
        window_market_id_member.__set__(value, market_id)
        window_accepted_at_member.__set__(value, accepted_at_utc_us)
        validate_window(value)
        return value

    def make_state(
        last_scheduled_at_utc_us: int | None,
        markets: tuple[MarketBookRateWindowState, ...],
    ) -> MarketBookRateGateState:
        value = object_new(state_type)
        state_policy_version_member.__set__(value, policy_version)
        state_last_scheduled_member.__set__(value, last_scheduled_at_utc_us)
        state_markets_member.__set__(value, markets)
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
        if type_of(scheduled_at_utc_us) is not int_type:
            raise type_error("scheduled_at_utc_us must be a non-boolean int")
        if type_of(allowed) is not bool_type:
            raise type_error("allowed must be bool")
        normalized = normalize_market_ids(market_ids)
        if normalized != market_ids:
            raise value_error("market_ids must be canonical")
        if type_of(blocked_market_ids) is not tuple_type:
            raise type_error("blocked_market_ids must be a tuple")
        if blocked_market_ids:
            normalized_blocked = normalize_market_ids(blocked_market_ids)
            if normalized_blocked != blocked_market_ids:
                raise value_error("blocked_market_ids must be canonical")
            if any_fn(market_id not in market_ids for market_id in blocked_market_ids):
                raise value_error("blocked_market_ids must be a subset of market_ids")
        if allowed:
            if blocked_market_ids or next_eligible_at_utc_us is not None:
                raise value_error(
                    "allowed decision cannot carry blocked/next-eligible state"
                )
        else:
            if (
                not blocked_market_ids
                or type_of(next_eligible_at_utc_us) is not int_type
                or next_eligible_at_utc_us <= scheduled_at_utc_us
            ):
                raise value_error(
                    "denied decision requires blocked markets and next-eligible time"
                )
        value = object_new(decision_type)
        decision_market_ids_member.__set__(value, market_ids)
        decision_scheduled_at_member.__set__(value, scheduled_at_utc_us)
        decision_allowed_member.__set__(value, allowed)
        decision_blocked_ids_member.__set__(value, blocked_market_ids)
        decision_next_eligible_member.__set__(value, next_eligible_at_utc_us)
        decision_limit_coverage_member.__set__(value, False)
        decision_dispatch_authorized_member.__set__(value, False)
        return value

    def gate_init(
        self: BetfairMarketBookPerMarketRateGate,
        state: MarketBookRateGateState | None = None,
    ) -> None:
        object_setattr(self, "_lock", lock_type())
        object_setattr(self, "_accepted", {})
        object_setattr(self, "_last_scheduled_at_utc_us", None)
        object_setattr(self, "_next_reservation_generation", 1)
        if state is not None:
            validate_state(state)
            source_markets = state_markets_member.__get__(state, state_type)
            detached_markets = tuple_type(
                make_window(
                    window_market_id_member.__get__(market, window_type),
                    tuple_type(
                        window_accepted_at_member.__get__(
                            market,
                            window_type,
                        )
                    ),
                )
                for market in source_markets
            )
            detached_state = make_state(
                state_last_scheduled_member.__get__(state, state_type),
                detached_markets,
            )
            object_setattr(
                self,
                "_last_scheduled_at_utc_us",
                state_last_scheduled_member.__get__(detached_state, state_type),
            )
            canonical_markets = state_markets_member.__get__(
                detached_state,
                state_type,
            )
            object_setattr(
                self,
                "_accepted",
                {
                    window_market_id_member.__get__(market, window_type): [
                        (timestamp, 0)
                        for timestamp in window_accepted_at_member.__get__(
                            market,
                            window_type,
                        )
                    ]
                    for market in canonical_markets
                },
            )

    def snapshot(
        self: BetfairMarketBookPerMarketRateGate,
    ) -> MarketBookRateGateState:
        lock = object_getattribute(self, "_lock")
        with lock:
            accepted_by_market = object_getattribute(self, "_accepted")
            last_scheduled_at_utc_us = object_getattribute(
                self,
                "_last_scheduled_at_utc_us",
            )
            markets = tuple_type(
                make_window(
                    market_id,
                    tuple_type(
                        timestamp
                        for timestamp, _generation in accepted_by_market[market_id]
                    ),
                )
                for market_id in sorted_fn(accepted_by_market)
                if accepted_by_market[market_id]
            )
            return make_state(last_scheduled_at_utc_us, markets)

    def reserve(
        self: BetfairMarketBookPerMarketRateGate,
        market_ids: Iterable[str],
        *,
        scheduled_at: datetime,
    ) -> MarketBookRateDecision:
        normalized_ids = normalize_market_ids(market_ids)
        scheduled_us = utc_microseconds(scheduled_at)
        lock = object_getattribute(self, "_lock")
        reservation_generation: int | None = None
        try:
            with lock:
                last_scheduled_at_utc_us = object_getattribute(
                    self,
                    "_last_scheduled_at_utc_us",
                )
                if (
                    last_scheduled_at_utc_us is not None
                    and scheduled_us < last_scheduled_at_utc_us
                ):
                    raise value_error("scheduled_at must not move backwards")

                accepted_by_market = object_getattribute(self, "_accepted")
                cutoff = scheduled_us - window_us
                working: dict[str, list[tuple[int, int]]] = {}
                for market_id, accepted in accepted_by_market.items():
                    retained = [entry for entry in accepted if entry[0] > cutoff]
                    if retained:
                        working[market_id] = retained

                blocked: list[str] = []
                next_eligible: list[int] = []
                for market_id in normalized_ids:
                    accepted = working.get(market_id, [])
                    if len_fn(accepted) >= max_calls:
                        blocked.append(market_id)
                        next_eligible.append(accepted[0][0] + window_us)

                object_setattr(self, "_accepted", working)
                object_setattr(self, "_last_scheduled_at_utc_us", scheduled_us)

                if blocked:
                    return make_decision(
                        market_ids=normalized_ids,
                        scheduled_at_utc_us=scheduled_us,
                        allowed=False,
                        blocked_market_ids=tuple_type(blocked),
                        next_eligible_at_utc_us=max_fn(next_eligible),
                    )

                reservation_generation = object_getattribute(
                    self,
                    "_next_reservation_generation",
                )
                object_setattr(
                    self,
                    "_next_reservation_generation",
                    reservation_generation + 1,
                )
                decision = make_decision(
                    market_ids=normalized_ids,
                    scheduled_at_utc_us=scheduled_us,
                    allowed=True,
                )
                for market_id in normalized_ids:
                    working.setdefault(market_id, []).append(
                        (scheduled_us, reservation_generation)
                    )
                return decision
        except base_exception_type as primary:
            if reservation_generation is not None:
                try:
                    with lock:
                        accepted_by_market = object_getattribute(self, "_accepted")
                        for market_id in normalized_ids:
                            accepted = accepted_by_market.get(market_id)
                            if not accepted:
                                continue
                            retained = [
                                entry
                                for entry in accepted
                                if entry[1] != reservation_generation
                            ]
                            if retained:
                                accepted_by_market[market_id] = retained
                            else:
                                accepted_by_market.pop(market_id, None)
                except base_exception_type as cleanup_exc:
                    if (
                        not isinstance_fn(cleanup_exc, exception_type)
                        and isinstance_fn(primary, exception_type)
                    ):
                        raise
                    add_note = getattr_fn(primary, "add_note", None)
                    if callable_fn(add_note):
                        add_note(
                            "rate-reservation cleanup also failed: "
                            f"{type_of(cleanup_exc).__name__}: {cleanup_exc}"
                        )
            raise

    def scheduled_at_property(self: MarketBookRateDecision) -> datetime:
        return datetime_from_utc_microseconds(
            decision_scheduled_at_member.__get__(self, decision_type)
        )

    def next_eligible_at_property(
        self: MarketBookRateDecision,
    ) -> datetime | None:
        next_eligible_at_utc_us = decision_next_eligible_member.__get__(
            self,
            decision_type,
        )
        if next_eligible_at_utc_us is None:
            return None
        return datetime_from_utc_microseconds(next_eligible_at_utc_us)

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

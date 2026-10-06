from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock


BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION = (
    "betfair.list-market-book.local-projection-pressure.v4-generation-safe"
)
_MAX_LOCAL_PROJECTION_REQUESTS_UNRESOLVED = 3
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _validate_request_id(value: object) -> str:
    if type(value) is not str:
        raise TypeError("request_id must be exact str")
    if not value or value != value.strip():
        raise ValueError("request_id must be non-empty and trimmed")
    return value


def _utc_microseconds(value: object, *, name: str) -> int:
    if type(value) is not datetime:
        raise TypeError(f"{name} must be exact datetime")
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
    generation: int

    def __post_init__(self) -> None:
        _validate_request_id(self.request_id)
        if type(self.acquired_at_utc_us) is not int:
            raise TypeError("acquired_at_utc_us must be a non-boolean int")
        if type(self.generation) is not int or self.generation < 1:
            raise ValueError("generation must be a positive non-boolean int")


@dataclass(frozen=True, slots=True)
class MarketBookProjectionConcurrencyState:
    policy_version: str
    last_observed_at_utc_us: int | None
    active: tuple[MarketBookProjectionLease, ...]
    next_lease_generation: int

    def __post_init__(self) -> None:
        if (
            type(self.policy_version) is not str
            or self.policy_version
            != BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION
        ):
            raise ValueError("unsupported Betfair MarketBook projection concurrency policy")
        if self.last_observed_at_utc_us is not None and type(
            self.last_observed_at_utc_us
        ) is not int:
            raise TypeError("last_observed_at_utc_us must be a non-boolean int or None")
        if type(self.active) is not tuple:
            raise TypeError("active must be a tuple")
        if type(self.next_lease_generation) is not int or self.next_lease_generation < 1:
            raise ValueError("next_lease_generation must be a positive non-boolean int")
        if len(self.active) > _MAX_LOCAL_PROJECTION_REQUESTS_UNRESOLVED:
            raise ValueError("projection concurrency state exceeds conservative local maximum")
        if self.active and self.last_observed_at_utc_us is None:
            raise ValueError("active leases require last observed time")
        ids: list[str] = []
        generations: list[int] = []
        for lease in self.active:
            if type(lease) is not MarketBookProjectionLease:
                raise TypeError("active must contain MarketBookProjectionLease values")
            ids.append(lease.request_id)
            generations.append(lease.generation)
            if lease.generation >= self.next_lease_generation:
                raise ValueError("active lease generation must precede next generation")
            if (
                self.last_observed_at_utc_us is not None
                and lease.acquired_at_utc_us > self.last_observed_at_utc_us
            ):
                raise ValueError("lease was acquired after last observed time")
        if ids != sorted(ids):
            raise ValueError("active leases must be sorted by request_id")
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate active request_id")
        if len(set(generations)) != len(generations):
            raise ValueError("duplicate active lease generation")


@dataclass(frozen=True, slots=True)
class MarketBookProjectionConcurrencyDecision:
    request_id: str
    observed_at_utc_us: int
    projection_bearing: bool
    allowed: bool
    active_projection_requests: int
    lease_generation: int | None = None
    provider_limit_coverage_complete: bool = False
    provider_dispatch_authorized: bool = False

    def __post_init__(self) -> None:
        _validate_request_id(self.request_id)
        if type(self.observed_at_utc_us) is not int:
            raise TypeError("observed_at_utc_us must be a non-boolean int")
        if type(self.projection_bearing) is not bool:
            raise TypeError("projection_bearing must be bool")
        if type(self.allowed) is not bool:
            raise TypeError("allowed must be bool")
        if (
            type(self.active_projection_requests) is not int
            or not 0 <= self.active_projection_requests <= _MAX_LOCAL_PROJECTION_REQUESTS_UNRESOLVED
        ):
            raise ValueError("active_projection_requests is outside local bounds")
        if self.projection_bearing and self.allowed:
            if type(self.lease_generation) is not int or self.lease_generation < 1:
                raise ValueError("allowed projection decision requires lease_generation")
        elif self.lease_generation is not None:
            raise ValueError("non-admitted decision cannot carry lease_generation")
        if not self.projection_bearing and not self.allowed:
            raise ValueError("price-only local decision cannot be denied by projection gate")
        if self.provider_limit_coverage_complete is not False:
            raise ValueError("local projection decision cannot claim complete provider-limit coverage")
        if self.provider_dispatch_authorized is not False:
            raise ValueError("local projection decision cannot authorize provider dispatch")


class BetfairMarketBookProjectionConcurrencyGate:
    """Conservative local pressure gate for projection-bearing listMarketBook calls.

    Betfair's current TOO_MANY_REQUESTS guidance is account-scoped rather than a
    standalone exact listMarketBook in-flight counter: projection-bearing
    listMarketBook calls contend with listCurrentOrders and listMarketProfitAndLoss,
    and the provider describes a limit involving queued requests that are ready for
    processing while already-processing requests do not count.

    This kernel therefore does NOT claim to model the provider queue or complete
    account-wide headroom. It deliberately applies a stricter local fail-safe cap of
    three unresolved projection-bearing listMarketBook requests. Price-only reads
    bypass this local bucket. Passing the gate proves only that this product-local
    conservative cap has room; it never proves provider acceptance, complete
    account-scoped limit coverage, or dispatch authorization.

    Active local leases NEVER expire merely because caller time advanced. A
    caller-chosen timeout cannot prove provider queue/processing state or establish
    that another account-scoped operation stopped contending. Slots therefore release
    only through complete for the exact active local request. On restart unresolved
    leases remain blocking; a separate product recovery authority must establish when
    they are safe to resolve. This kernel does no network I/O, scheduling, sleeping,
    retry, cross-operation accounting, or recovery.
    """

    def __init__(
        self,
        *,
        state: MarketBookProjectionConcurrencyState | None = None,
    ) -> None:
        self._lock = RLock()
        self._active: dict[str, MarketBookProjectionLease] = {}
        self._last_observed_at_utc_us: int | None = None
        self._next_lease_generation = 1
        if state is not None:
            if type(state) is not MarketBookProjectionConcurrencyState:
                raise TypeError(
                    "state must be MarketBookProjectionConcurrencyState or None"
                )
            # Revalidate frozen restart DTOs at use time. A caller can mutate a
            # frozen dataclass through object.__setattr__, so construction-time
            # validation alone cannot authorize restored local pressure state.
            if type(state.active) is not tuple:
                raise TypeError("active must be a tuple")
            for lease in state.active:
                if type(lease) is not MarketBookProjectionLease:
                    raise TypeError(
                        "active must contain MarketBookProjectionLease values"
                    )
                MarketBookProjectionLease.__post_init__(lease)
            MarketBookProjectionConcurrencyState.__post_init__(state)
            self._last_observed_at_utc_us = state.last_observed_at_utc_us
            self._active = {lease.request_id: lease for lease in state.active}
            self._next_lease_generation = state.next_lease_generation

    @property
    def policy_version(self) -> str:
        return BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION

    def _require_not_backwards(self, observed_us: int) -> None:
        if (
            self._last_observed_at_utc_us is not None
            and observed_us < self._last_observed_at_utc_us
        ):
            raise ValueError("observed_at must not move backwards")

    def snapshot(self) -> MarketBookProjectionConcurrencyState:
        with self._lock:
            return MarketBookProjectionConcurrencyState(
                policy_version=BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION,
                last_observed_at_utc_us=self._last_observed_at_utc_us,
                active=tuple(
                    self._active[request_id]
                    for request_id in sorted(self._active)
                ),
                next_lease_generation=self._next_lease_generation,
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
        observed_us = _utc_microseconds(observed_at, name="observed_at")
        projection_bearing = has_order_projection or has_match_projection

        generation: int | None = None
        try:
            with self._lock:
                if (
                    self._last_observed_at_utc_us is not None
                    and observed_us < self._last_observed_at_utc_us
                ):
                    raise ValueError("observed_at must not move backwards")
                if request_id in self._active:
                    raise ValueError("request_id is already active")

                if not projection_bearing:
                    decision = MarketBookProjectionConcurrencyDecision(
                        request_id=request_id,
                        observed_at_utc_us=observed_us,
                        projection_bearing=False,
                        allowed=True,
                        active_projection_requests=len(self._active),
                    )
                    self._last_observed_at_utc_us = observed_us
                    return decision

                if len(self._active) >= _MAX_LOCAL_PROJECTION_REQUESTS_UNRESOLVED:
                    decision = MarketBookProjectionConcurrencyDecision(
                        request_id=request_id,
                        observed_at_utc_us=observed_us,
                        projection_bearing=True,
                        allowed=False,
                        active_projection_requests=len(self._active),
                    )
                    self._last_observed_at_utc_us = observed_us
                    return decision

                generation = self._next_lease_generation
                # Consume generation authority before any lease object can become
                # visible. Process control during construction/insertion may burn
                # a generation, but a successor must never reuse one.
                self._next_lease_generation = generation + 1
                lease = MarketBookProjectionLease(
                    request_id=request_id,
                    acquired_at_utc_us=observed_us,
                    generation=generation,
                )
                decision = MarketBookProjectionConcurrencyDecision(
                    request_id=request_id,
                    observed_at_utc_us=observed_us,
                    projection_bearing=True,
                    allowed=True,
                    active_projection_requests=len(self._active) + 1,
                    lease_generation=generation,
                )
                self._active[request_id] = lease
                self._last_observed_at_utc_us = observed_us
                return decision
        except BaseException as primary:
            # A process-control interruption can land after lease mutation but
            # before the decision reaches the caller. Remove only the exact
            # request+generation introduced by this begin attempt. Never rewind
            # causal time or generation counters and never touch a successor.
            if generation is not None:
                try:
                    with self._lock:
                        lease = self._active.get(request_id)
                        if lease is not None and lease.generation == generation:
                            del self._active[request_id]
                except BaseException as cleanup_exc:
                    if (
                        not isinstance(cleanup_exc, Exception)
                        and isinstance(primary, Exception)
                    ):
                        raise
                    add_note = getattr(primary, "add_note", None)
                    if callable(add_note):
                        add_note(
                            "projection-begin cleanup also failed: "
                            f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                        )
            raise

    def complete(
        self,
        request_id: str,
        *,
        lease_generation: int,
        observed_at: datetime,
    ) -> None:
        request_id = _validate_request_id(request_id)
        if type(lease_generation) is not int or lease_generation < 1:
            raise ValueError("lease_generation must be a positive non-boolean int")
        observed_us = _utc_microseconds(observed_at, name="observed_at")

        with self._lock:
            if (
                self._last_observed_at_utc_us is not None
                and observed_us < self._last_observed_at_utc_us
            ):
                raise ValueError("observed_at must not move backwards")
            lease = self._active.get(request_id)
            if lease is None:
                raise ValueError(
                    "request_id is not an active projection-bearing request"
                )
            if lease.generation != lease_generation:
                raise ValueError("lease_generation does not match active request")
            # Publish causal completion time before the destructive release.
            # If process control lands during deletion, restart must not forget
            # that this completion instant was already admitted.
            self._last_observed_at_utc_us = observed_us
            del self._active[request_id]


def _install_projection_gate_authority() -> None:
    """Seal canonical projection pressure operations against later rebinding."""

    gate_type = BetfairMarketBookProjectionConcurrencyGate
    state_type = MarketBookProjectionConcurrencyState
    lease_type = MarketBookProjectionLease
    decision_type = MarketBookProjectionConcurrencyDecision
    lock_type = RLock
    datetime_type = datetime
    utc_timezone = timezone.utc
    epoch = datetime_type(1970, 1, 1, tzinfo=utc_timezone)
    policy_version = BETFAIR_MARKETBOOK_PROJECTION_CONCURRENCY_POLICY_VERSION
    max_unresolved = _MAX_LOCAL_PROJECTION_REQUESTS_UNRESOLVED

    def validate_request_id(value: object) -> str:
        if type(value) is not str:
            raise TypeError("request_id must be exact str")
        if not value or value != value.strip():
            raise ValueError("request_id must be non-empty and trimmed")
        return value

    def utc_microseconds(value: object, *, name: str) -> int:
        if type(value) is not datetime_type:
            raise TypeError(f"{name} must be exact datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
        utc_value = value.astimezone(utc_timezone)
        delta = utc_value - epoch
        return (
            delta.days * 86_400 * 1_000_000
            + delta.seconds * 1_000_000
            + delta.microseconds
        )

    def validate_lease(value: object) -> None:
        if type(value) is not lease_type:
            raise TypeError("active must contain MarketBookProjectionLease values")
        validate_request_id(value.request_id)
        if type(value.acquired_at_utc_us) is not int:
            raise TypeError("acquired_at_utc_us must be a non-boolean int")
        if type(value.generation) is not int or value.generation < 1:
            raise ValueError("generation must be a positive non-boolean int")

    def validate_state(value: object) -> None:
        if type(value) is not state_type:
            raise TypeError(
                "state must be MarketBookProjectionConcurrencyState or None"
            )
        if type(value.policy_version) is not str or value.policy_version != policy_version:
            raise ValueError(
                "unsupported Betfair MarketBook projection concurrency policy"
            )
        if (
            value.last_observed_at_utc_us is not None
            and type(value.last_observed_at_utc_us) is not int
        ):
            raise TypeError(
                "last_observed_at_utc_us must be a non-boolean int or None"
            )
        if type(value.active) is not tuple:
            raise TypeError("active must be a tuple")
        if (
            type(value.next_lease_generation) is not int
            or value.next_lease_generation < 1
        ):
            raise ValueError(
                "next_lease_generation must be a positive non-boolean int"
            )
        if len(value.active) > max_unresolved:
            raise ValueError(
                "projection concurrency state exceeds conservative local maximum"
            )
        if value.active and value.last_observed_at_utc_us is None:
            raise ValueError("active leases require last observed time")
        ids: list[str] = []
        generations: list[int] = []
        for lease in value.active:
            validate_lease(lease)
            ids.append(lease.request_id)
            generations.append(lease.generation)
            if lease.generation >= value.next_lease_generation:
                raise ValueError(
                    "active lease generation must precede next generation"
                )
            if (
                value.last_observed_at_utc_us is not None
                and lease.acquired_at_utc_us > value.last_observed_at_utc_us
            ):
                raise ValueError("lease was acquired after last observed time")
        if ids != sorted(ids):
            raise ValueError("active leases must be sorted by request_id")
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate active request_id")
        if len(set(generations)) != len(generations):
            raise ValueError("duplicate active lease generation")

    def make_lease(
        request_id: str,
        acquired_at_utc_us: int,
        generation: int,
    ) -> MarketBookProjectionLease:
        value = object.__new__(lease_type)
        object.__setattr__(value, "request_id", request_id)
        object.__setattr__(value, "acquired_at_utc_us", acquired_at_utc_us)
        object.__setattr__(value, "generation", generation)
        validate_lease(value)
        return value

    def make_state(
        last_observed_at_utc_us: int | None,
        active: tuple[MarketBookProjectionLease, ...],
        next_lease_generation: int,
    ) -> MarketBookProjectionConcurrencyState:
        value = object.__new__(state_type)
        object.__setattr__(value, "policy_version", policy_version)
        object.__setattr__(
            value,
            "last_observed_at_utc_us",
            last_observed_at_utc_us,
        )
        object.__setattr__(value, "active", active)
        object.__setattr__(
            value,
            "next_lease_generation",
            next_lease_generation,
        )
        validate_state(value)
        return value

    def make_decision(
        *,
        request_id: str,
        observed_at_utc_us: int,
        projection_bearing: bool,
        allowed: bool,
        active_projection_requests: int,
        lease_generation: int | None = None,
    ) -> MarketBookProjectionConcurrencyDecision:
        validate_request_id(request_id)
        if type(observed_at_utc_us) is not int:
            raise TypeError("observed_at_utc_us must be a non-boolean int")
        if type(projection_bearing) is not bool:
            raise TypeError("projection_bearing must be bool")
        if type(allowed) is not bool:
            raise TypeError("allowed must be bool")
        if (
            type(active_projection_requests) is not int
            or not 0 <= active_projection_requests <= max_unresolved
        ):
            raise ValueError("active_projection_requests is outside local bounds")
        if projection_bearing and allowed:
            if type(lease_generation) is not int or lease_generation < 1:
                raise ValueError(
                    "allowed projection decision requires lease_generation"
                )
        elif lease_generation is not None:
            raise ValueError("non-admitted decision cannot carry lease_generation")
        if not projection_bearing and not allowed:
            raise ValueError("price-only local decision cannot be denied by projection gate")
        value = object.__new__(decision_type)
        object.__setattr__(value, "request_id", request_id)
        object.__setattr__(value, "observed_at_utc_us", observed_at_utc_us)
        object.__setattr__(value, "projection_bearing", projection_bearing)
        object.__setattr__(value, "allowed", allowed)
        object.__setattr__(
            value,
            "active_projection_requests",
            active_projection_requests,
        )
        object.__setattr__(value, "lease_generation", lease_generation)
        object.__setattr__(value, "provider_limit_coverage_complete", False)
        object.__setattr__(value, "provider_dispatch_authorized", False)
        return value

    def gate_init(
        self: BetfairMarketBookProjectionConcurrencyGate,
        *,
        state: MarketBookProjectionConcurrencyState | None = None,
    ) -> None:
        self._lock = lock_type()
        self._active = {}
        self._last_observed_at_utc_us = None
        self._next_lease_generation = 1
        if state is not None:
            validate_state(state)
            detached_active = tuple(
                make_lease(
                    lease.request_id,
                    lease.acquired_at_utc_us,
                    lease.generation,
                )
                for lease in state.active
            )
            detached_state = make_state(
                state.last_observed_at_utc_us,
                detached_active,
                state.next_lease_generation,
            )
            self._last_observed_at_utc_us = (
                detached_state.last_observed_at_utc_us
            )
            self._active = {
                lease.request_id: lease for lease in detached_state.active
            }
            self._next_lease_generation = (
                detached_state.next_lease_generation
            )

    def snapshot(
        self: BetfairMarketBookProjectionConcurrencyGate,
    ) -> MarketBookProjectionConcurrencyState:
        with self._lock:
            active = tuple(
                make_lease(
                    self._active[request_id].request_id,
                    self._active[request_id].acquired_at_utc_us,
                    self._active[request_id].generation,
                )
                for request_id in sorted(self._active)
            )
            return make_state(
                self._last_observed_at_utc_us,
                active,
                self._next_lease_generation,
            )

    def begin(
        self: BetfairMarketBookProjectionConcurrencyGate,
        request_id: str,
        *,
        observed_at: datetime,
        has_order_projection: bool,
        has_match_projection: bool,
    ) -> MarketBookProjectionConcurrencyDecision:
        request = validate_request_id(request_id)
        if type(has_order_projection) is not bool:
            raise TypeError("has_order_projection must be bool")
        if type(has_match_projection) is not bool:
            raise TypeError("has_match_projection must be bool")
        observed_us = utc_microseconds(observed_at, name="observed_at")
        projection_bearing = has_order_projection or has_match_projection

        generation: int | None = None
        try:
            with self._lock:
                if (
                    self._last_observed_at_utc_us is not None
                    and observed_us < self._last_observed_at_utc_us
                ):
                    raise ValueError("observed_at must not move backwards")
                if request in self._active:
                    raise ValueError("request_id is already active")

                if not projection_bearing:
                    decision = make_decision(
                        request_id=request,
                        observed_at_utc_us=observed_us,
                        projection_bearing=False,
                        allowed=True,
                        active_projection_requests=len(self._active),
                    )
                    self._last_observed_at_utc_us = observed_us
                    return decision

                if len(self._active) >= max_unresolved:
                    decision = make_decision(
                        request_id=request,
                        observed_at_utc_us=observed_us,
                        projection_bearing=True,
                        allowed=False,
                        active_projection_requests=len(self._active),
                    )
                    self._last_observed_at_utc_us = observed_us
                    return decision

                generation = self._next_lease_generation
                self._next_lease_generation = generation + 1
                lease = make_lease(request, observed_us, generation)
                decision = make_decision(
                    request_id=request,
                    observed_at_utc_us=observed_us,
                    projection_bearing=True,
                    allowed=True,
                    active_projection_requests=len(self._active) + 1,
                    lease_generation=generation,
                )
                self._active[request] = lease
                self._last_observed_at_utc_us = observed_us
                return decision
        except BaseException as primary:
            if generation is not None:
                try:
                    with self._lock:
                        lease = self._active.get(request)
                        if lease is not None and lease.generation == generation:
                            del self._active[request]
                except BaseException as cleanup_exc:
                    if (
                        not isinstance(cleanup_exc, Exception)
                        and isinstance(primary, Exception)
                    ):
                        raise
                    add_note = getattr(primary, "add_note", None)
                    if callable(add_note):
                        add_note(
                            "projection-begin cleanup also failed: "
                            f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                        )
            raise

    def complete(
        self: BetfairMarketBookProjectionConcurrencyGate,
        request_id: str,
        *,
        lease_generation: int,
        observed_at: datetime,
    ) -> None:
        request = validate_request_id(request_id)
        if type(lease_generation) is not int or lease_generation < 1:
            raise ValueError(
                "lease_generation must be a positive non-boolean int"
            )
        observed_us = utc_microseconds(observed_at, name="observed_at")

        with self._lock:
            if (
                self._last_observed_at_utc_us is not None
                and observed_us < self._last_observed_at_utc_us
            ):
                raise ValueError("observed_at must not move backwards")
            lease = self._active.get(request)
            if lease is None:
                raise ValueError(
                    "request_id is not an active projection-bearing request"
                )
            if lease.generation != lease_generation:
                raise ValueError(
                    "lease_generation does not match active request"
                )
            self._last_observed_at_utc_us = observed_us
            del self._active[request]

    def policy_version_property(
        self: BetfairMarketBookProjectionConcurrencyGate,
    ) -> str:
        return policy_version

    gate_type.__init__ = gate_init
    gate_type.snapshot = snapshot
    gate_type.begin = begin
    gate_type.complete = complete
    gate_type.policy_version = property(policy_version_property)


_install_projection_gate_authority()
del _install_projection_gate_authority

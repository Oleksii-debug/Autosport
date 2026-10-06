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
    object_getattribute = object.__getattribute__
    object_setattr = object.__setattr__
    lease_request_id_member = lease_type.__dict__["request_id"]
    lease_acquired_at_member = lease_type.__dict__["acquired_at_utc_us"]
    lease_generation_member = lease_type.__dict__["generation"]
    state_policy_version_member = state_type.__dict__["policy_version"]
    state_last_observed_member = state_type.__dict__["last_observed_at_utc_us"]
    state_active_member = state_type.__dict__["active"]
    state_next_generation_member = state_type.__dict__["next_lease_generation"]
    decision_request_id_member = decision_type.__dict__["request_id"]
    decision_observed_at_member = decision_type.__dict__["observed_at_utc_us"]
    decision_projection_bearing_member = decision_type.__dict__["projection_bearing"]
    decision_allowed_member = decision_type.__dict__["allowed"]
    decision_active_requests_member = decision_type.__dict__["active_projection_requests"]
    decision_lease_generation_member = decision_type.__dict__["lease_generation"]
    decision_limit_coverage_member = decision_type.__dict__["provider_limit_coverage_complete"]
    decision_dispatch_authorized_member = decision_type.__dict__["provider_dispatch_authorized"]

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
        request_id = lease_request_id_member.__get__(value, lease_type)
        acquired_at_utc_us = lease_acquired_at_member.__get__(value, lease_type)
        generation = lease_generation_member.__get__(value, lease_type)
        validate_request_id(request_id)
        if type(acquired_at_utc_us) is not int:
            raise TypeError("acquired_at_utc_us must be a non-boolean int")
        if type(generation) is not int or generation < 1:
            raise ValueError("generation must be a positive non-boolean int")

    def validate_state(value: object) -> None:
        if type(value) is not state_type:
            raise TypeError(
                "state must be MarketBookProjectionConcurrencyState or None"
            )
        policy = state_policy_version_member.__get__(value, state_type)
        last_observed_at_utc_us = state_last_observed_member.__get__(
            value,
            state_type,
        )
        active = state_active_member.__get__(value, state_type)
        next_lease_generation = state_next_generation_member.__get__(
            value,
            state_type,
        )
        if type(policy) is not str or policy != policy_version:
            raise ValueError(
                "unsupported Betfair MarketBook projection concurrency policy"
            )
        if (
            last_observed_at_utc_us is not None
            and type(last_observed_at_utc_us) is not int
        ):
            raise TypeError(
                "last_observed_at_utc_us must be a non-boolean int or None"
            )
        if type(active) is not tuple:
            raise TypeError("active must be a tuple")
        if type(next_lease_generation) is not int or next_lease_generation < 1:
            raise ValueError(
                "next_lease_generation must be a positive non-boolean int"
            )
        if len(active) > max_unresolved:
            raise ValueError(
                "projection concurrency state exceeds conservative local maximum"
            )
        if active and last_observed_at_utc_us is None:
            raise ValueError("active leases require last observed time")
        ids: list[str] = []
        generations: list[int] = []
        for lease in active:
            validate_lease(lease)
            request_id = lease_request_id_member.__get__(lease, lease_type)
            generation = lease_generation_member.__get__(lease, lease_type)
            acquired_at_utc_us = lease_acquired_at_member.__get__(
                lease,
                lease_type,
            )
            ids.append(request_id)
            generations.append(generation)
            if generation >= next_lease_generation:
                raise ValueError(
                    "active lease generation must precede next generation"
                )
            if (
                last_observed_at_utc_us is not None
                and acquired_at_utc_us > last_observed_at_utc_us
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
        lease_request_id_member.__set__(value, request_id)
        lease_acquired_at_member.__set__(value, acquired_at_utc_us)
        lease_generation_member.__set__(value, generation)
        validate_lease(value)
        return value

    def make_state(
        last_observed_at_utc_us: int | None,
        active: tuple[MarketBookProjectionLease, ...],
        next_lease_generation: int,
    ) -> MarketBookProjectionConcurrencyState:
        value = object.__new__(state_type)
        state_policy_version_member.__set__(value, policy_version)
        state_last_observed_member.__set__(value, last_observed_at_utc_us)
        state_active_member.__set__(value, active)
        state_next_generation_member.__set__(value, next_lease_generation)
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
        decision_request_id_member.__set__(value, request_id)
        decision_observed_at_member.__set__(value, observed_at_utc_us)
        decision_projection_bearing_member.__set__(value, projection_bearing)
        decision_allowed_member.__set__(value, allowed)
        decision_active_requests_member.__set__(value, active_projection_requests)
        decision_lease_generation_member.__set__(value, lease_generation)
        decision_limit_coverage_member.__set__(value, False)
        decision_dispatch_authorized_member.__set__(value, False)
        return value

    def gate_init(
        self: BetfairMarketBookProjectionConcurrencyGate,
        *,
        state: MarketBookProjectionConcurrencyState | None = None,
    ) -> None:
        object_setattr(self, "_lock", lock_type())
        object_setattr(self, "_active", {})
        object_setattr(self, "_last_observed_at_utc_us", None)
        object_setattr(self, "_next_lease_generation", 1)
        if state is not None:
            validate_state(state)
            source_active = state_active_member.__get__(state, state_type)
            detached_active = tuple(
                make_lease(
                    lease_request_id_member.__get__(lease, lease_type),
                    lease_acquired_at_member.__get__(lease, lease_type),
                    lease_generation_member.__get__(lease, lease_type),
                )
                for lease in source_active
            )
            detached_state = make_state(
                state_last_observed_member.__get__(state, state_type),
                detached_active,
                state_next_generation_member.__get__(state, state_type),
            )
            object_setattr(
                self,
                "_last_observed_at_utc_us",
                state_last_observed_member.__get__(detached_state, state_type),
            )
            canonical_active = state_active_member.__get__(
                detached_state,
                state_type,
            )
            object_setattr(
                self,
                "_active",
                {
                    lease_request_id_member.__get__(lease, lease_type): lease
                    for lease in canonical_active
                },
            )
            object_setattr(
                self,
                "_next_lease_generation",
                state_next_generation_member.__get__(detached_state, state_type),
            )

    def snapshot(
        self: BetfairMarketBookProjectionConcurrencyGate,
    ) -> MarketBookProjectionConcurrencyState:
        lock = object_getattribute(self, "_lock")
        with lock:
            active_by_request = object_getattribute(self, "_active")
            active = tuple(
                make_lease(
                    lease_request_id_member.__get__(
                        active_by_request[request_id],
                        lease_type,
                    ),
                    lease_acquired_at_member.__get__(
                        active_by_request[request_id],
                        lease_type,
                    ),
                    lease_generation_member.__get__(
                        active_by_request[request_id],
                        lease_type,
                    ),
                )
                for request_id in sorted(active_by_request)
            )
            return make_state(
                object_getattribute(self, "_last_observed_at_utc_us"),
                active,
                object_getattribute(self, "_next_lease_generation"),
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
        lock = object_getattribute(self, "_lock")

        generation: int | None = None
        try:
            with lock:
                active_by_request = object_getattribute(self, "_active")
                last_observed_at_utc_us = object_getattribute(
                    self,
                    "_last_observed_at_utc_us",
                )
                if (
                    last_observed_at_utc_us is not None
                    and observed_us < last_observed_at_utc_us
                ):
                    raise ValueError("observed_at must not move backwards")
                if request in active_by_request:
                    raise ValueError("request_id is already active")

                if not projection_bearing:
                    decision = make_decision(
                        request_id=request,
                        observed_at_utc_us=observed_us,
                        projection_bearing=False,
                        allowed=True,
                        active_projection_requests=len(active_by_request),
                    )
                    object_setattr(self, "_last_observed_at_utc_us", observed_us)
                    return decision

                if len(active_by_request) >= max_unresolved:
                    decision = make_decision(
                        request_id=request,
                        observed_at_utc_us=observed_us,
                        projection_bearing=True,
                        allowed=False,
                        active_projection_requests=len(active_by_request),
                    )
                    object_setattr(self, "_last_observed_at_utc_us", observed_us)
                    return decision

                generation = object_getattribute(self, "_next_lease_generation")
                object_setattr(self, "_next_lease_generation", generation + 1)
                lease = make_lease(request, observed_us, generation)
                decision = make_decision(
                    request_id=request,
                    observed_at_utc_us=observed_us,
                    projection_bearing=True,
                    allowed=True,
                    active_projection_requests=len(active_by_request) + 1,
                    lease_generation=generation,
                )
                active_by_request[request] = lease
                object_setattr(self, "_last_observed_at_utc_us", observed_us)
                return decision
        except BaseException as primary:
            if generation is not None:
                try:
                    with lock:
                        active_by_request = object_getattribute(self, "_active")
                        lease = active_by_request.get(request)
                        if (
                            lease is not None
                            and lease_generation_member.__get__(lease, lease_type)
                            == generation
                        ):
                            del active_by_request[request]
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
        lock = object_getattribute(self, "_lock")

        with lock:
            last_observed_at_utc_us = object_getattribute(
                self,
                "_last_observed_at_utc_us",
            )
            if (
                last_observed_at_utc_us is not None
                and observed_us < last_observed_at_utc_us
            ):
                raise ValueError("observed_at must not move backwards")
            active_by_request = object_getattribute(self, "_active")
            lease = active_by_request.get(request)
            if lease is None:
                raise ValueError(
                    "request_id is not an active projection-bearing request"
                )
            if lease_generation_member.__get__(lease, lease_type) != lease_generation:
                raise ValueError(
                    "lease_generation does not match active request"
                )
            object_setattr(self, "_last_observed_at_utc_us", observed_us)
            del active_by_request[request]

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

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Final

from .providers import ProviderUnavailableError


class MatchbookRateGovernorError(ValueError):
    """Invalid local rate-governor configuration or caller input."""


class MatchbookRateDeferred(ProviderUnavailableError):
    """Typed provider degradation: the request must not be sent yet."""

    def __init__(
        self,
        *,
        bucket: "MatchbookRateBucket",
        reason: str,
        retry_after_seconds: float | None,
    ) -> None:
        self.bucket = bucket
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
        suffix = (
            ""
            if retry_after_seconds is None
            else f"; retry_after_seconds={retry_after_seconds:.6f}"
        )
        super().__init__(
            f"Matchbook {bucket.value} request deferred: {reason}{suffix}"
        )


class MatchbookRateBucket(str, Enum):
    ACCOUNT = "account"
    EVENTS = "events"
    SECURITY = "security"
    NAVIGATION = "navigation"
    REPORTS = "reports"
    BETTING_WRITE = "betting_write"


class MatchbookRatePriority(str, Enum):
    BACKGROUND_READ = "background_read"
    SAFETY_READ = "safety_read"


_DOCUMENTED_MIN_WINDOW_SECONDS: Final = 60.0
_DOCUMENTED_DEFAULT_BLOCK_SECONDS: Final = 600.0
_DOCUMENTED_READ_CAPACITY_PER_MINUTE: Final[dict[MatchbookRateBucket, int]] = {
    MatchbookRateBucket.ACCOUNT: 300,
    MatchbookRateBucket.EVENTS: 700,
    MatchbookRateBucket.SECURITY: 200,
    MatchbookRateBucket.NAVIGATION: 100,
    MatchbookRateBucket.REPORTS: 40,
}


@dataclass(frozen=True, slots=True)
class MatchbookRatePolicy:
    """Conservative local budget for one documented Matchbook API group."""

    capacity: int
    window_seconds: float
    safety_read_reserve: int = 0
    cold_start_seconds: float | None = None

    def __post_init__(self) -> None:
        if type(self.capacity) is not int or self.capacity <= 0:
            raise MatchbookRateGovernorError(
                "capacity must be a positive non-boolean integer"
            )
        window = _positive_finite(self.window_seconds, "window_seconds")
        if (
            type(self.safety_read_reserve) is not int
            or self.safety_read_reserve < 0
            or self.safety_read_reserve >= self.capacity
        ):
            raise MatchbookRateGovernorError(
                "safety_read_reserve must be a non-negative integer "
                "strictly smaller than capacity"
            )
        minimum_cold_start = max(
            window,
            _DOCUMENTED_DEFAULT_BLOCK_SECONDS,
        )
        cold = self.cold_start_seconds
        if cold is None:
            cold = minimum_cold_start
        else:
            cold = _nonnegative_finite(cold, "cold_start_seconds")
            if cold < minimum_cold_start:
                raise MatchbookRateGovernorError(
                    "cold_start_seconds must cover both the configured window "
                    "and Matchbook's documented default provider blocking period"
                )
        object.__setattr__(self, "window_seconds", window)
        object.__setattr__(self, "cold_start_seconds", cold)


@dataclass(frozen=True, slots=True)
class MatchbookRateAdmission:
    """Audit-only local admission receipt, never execution authority."""

    governor_id: str
    bucket: MatchbookRateBucket
    priority: MatchbookRatePriority
    sequence: int
    admitted_at: float
    remaining_total: int
    remaining_background: int
    grants_execution_authority: bool = field(init=False, default=False)


@dataclass(frozen=True, slots=True)
class MatchbookRateSnapshot:
    governor_id: str
    bucket: MatchbookRateBucket
    active_requests: int
    capacity: int
    safety_read_reserve: int
    blocked_for_seconds: float
    clock_failed_closed: bool
    multi_process_safe: bool = field(init=False, default=False)


Clock = Callable[[], float]


@dataclass(slots=True)
class _BucketState:
    admitted_at: deque[float]
    blocked_until: float


@dataclass(slots=True)
class _SharedScopeState:
    scope_kind: str
    scope_id: str
    fingerprint: tuple[tuple[str, int, float, int, float], ...]
    clock: Clock
    states: dict[MatchbookRateBucket, _BucketState]
    provider_blocked_until: float
    last_now: float
    clock_failed_closed: bool = False


_REGISTRY_LOCK: Final = threading.RLock()
_CANONICAL_GOVERNORS: Final[
    dict[tuple[str, str], "MatchbookRateGovernor"]
] = {}
_ACCOUNT_STATES: Final[dict[str, _SharedScopeState]] = {}
_NETWORK_STATES: Final[dict[str, _SharedScopeState]] = {}
_CONSTRUCTION_TOKEN: Final = object()


def _positive_finite(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MatchbookRateGovernorError(f"{field_name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise MatchbookRateGovernorError(
            f"{field_name} must be finite and positive"
        )
    return result


def _nonnegative_finite(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MatchbookRateGovernorError(f"{field_name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise MatchbookRateGovernorError(
            f"{field_name} must be finite and non-negative"
        )
    return result


def _scope_id(value: object, field_name: str = "scope_id") -> str:
    if not isinstance(value, str):
        raise MatchbookRateGovernorError(f"{field_name} must be str")
    if not value or value != value.strip() or len(value) > 256:
        raise MatchbookRateGovernorError(
            f"{field_name} must be a non-empty trimmed non-secret identifier"
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise MatchbookRateGovernorError(
            f"{field_name} contains a control character"
        )
    return value


def _normalize_policies(
    policies: Mapping[MatchbookRateBucket, MatchbookRatePolicy],
) -> dict[MatchbookRateBucket, MatchbookRatePolicy]:
    if not isinstance(policies, Mapping) or not policies:
        raise MatchbookRateGovernorError(
            "at least one read bucket policy is required"
        )
    result: dict[MatchbookRateBucket, MatchbookRatePolicy] = {}
    for bucket, policy in policies.items():
        if not isinstance(bucket, MatchbookRateBucket):
            raise MatchbookRateGovernorError(
                "policy keys must be MatchbookRateBucket values"
            )
        if bucket is MatchbookRateBucket.BETTING_WRITE:
            raise MatchbookRateGovernorError(
                "Betting Write budget is intentionally unsupported by "
                "the read-only governor"
            )
        if type(policy) is not MatchbookRatePolicy:
            raise MatchbookRateGovernorError(
                "policy values must be exact MatchbookRatePolicy values"
            )
        documented_capacity = _DOCUMENTED_READ_CAPACITY_PER_MINUTE[bucket]
        if policy.capacity > documented_capacity:
            raise MatchbookRateGovernorError(
                f"{bucket.value} capacity exceeds Matchbook's documented "
                f"{documented_capacity} requests-per-minute limit"
            )
        if policy.window_seconds < _DOCUMENTED_MIN_WINDOW_SECONDS:
            raise MatchbookRateGovernorError(
                f"{bucket.value} window_seconds must be at least "
                f"{_DOCUMENTED_MIN_WINDOW_SECONDS:.0f} seconds to preserve "
                "Matchbook's documented per-minute limit"
            )
        result[bucket] = policy
    return result


def _policy_fingerprint(
    policies: Mapping[MatchbookRateBucket, MatchbookRatePolicy],
) -> tuple[tuple[str, int, float, int, float], ...]:
    return tuple(
        (
            bucket.value,
            policy.capacity,
            policy.window_seconds,
            policy.safety_read_reserve,
            float(policy.cold_start_seconds),
        )
        for bucket, policy in sorted(
            policies.items(), key=lambda item: item[0].value
        )
    )


def _governor_id(
    account_scope_id: str,
    network_scope_id: str,
    fingerprint: tuple[tuple[str, int, float, int, float], ...],
) -> str:
    payload = repr(
        (account_scope_id, network_scope_id, fingerprint)
    ).encode("utf-8")
    return "matchbook-rate:" + hashlib.sha256(payload).hexdigest()


def _read_clock(clock: Clock) -> float:
    try:
        value = clock()
    except Exception as exc:
        raise MatchbookRateGovernorError("monotonic clock failed") from exc
    return _nonnegative_finite(value, "monotonic clock")


def _new_shared_scope_state(
    *,
    scope_kind: str,
    scope_id: str,
    policies: Mapping[MatchbookRateBucket, MatchbookRatePolicy],
    fingerprint: tuple[tuple[str, int, float, int, float], ...],
    clock: Clock,
    now: float,
) -> _SharedScopeState:
    return _SharedScopeState(
        scope_kind=scope_kind,
        scope_id=scope_id,
        fingerprint=fingerprint,
        clock=clock,
        states={
            bucket: _BucketState(
                admitted_at=deque(),
                blocked_until=now + float(policy.cold_start_seconds),
            )
            for bucket, policy in policies.items()
        },
        provider_blocked_until=now,
        last_now=now,
    )


def _validate_shared_scope_binding(
    *,
    registry: dict[str, _SharedScopeState],
    scope_kind: str,
    scope_id: str,
    fingerprint: tuple[tuple[str, int, float, int, float], ...],
    clock: Clock,
) -> _SharedScopeState | None:
    existing = registry.get(scope_id)
    if existing is None:
        return None
    if existing.fingerprint != fingerprint:
        raise MatchbookRateGovernorError(
            f"same Matchbook {scope_kind} scope cannot be rebound to a "
            "different rate policy"
        )
    if existing.clock is not clock:
        raise MatchbookRateGovernorError(
            f"same Matchbook {scope_kind} scope cannot be rebound to a "
            "different clock"
        )
    return existing


def _unknown_network_scope(
    fingerprint: tuple[tuple[str, int, float, int, float], ...],
    clock: Clock,
) -> str:
    # Backwards-compatible callers that do not yet provide an egress identity
    # share one conservative unknown-network budget when they use the same
    # policy/clock. This avoids multiplying allowance by arbitrary account
    # aliases while canonical adapter composition migrates to explicit egress.
    payload = repr((fingerprint, id(clock))).encode("utf-8")
    return "matchbook-network:unknown:" + hashlib.sha256(payload).hexdigest()


class MatchbookRateGovernor:
    """Process-shared Matchbook read-admission authority.

    Resolve through resolve_matchbook_rate_governor(). Every admission is
    debited atomically from both a provider-account scope and a network-egress
    scope. Governors sharing either dimension therefore share that documented
    provider-group allowance inside this process.

    The governor never sleeps, retries, performs HTTP, stores credentials, or
    grants provider-write/execution authority. It remains explicitly not
    cross-process safe.
    """

    def __init__(
        self,
        account_scope_id: str,
        network_scope_id: str,
        policies: Mapping[MatchbookRateBucket, MatchbookRatePolicy],
        *,
        clock: Clock,
        account_state: _SharedScopeState,
        network_state: _SharedScopeState,
        _registry_token: object,
    ) -> None:
        if _registry_token is not _CONSTRUCTION_TOKEN:
            raise MatchbookRateGovernorError(
                "MatchbookRateGovernor must be resolved through "
                "resolve_matchbook_rate_governor()"
            )
        self.account_scope_id = account_scope_id
        self.network_scope_id = network_scope_id
        self._policies = dict(policies)
        self._fingerprint = _policy_fingerprint(self._policies)
        self.governor_id = _governor_id(
            account_scope_id,
            network_scope_id,
            self._fingerprint,
        )
        self._clock = clock
        self._account_state = account_state
        self._network_state = network_state
        self._sequence = 0

    @property
    def multi_process_safe(self) -> bool:
        return False

    @property
    def configured_buckets(self) -> tuple[MatchbookRateBucket, ...]:
        return tuple(sorted(self._policies, key=lambda item: item.value))

    def admit(
        self,
        bucket: MatchbookRateBucket,
        *,
        priority: MatchbookRatePriority = MatchbookRatePriority.BACKGROUND_READ,
    ) -> MatchbookRateAdmission:
        if not isinstance(priority, MatchbookRatePriority):
            raise MatchbookRateGovernorError(
                "priority must be MatchbookRatePriority"
            )
        with _REGISTRY_LOCK:
            policy, account_bucket, network_bucket = self._state_for(bucket)
            now = self._now(bucket)
            self._prune(account_bucket, policy, now)
            self._prune(network_bucket, policy, now)

            blocked_until = max(
                self._account_state.provider_blocked_until,
                account_bucket.blocked_until,
                self._network_state.provider_blocked_until,
                network_bucket.blocked_until,
            )
            if blocked_until > now:
                raise MatchbookRateDeferred(
                    bucket=bucket,
                    reason="provider_rate_window_blocked",
                    retry_after_seconds=blocked_until - now,
                )

            account_active = len(account_bucket.admitted_at)
            network_active = len(network_bucket.admitted_at)
            if priority is MatchbookRatePriority.BACKGROUND_READ:
                limit = policy.capacity - policy.safety_read_reserve
            else:
                limit = policy.capacity

            exhausted: list[tuple[_BucketState, int]] = []
            if account_active >= limit:
                exhausted.append((account_bucket, account_active))
            if network_active >= limit:
                exhausted.append((network_bucket, network_active))
            if exhausted:
                retry_at = max(
                    state.admitted_at[active - limit] + policy.window_seconds
                    for state, active in exhausted
                )
                raise MatchbookRateDeferred(
                    bucket=bucket,
                    reason=(
                        "safety_read_capacity_reserved"
                        if priority is MatchbookRatePriority.BACKGROUND_READ
                        and policy.safety_read_reserve
                        else "provider_rate_capacity_exhausted"
                    ),
                    retry_after_seconds=max(0.0, retry_at - now),
                )

            account_bucket.admitted_at.append(now)
            network_bucket.admitted_at.append(now)
            self._sequence += 1
            account_after = len(account_bucket.admitted_at)
            network_after = len(network_bucket.admitted_at)
            background_limit = policy.capacity - policy.safety_read_reserve
            return MatchbookRateAdmission(
                governor_id=self.governor_id,
                bucket=bucket,
                priority=priority,
                sequence=self._sequence,
                admitted_at=now,
                remaining_total=max(
                    0,
                    min(
                        policy.capacity - account_after,
                        policy.capacity - network_after,
                    ),
                ),
                remaining_background=max(
                    0,
                    min(
                        background_limit - account_after,
                        background_limit - network_after,
                    ),
                ),
            )

    def observe_throttle(
        self,
        bucket: MatchbookRateBucket,
        *,
        retry_after_seconds: float,
        provider_wide: bool = False,
    ) -> None:
        delay = max(
            _DOCUMENTED_DEFAULT_BLOCK_SECONDS,
            _nonnegative_finite(
                retry_after_seconds, "retry_after_seconds"
            ),
        )
        if type(provider_wide) is not bool:
            raise MatchbookRateGovernorError("provider_wide must be bool")
        with _REGISTRY_LOCK:
            _, account_bucket, network_bucket = self._state_for(bucket)
            now = self._now(bucket)
            blocked_until = now + delay
            account_bucket.blocked_until = max(
                account_bucket.blocked_until,
                blocked_until,
            )
            network_bucket.blocked_until = max(
                network_bucket.blocked_until,
                blocked_until,
            )
            if provider_wide:
                self._account_state.provider_blocked_until = max(
                    self._account_state.provider_blocked_until,
                    blocked_until,
                )
                self._network_state.provider_blocked_until = max(
                    self._network_state.provider_blocked_until,
                    blocked_until,
                )

    def observe_success(
        self,
        bucket: MatchbookRateBucket,
    ) -> MatchbookRateSnapshot:
        """Never mint allowance; expose state after provider success."""

        return self.snapshot(bucket)

    def snapshot(
        self,
        bucket: MatchbookRateBucket,
    ) -> MatchbookRateSnapshot:
        with _REGISTRY_LOCK:
            policy, account_bucket, network_bucket = self._state_for(bucket)
            now = self._now(bucket)
            self._prune(account_bucket, policy, now)
            self._prune(network_bucket, policy, now)
            blocked_until = max(
                self._account_state.provider_blocked_until,
                account_bucket.blocked_until,
                self._network_state.provider_blocked_until,
                network_bucket.blocked_until,
            )
            return MatchbookRateSnapshot(
                governor_id=self.governor_id,
                bucket=bucket,
                active_requests=max(
                    len(account_bucket.admitted_at),
                    len(network_bucket.admitted_at),
                ),
                capacity=policy.capacity,
                safety_read_reserve=policy.safety_read_reserve,
                blocked_for_seconds=max(0.0, blocked_until - now),
                clock_failed_closed=(
                    self._account_state.clock_failed_closed
                    or self._network_state.clock_failed_closed
                ),
            )

    def _state_for(
        self,
        bucket: MatchbookRateBucket,
    ) -> tuple[MatchbookRatePolicy, _BucketState, _BucketState]:
        if not isinstance(bucket, MatchbookRateBucket):
            raise MatchbookRateGovernorError(
                "bucket must be MatchbookRateBucket"
            )
        try:
            return (
                self._policies[bucket],
                self._account_state.states[bucket],
                self._network_state.states[bucket],
            )
        except KeyError as exc:
            raise MatchbookRateGovernorError(
                f"bucket {bucket.value} is not configured"
            ) from exc

    @staticmethod
    def _prune(
        state: _BucketState,
        policy: MatchbookRatePolicy,
        now: float,
    ) -> None:
        cutoff = now - policy.window_seconds
        while state.admitted_at and state.admitted_at[0] <= cutoff:
            state.admitted_at.popleft()

    def _now(self, bucket: MatchbookRateBucket) -> float:
        if (
            self._account_state.clock_failed_closed
            or self._network_state.clock_failed_closed
        ):
            raise MatchbookRateDeferred(
                bucket=bucket,
                reason="monotonic_clock_failed_closed",
                retry_after_seconds=None,
            )
        now = _read_clock(self._clock)
        if (
            now < self._account_state.last_now
            or now < self._network_state.last_now
        ):
            self._account_state.clock_failed_closed = True
            self._network_state.clock_failed_closed = True
            raise MatchbookRateDeferred(
                bucket=bucket,
                reason="monotonic_clock_rollback",
                retry_after_seconds=None,
            )
        self._account_state.last_now = now
        self._network_state.last_now = now
        return now


def resolve_matchbook_rate_governor(
    account_scope_id: str,
    policies: Mapping[MatchbookRateBucket, MatchbookRatePolicy],
    *,
    network_scope_id: str | None = None,
    clock: Clock = time.monotonic,
) -> MatchbookRateGovernor:
    """Resolve one canonical governor over account and network dimensions.

    Supplying network_scope_id is the canonical integration path. Legacy
    callers that omit it are placed into a conservative process-shared unknown
    network scope for their exact policy/clock, rather than receiving an
    independent full network allowance for every arbitrary account label.
    """

    account_scope = _scope_id(account_scope_id, "account_scope_id")
    normalized = _normalize_policies(policies)
    fingerprint = _policy_fingerprint(normalized)
    if not callable(clock):
        raise MatchbookRateGovernorError("clock must be callable")
    if network_scope_id is None:
        network_scope = _unknown_network_scope(fingerprint, clock)
    else:
        network_scope = _scope_id(network_scope_id, "network_scope_id")

    with _REGISTRY_LOCK:
        existing = _CANONICAL_GOVERNORS.get(
            (account_scope, network_scope)
        )
        if existing is not None:
            if existing._fingerprint != fingerprint:
                raise MatchbookRateGovernorError(
                    "same Matchbook account/network scope cannot be rebound "
                    "to a different rate policy"
                )
            if existing._clock is not clock:
                raise MatchbookRateGovernorError(
                    "same Matchbook account/network scope cannot be rebound "
                    "to a different clock"
                )
            return existing

        # Validate both dimensions before creating either. A rejected resolve
        # must not leave one orphaned binding that changes future authority.
        account_state = _validate_shared_scope_binding(
            registry=_ACCOUNT_STATES,
            scope_kind="account",
            scope_id=account_scope,
            fingerprint=fingerprint,
            clock=clock,
        )
        network_state = _validate_shared_scope_binding(
            registry=_NETWORK_STATES,
            scope_kind="network",
            scope_id=network_scope,
            fingerprint=fingerprint,
            clock=clock,
        )
        now = _read_clock(clock)
        if account_state is None:
            account_state = _new_shared_scope_state(
                scope_kind="account",
                scope_id=account_scope,
                policies=normalized,
                fingerprint=fingerprint,
                clock=clock,
                now=now,
            )
            _ACCOUNT_STATES[account_scope] = account_state
        if network_state is None:
            network_state = _new_shared_scope_state(
                scope_kind="network",
                scope_id=network_scope,
                policies=normalized,
                fingerprint=fingerprint,
                clock=clock,
                now=now,
            )
            _NETWORK_STATES[network_scope] = network_state

        governor = MatchbookRateGovernor(
            account_scope,
            network_scope,
            normalized,
            clock=clock,
            account_state=account_state,
            network_state=network_state,
            _registry_token=_CONSTRUCTION_TOKEN,
        )
        _CANONICAL_GOVERNORS[
            (account_scope, network_scope)
        ] = governor
        return governor

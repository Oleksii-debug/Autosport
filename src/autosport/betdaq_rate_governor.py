from __future__ import annotations

import hashlib
import json
import math
import threading
import time
import weakref
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Final


class BetdaqRateGovernorError(ValueError):
    """Invalid BETDAQ rate-governor policy, scope, or evidence."""


class BetdaqRateDeferred(RuntimeError):
    """Typed fail-closed denial: the caller must not dispatch the provider call."""

    def __init__(
        self,
        *,
        method: "BetdaqApiMethod",
        reason: str,
        retry_after_seconds: float | None,
    ) -> None:
        self.method = method
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
        suffix = (
            ""
            if retry_after_seconds is None
            else f"; retry_after_seconds={retry_after_seconds:.6f}"
        )
        super().__init__(f"BETDAQ {method.value} request deferred: {reason}{suffix}")


class BetdaqApiMethod(str, Enum):
    GET_PRICES = "GetPrices"
    GET_EVENT_SUBTREE_NO_SELECTIONS = "GetEventSubTreeNoSelections"
    GET_EVENT_SUBTREE_WITH_SELECTIONS = "GetEventSubTreeWithSelections"
    LIST_BOOTSTRAP_ORDERS = "ListBootstrapOrders"
    LIST_ORDERS_CHANGED_SINCE = "ListOrdersChangedSince"
    PLACE_ORDERS_NO_RECEIPT = "PlaceOrdersNoReceipt"
    PLACE_ORDERS_WITH_RECEIPT = "PlaceOrdersWithReceipt"
    CHANGE_ORDER_NO_RECEIPT = "ChangeOrderNoReceipt"
    LIST_BLACKLIST_INFORMATION = "ListBlacklistInformation"


class BetdaqRatePriority(str, Enum):
    BACKGROUND = "background"
    RECONCILIATION = "reconciliation"
    SAFETY = "safety"
    EXECUTION_CRITICAL = "execution_critical"


class BetdaqRateTier(str, Enum):
    UNKNOWN = "unknown"
    DEFAULT = "default"
    STANDARD = "standard"


# Public Calls and Fees table. "Any" is intentionally not modeled because the
# public page does not define its exact scope/interaction with Combined.
_DOCUMENTED_DEFAULT_PER_MINUTE: Final[dict[BetdaqApiMethod, int]] = {
    BetdaqApiMethod.GET_PRICES: 130,
    BetdaqApiMethod.GET_EVENT_SUBTREE_NO_SELECTIONS: 25,
    BetdaqApiMethod.GET_EVENT_SUBTREE_WITH_SELECTIONS: 25,
    BetdaqApiMethod.LIST_BOOTSTRAP_ORDERS: 50,
    BetdaqApiMethod.LIST_ORDERS_CHANGED_SINCE: 130,
    BetdaqApiMethod.PLACE_ORDERS_NO_RECEIPT: 100,
    BetdaqApiMethod.PLACE_ORDERS_WITH_RECEIPT: 20,
    BetdaqApiMethod.CHANGE_ORDER_NO_RECEIPT: 100,
}
_DOCUMENTED_STANDARD_PER_MINUTE: Final[dict[BetdaqApiMethod, int]] = {
    BetdaqApiMethod.GET_PRICES: 2000,
    BetdaqApiMethod.GET_EVENT_SUBTREE_NO_SELECTIONS: 50,
    BetdaqApiMethod.GET_EVENT_SUBTREE_WITH_SELECTIONS: 50,
    BetdaqApiMethod.LIST_BOOTSTRAP_ORDERS: 50,
    BetdaqApiMethod.LIST_ORDERS_CHANGED_SINCE: 600,
    BetdaqApiMethod.PLACE_ORDERS_NO_RECEIPT: 800,
    BetdaqApiMethod.PLACE_ORDERS_WITH_RECEIPT: 800,
    BetdaqApiMethod.CHANGE_ORDER_NO_RECEIPT: 800,
}
_DOCUMENTED_DEFAULT_COMBINED_PER_MINUTE: Final = 300
_DOCUMENTED_STANDARD_COMBINED_PER_MINUTE: Final = 3000
_WINDOW_SECONDS: Final = 60.0
_POLICY_VERSION: Final = "betdaq-calls-fees-2026-09-23"


@dataclass(frozen=True, slots=True)
class BetdaqRatePolicy:
    """Conservative product policy bounded by documented provider ceilings."""

    method_capacity: Mapping[BetdaqApiMethod, int]
    combined_capacity: int
    safety_reserve: int = 0
    reconciliation_reserve: int = 0
    cold_start_seconds: float = _WINDOW_SECONDS
    revision: str = "default-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.method_capacity, Mapping) or not self.method_capacity:
            raise BetdaqRateGovernorError(
                "method_capacity must be a non-empty mapping"
            )
        normalized: dict[BetdaqApiMethod, int] = {}
        for method, capacity in self.method_capacity.items():
            if not isinstance(method, BetdaqApiMethod):
                raise BetdaqRateGovernorError(
                    "method_capacity keys must be BetdaqApiMethod"
                )
            if method is BetdaqApiMethod.LIST_BLACKLIST_INFORMATION:
                raise BetdaqRateGovernorError(
                    "ListBlacklistInformation has no documented public method ceiling"
                )
            if type(capacity) is not int or capacity <= 0:
                raise BetdaqRateGovernorError(
                    "method capacities must be positive non-boolean integers"
                )
            documented = _DOCUMENTED_DEFAULT_PER_MINUTE[method]
            if capacity > documented:
                raise BetdaqRateGovernorError(
                    f"{method.value} capacity exceeds documented DEFAULT ceiling "
                    f"{documented}/minute"
                )
            normalized[method] = capacity
        if type(self.combined_capacity) is not int or self.combined_capacity <= 0:
            raise BetdaqRateGovernorError(
                "combined_capacity must be a positive non-boolean integer"
            )
        if self.combined_capacity > _DOCUMENTED_DEFAULT_COMBINED_PER_MINUTE:
            raise BetdaqRateGovernorError(
                "combined_capacity exceeds documented DEFAULT Combined ceiling 300/minute"
            )
        for field_name, value in (
            ("safety_reserve", self.safety_reserve),
            ("reconciliation_reserve", self.reconciliation_reserve),
        ):
            if type(value) is not int or value < 0:
                raise BetdaqRateGovernorError(
                    f"{field_name} must be a non-negative non-boolean integer"
                )
        if self.safety_reserve + self.reconciliation_reserve >= self.combined_capacity:
            raise BetdaqRateGovernorError(
                "combined reserves must be strictly smaller than combined_capacity"
            )
        cold = _finite_nonnegative(self.cold_start_seconds, "cold_start_seconds")
        if cold < _WINDOW_SECONDS:
            raise BetdaqRateGovernorError(
                "cold_start_seconds must be at least one documented 60-second window"
            )
        if (
            not isinstance(self.revision, str)
            or not self.revision
            or self.revision != self.revision.strip()
        ):
            raise BetdaqRateGovernorError(
                "revision must be a non-empty trimmed string"
            )
        object.__setattr__(self, "method_capacity", normalized)
        object.__setattr__(self, "cold_start_seconds", cold)

    @classmethod
    def documented_default(
        cls,
        *,
        safety_reserve: int = 0,
        reconciliation_reserve: int = 0,
        cold_start_seconds: float = _WINDOW_SECONDS,
        revision: str = "default-v1",
    ) -> "BetdaqRatePolicy":
        return cls(
            method_capacity=dict(_DOCUMENTED_DEFAULT_PER_MINUTE),
            combined_capacity=_DOCUMENTED_DEFAULT_COMBINED_PER_MINUTE,
            safety_reserve=safety_reserve,
            reconciliation_reserve=reconciliation_reserve,
            cold_start_seconds=cold_start_seconds,
            revision=revision,
        )


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetdaqBlacklistEvidence:
    """Sanitized provider blacklist observation; no credentials/raw SOAP allowed."""

    provider: str
    api_method: BetdaqApiMethod
    remaining_ms: int
    observation_id: str
    source_authority_proven: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        if self.provider != "BETDAQ":
            raise BetdaqRateGovernorError(
                "blacklist evidence provider must be BETDAQ"
            )
        if type(self.remaining_ms) is not int or self.remaining_ms < 0:
            raise BetdaqRateGovernorError(
                "remaining_ms must be a non-negative non-boolean integer"
            )
        if (
            not isinstance(self.observation_id, str)
            or not self.observation_id
            or self.observation_id != self.observation_id.strip()
        ):
            raise BetdaqRateGovernorError(
                "observation_id must be a non-empty trimmed string"
            )


@dataclass(frozen=True, slots=True)
class BetdaqRateAdmission:
    provider: str
    governor_id: str
    account_scope_id: str
    method: BetdaqApiMethod
    priority: BetdaqRatePriority
    policy_version: str
    policy_revision: str
    tier: BetdaqRateTier
    sequence: int
    admitted_at_monotonic: float
    admitted_at_utc: float
    method_active_count: int
    combined_active_count: int
    receipt_sha256: str
    multi_process_safe: bool = field(init=False, default=False)
    grants_execution_authority: bool = field(init=False, default=False)
    grants_write_permission: bool = field(init=False, default=False)
    grants_freshness: bool = field(init=False, default=False)


@dataclass(frozen=True, slots=True)
class BetdaqRateSnapshot:
    method: BetdaqApiMethod
    method_active_count: int
    method_capacity: int
    combined_active_count: int
    combined_capacity: int
    blacklist_remaining_seconds: float
    cold_start_remaining_seconds: float
    clock_failed_closed: bool
    tier: BetdaqRateTier
    multi_process_safe: bool = field(init=False, default=False)


Clock = Callable[[], float]
UtcClock = Callable[[], float]


@dataclass(slots=True)
class _ScopeState:
    method_admissions: dict[BetdaqApiMethod, deque[float]]
    combined_admissions: deque[float]
    blacklisted_until: dict[BetdaqApiMethod, float]
    blacklist_observation_ids: dict[BetdaqApiMethod, str]
    cold_start_until: float
    last_now: float
    clock_failed_closed: bool = False


_REGISTRY_LOCK: Final = threading.RLock()
_GOVERNORS: Final[dict[str, "BetdaqRateGovernor"]] = {}
_STATES: Final[dict[str, _ScopeState]] = {}
_CONSTRUCTION_TOKEN: Final = object()
_ISSUED_BLACKLIST: Final[
    dict[int, weakref.ReferenceType[BetdaqBlacklistEvidence]]
] = {}


def _finite_nonnegative(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BetdaqRateGovernorError(
            f"{field_name} must be a finite non-negative number"
        )
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise BetdaqRateGovernorError(
            f"{field_name} must be a finite non-negative number"
        )
    return result


def _safe_scope_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
    ):
        raise BetdaqRateGovernorError(
            "account_scope_id must be a non-empty trimmed identifier <=256 chars"
        )
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise BetdaqRateGovernorError(
            "account_scope_id contains a control character"
        )
    return value


def _policy_fingerprint(policy: BetdaqRatePolicy) -> tuple[object, ...]:
    methods = tuple(
        sorted(
            (method.value, capacity)
            for method, capacity in policy.method_capacity.items()
        )
    )
    return (
        methods,
        policy.combined_capacity,
        policy.safety_reserve,
        policy.reconciliation_reserve,
        float(policy.cold_start_seconds),
        policy.revision,
    )


def _governor_id(scope_id: str, policy: BetdaqRatePolicy) -> str:
    material = repr((scope_id, _policy_fingerprint(policy))).encode("utf-8")
    return "betdaq-rate:" + hashlib.sha256(material).hexdigest()


def _read_clock(clock: Clock) -> float:
    try:
        value = clock()
    except Exception as exc:
        raise BetdaqRateGovernorError("monotonic clock failed") from exc
    return _finite_nonnegative(value, "monotonic clock")


def _read_utc(clock: UtcClock) -> float:
    try:
        value = clock()
    except Exception as exc:
        raise BetdaqRateGovernorError("UTC audit clock failed") from exc
    return _finite_nonnegative(value, "UTC audit clock")


class BetdaqRateGovernor:
    """Process-shared BETDAQ request-admission authority.

    This object never performs network I/O, sleeps, retries, or grants write/
    execution authority. Current source supports only documented DEFAULT
    ceilings. STANDARD is deliberately unavailable until a canonical product
    entitlement witness is composed; callers cannot unlock it with a boolean.
    """

    def __init__(
        self,
        *,
        account_scope_id: str,
        policy: BetdaqRatePolicy,
        clock: Clock,
        utc_clock: UtcClock,
        state: _ScopeState,
        _token: object,
    ) -> None:
        if _token is not _CONSTRUCTION_TOKEN:
            raise BetdaqRateGovernorError(
                "resolve through resolve_betdaq_rate_governor()"
            )
        self.account_scope_id = account_scope_id
        self.policy = policy
        self._clock = clock
        self._utc_clock = utc_clock
        self._state = state
        self.governor_id = _governor_id(account_scope_id, policy)
        self._sequence = 0

    @property
    def tier(self) -> BetdaqRateTier:
        return BetdaqRateTier.DEFAULT

    @property
    def multi_process_safe(self) -> bool:
        return False

    def admit(
        self,
        method: BetdaqApiMethod,
        *,
        priority: BetdaqRatePriority = BetdaqRatePriority.BACKGROUND,
    ) -> BetdaqRateAdmission:
        if not isinstance(method, BetdaqApiMethod):
            raise BetdaqRateGovernorError("method must be BetdaqApiMethod")
        if method not in self.policy.method_capacity:
            raise BetdaqRateDeferred(
                method=method,
                reason="method_policy_unknown",
                retry_after_seconds=None,
            )
        if not isinstance(priority, BetdaqRatePriority):
            raise BetdaqRateGovernorError(
                "priority must be BetdaqRatePriority"
            )
        with _REGISTRY_LOCK:
            now = self._now(method)
            self._prune(now)
            blacklisted_until = self._state.blacklisted_until.get(method, 0.0)
            if blacklisted_until > now:
                raise BetdaqRateDeferred(
                    method=method,
                    reason="provider_api_blacklisted",
                    retry_after_seconds=blacklisted_until - now,
                )
            if self._state.cold_start_until > now:
                raise BetdaqRateDeferred(
                    method=method,
                    reason="restart_cold_start_fence",
                    retry_after_seconds=self._state.cold_start_until - now,
                )
            method_queue = self._state.method_admissions[method]
            capacity = self.policy.method_capacity[method]
            if len(method_queue) >= capacity:
                retry = max(
                    0.0,
                    method_queue[0] + _WINDOW_SECONDS - now,
                )
                raise BetdaqRateDeferred(
                    method=method,
                    reason="method_budget_exhausted",
                    retry_after_seconds=retry,
                )
            combined = len(self._state.combined_admissions)
            background_ceiling = (
                self.policy.combined_capacity
                - self.policy.safety_reserve
                - self.policy.reconciliation_reserve
            )
            reconciliation_ceiling = (
                self.policy.combined_capacity - self.policy.safety_reserve
            )
            if priority is BetdaqRatePriority.BACKGROUND:
                allowed = background_ceiling
                reason = "combined_background_reserve"
            elif priority is BetdaqRatePriority.RECONCILIATION:
                allowed = reconciliation_ceiling
                reason = "combined_safety_reserve"
            else:
                allowed = self.policy.combined_capacity
                reason = "combined_budget_exhausted"
            if combined >= allowed:
                retry = None
                if self._state.combined_admissions:
                    retry = max(
                        0.0,
                        self._state.combined_admissions[0]
                        + _WINDOW_SECONDS
                        - now,
                    )
                raise BetdaqRateDeferred(
                    method=method,
                    reason=reason,
                    retry_after_seconds=retry,
                )
            method_queue.append(now)
            self._state.combined_admissions.append(now)
            self._sequence += 1
            utc_now = _read_utc(self._utc_clock)
            receipt_material = {
                "provider": "BETDAQ",
                "governor_id": self.governor_id,
                "account_scope_id": self.account_scope_id,
                "method": method.value,
                "priority": priority.value,
                "policy_version": _POLICY_VERSION,
                "policy_revision": self.policy.revision,
                "tier": self.tier.value,
                "sequence": self._sequence,
                "admitted_at_monotonic": now,
                "admitted_at_utc": utc_now,
                "method_active_count": len(method_queue),
                "combined_active_count": len(self._state.combined_admissions),
                "multi_process_safe": False,
                "grants_execution_authority": False,
                "grants_write_permission": False,
                "grants_freshness": False,
            }
            digest = hashlib.sha256(
                json.dumps(
                    receipt_material,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            return BetdaqRateAdmission(
                provider="BETDAQ",
                governor_id=self.governor_id,
                account_scope_id=self.account_scope_id,
                method=method,
                priority=priority,
                policy_version=_POLICY_VERSION,
                policy_revision=self.policy.revision,
                tier=self.tier,
                sequence=self._sequence,
                admitted_at_monotonic=now,
                admitted_at_utc=utc_now,
                method_active_count=len(method_queue),
                combined_active_count=len(self._state.combined_admissions),
                receipt_sha256=digest,
            )

    def observe_blacklist(self, evidence: BetdaqBlacklistEvidence) -> None:
        if type(evidence) is not BetdaqBlacklistEvidence:
            raise BetdaqRateGovernorError(
                "blacklist evidence must be exact BetdaqBlacklistEvidence"
            )
        issued = _ISSUED_BLACKLIST.get(id(evidence))
        if issued is None or issued() is not evidence:
            raise BetdaqRateGovernorError(
                "caller-authored blacklist evidence cannot change provider fences"
            )
        with _REGISTRY_LOCK:
            now = self._now(evidence.api_method)
            prior_id = self._state.blacklist_observation_ids.get(
                evidence.api_method
            )
            if prior_id == evidence.observation_id:
                return
            horizon = now + (evidence.remaining_ms / 1000.0)
            self._state.blacklisted_until[evidence.api_method] = max(
                self._state.blacklisted_until.get(evidence.api_method, 0.0),
                horizon,
            )
            self._state.blacklist_observation_ids[
                evidence.api_method
            ] = evidence.observation_id

    def snapshot(self, method: BetdaqApiMethod) -> BetdaqRateSnapshot:
        if method not in self.policy.method_capacity:
            raise BetdaqRateGovernorError(
                "method has no configured documented policy"
            )
        with _REGISTRY_LOCK:
            now = self._now(method)
            self._prune(now)
            return BetdaqRateSnapshot(
                method=method,
                method_active_count=len(self._state.method_admissions[method]),
                method_capacity=self.policy.method_capacity[method],
                combined_active_count=len(self._state.combined_admissions),
                combined_capacity=self.policy.combined_capacity,
                blacklist_remaining_seconds=max(
                    0.0,
                    self._state.blacklisted_until.get(method, 0.0) - now,
                ),
                cold_start_remaining_seconds=max(
                    0.0,
                    self._state.cold_start_until - now,
                ),
                clock_failed_closed=self._state.clock_failed_closed,
                tier=self.tier,
            )

    def _now(self, method: BetdaqApiMethod) -> float:
        now = _read_clock(self._clock)
        if self._state.clock_failed_closed:
            raise BetdaqRateDeferred(
                method=method,
                reason="monotonic_clock_failed_closed",
                retry_after_seconds=None,
            )
        if now < self._state.last_now:
            self._state.clock_failed_closed = True
            raise BetdaqRateDeferred(
                method=method,
                reason="monotonic_clock_rollback",
                retry_after_seconds=None,
            )
        self._state.last_now = now
        return now

    def _prune(self, now: float) -> None:
        cutoff = now - _WINDOW_SECONDS
        for queue in self._state.method_admissions.values():
            while queue and queue[0] <= cutoff:
                queue.popleft()
        while (
            self._state.combined_admissions
            and self._state.combined_admissions[0] <= cutoff
        ):
            self._state.combined_admissions.popleft()


def _issue_verified_blacklist_evidence(
    *,
    api_method: BetdaqApiMethod,
    remaining_ms: int,
    observation_id: str,
) -> BetdaqBlacklistEvidence:
    """Package-internal trust-root hook for the canonical BETDAQ provider reader.

    Structural/caller-created objects remain non-authoritative. A future canonical
    ListBlacklistInformation acquisition path must call this hook only after
    validating provider origin and the full response. Durable/reloaded copies do
    not regain authority because issuance is exact-object and process-ephemeral.
    """

    evidence = BetdaqBlacklistEvidence(
        provider="BETDAQ",
        api_method=api_method,
        remaining_ms=remaining_ms,
        observation_id=observation_id,
    )
    reference = weakref.ref(
        evidence,
        lambda ref, key=id(evidence): (
            _ISSUED_BLACKLIST.pop(key, None)
            if _ISSUED_BLACKLIST.get(key) is ref
            else None
        ),
    )
    _ISSUED_BLACKLIST[id(evidence)] = reference
    object.__setattr__(evidence, "source_authority_proven", True)
    return evidence


def resolve_betdaq_rate_governor(
    account_scope_id: str,
    *,
    policy: BetdaqRatePolicy | None = None,
    clock: Clock = time.monotonic,
    utc_clock: UtcClock = time.time,
) -> BetdaqRateGovernor:
    """Resolve the single process-shared governor for one safe account scope."""

    scope = _safe_scope_id(account_scope_id)
    if policy is None:
        policy = BetdaqRatePolicy.documented_default()
    if type(policy) is not BetdaqRatePolicy:
        raise BetdaqRateGovernorError(
            "policy must be exact BetdaqRatePolicy"
        )
    fingerprint = _policy_fingerprint(policy)
    registry_key = scope
    with _REGISTRY_LOCK:
        existing = _GOVERNORS.get(registry_key)
        if existing is not None:
            if _policy_fingerprint(existing.policy) != fingerprint:
                raise BetdaqRateGovernorError(
                    "same account scope cannot be rebound to a different policy"
                )
            if existing._clock is not clock or existing._utc_clock is not utc_clock:
                raise BetdaqRateGovernorError(
                    "same account scope cannot be rebound to different clocks"
                )
            return existing
        now = _read_clock(clock)
        state = _STATES.get(scope)
        if state is None:
            state = _ScopeState(
                method_admissions={
                    method: deque() for method in policy.method_capacity
                },
                combined_admissions=deque(),
                blacklisted_until={},
                blacklist_observation_ids={},
                cold_start_until=now + float(policy.cold_start_seconds),
                last_now=now,
            )
            _STATES[scope] = state
        governor = BetdaqRateGovernor(
            account_scope_id=scope,
            policy=policy,
            clock=clock,
            utc_clock=utc_clock,
            state=state,
            _token=_CONSTRUCTION_TOKEN,
        )
        _GOVERNORS[registry_key] = governor
        return governor


def reset_betdaq_rate_governor_registry_for_tests() -> None:
    """Test-only deterministic registry reset; production callers must not use it."""

    with _REGISTRY_LOCK:
        _GOVERNORS.clear()
        _STATES.clear()
        _ISSUED_BLACKLIST.clear()

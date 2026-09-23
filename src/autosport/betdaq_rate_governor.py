from __future__ import annotations

"""Shared fail-closed BETDAQ request-admission core.

This module owns only process-local request budgeting plus durable provider-blacklist
fences.  It deliberately does not perform HTTP/SOAP, retry, sleep, place/update an
order, prove Standard-tier entitlement, or grant execution/write authority.

BETDAQ's public Calls and Fees page documents exact Default per-minute ceilings for
a bounded set of methods plus a Combined row.  The public meaning of the separate
"Any" row is not sufficiently specified to use as a positive admission axis, so
unknown methods fail closed instead of inheriting that capacity.
"""

from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import hashlib
import json
import math
from pathlib import Path
import threading
from typing import Final

from .integrity import atomic_write_json, durable_path_lock
from .monotonic_workspace_authority import (
    AuthorityPhase,
    MonotonicAuthorityRecoveryRequiredError,
    MonotonicWorkspaceAuthority,
    MonotonicWorkspaceAuthorityError,
)
from .providers import ProviderUnavailableError


_CALLS_AND_FEES_URL: Final = "https://api.betdaq.com/v2.0/Docs/CallsAndFees.aspx"
_POLICY_SCHEMA: Final = "autosport.betdaq.rate-governor.v1"
_BLACKLIST_SCHEMA: Final = "autosport.betdaq.rate-governor.blacklist.v1"
_BLACKLIST_AUTHORITY_DOMAIN: Final = "autosport.betdaq-rate-governor.blacklist.v1"
_BLACKLIST_FILE: Final = "betdaq-rate-governor-blacklist.json"
_MIN_WINDOW_SECONDS: Final = 60.0
_DEFAULT_COMBINED_PER_MINUTE: Final = 300
_DEFAULT_METHOD_PER_MINUTE: Final[dict[str, int]] = {
    "PlaceOrdersNoReceipt": 100,
    "PlaceOrdersWithReceipt": 20,
    "ChangeOrderNoReceipt": 100,
    "GetEventSubTreeNoSelections": 25,
    "GetEventSubTreeWithSelections": 25,
    "ListBootstrapOrders": 50,
    "GetPrices": 130,
    "ListOrdersChangedSince": 130,
}
_POLICY_SOURCE_SHA256: Final = hashlib.sha256(
    json.dumps(
        {
            "source": _CALLS_AND_FEES_URL,
            "tier": "DEFAULT",
            "methods": _DEFAULT_METHOD_PER_MINUTE,
            "combined": _DEFAULT_COMBINED_PER_MINUTE,
            "any_axis": "UNKNOWN_UNMODELED",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
).hexdigest()
_SHA256_HEX = frozenset("0123456789abcdef")


class BetdaqRateGovernorError(ValueError):
    """Invalid BETDAQ governor configuration, evidence, or durable state."""


class BetdaqRateDeferred(ProviderUnavailableError):
    """Typed fail-closed denial: no provider dispatch may occur."""

    def __init__(
        self,
        *,
        method: str,
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
        super().__init__(f"BETDAQ {method} request deferred: {reason}{suffix}")


class BetdaqRatePriority(StrEnum):
    BACKGROUND_READ = "BACKGROUND_READ"
    RECONCILIATION = "RECONCILIATION"
    SAFETY = "SAFETY"


class BetdaqRateTier(StrEnum):
    DEFAULT = "DEFAULT"


class BetdaqBlacklistStatus(StrEnum):
    UNKNOWN = "UNKNOWN"
    BLACKLISTED = "BLACKLISTED"
    EXPIRED_OBSERVATION = "EXPIRED_OBSERVATION"


Clock = Callable[[], float]
WallClock = Callable[[], datetime]


def _default_wall_clock() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_text(value: object, field_name: str, *, max_length: int = 256) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or len(value) > max_length
        or "\x00" in value
        or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)
    ):
        raise BetdaqRateGovernorError(
            f"{field_name} must be a non-empty canonical string"
        )
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise BetdaqRateGovernorError(f"{field_name} must be valid UTF-8") from exc
    return value


def _sha256(value: object, field_name: str) -> str:
    text = _canonical_text(value, field_name, max_length=64)
    if len(text) != 64 or any(ch not in _SHA256_HEX for ch in text):
        raise BetdaqRateGovernorError(
            f"{field_name} must be canonical lowercase SHA-256 hex"
        )
    return text


def _positive_finite(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BetdaqRateGovernorError(f"{field_name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise BetdaqRateGovernorError(
            f"{field_name} must be finite and positive"
        )
    return result


def _nonnegative_finite(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BetdaqRateGovernorError(f"{field_name} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise BetdaqRateGovernorError(
            f"{field_name} must be finite and non-negative"
        )
    return result


def _utc(value: object, field_name: str) -> datetime:
    if type(value) is not datetime:
        raise BetdaqRateGovernorError(f"{field_name} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise BetdaqRateGovernorError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _utc_text(value: datetime) -> str:
    return _utc(value, "datetime").isoformat().replace("+00:00", "Z")


def _parse_utc_text(value: object, field_name: str) -> datetime:
    text = _canonical_text(value, field_name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BetdaqRateGovernorError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetdaqRateGovernorError(f"{field_name} must include timezone")
    return parsed.astimezone(timezone.utc)


def _digest(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BetdaqRateGovernorError(
            "BETDAQ governor value is outside canonical JSON domain"
        ) from exc
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class BetdaqMethodRatePolicy:
    method: str
    capacity: int
    safety_reserve: int = 0

    def __post_init__(self) -> None:
        method = _canonical_text(self.method, "method")
        documented = _DEFAULT_METHOD_PER_MINUTE.get(method)
        if documented is None:
            raise BetdaqRateGovernorError(
                f"{method} has no exact documented BETDAQ method-rate mapping"
            )
        if isinstance(self.capacity, bool) or type(self.capacity) is not int:
            raise BetdaqRateGovernorError("capacity must be an integer")
        if self.capacity <= 0 or self.capacity > documented:
            raise BetdaqRateGovernorError(
                f"{method} capacity must be within 1..{documented} "
                "for the documented Default tier"
            )
        if (
            isinstance(self.safety_reserve, bool)
            or type(self.safety_reserve) is not int
            or self.safety_reserve < 0
            or self.safety_reserve >= self.capacity
        ):
            raise BetdaqRateGovernorError(
                "safety_reserve must be a non-negative integer "
                "strictly below method capacity"
            )


@dataclass(frozen=True, slots=True)
class BetdaqRatePolicy:
    policy_revision: str
    methods: tuple[BetdaqMethodRatePolicy, ...]
    combined_capacity: int = _DEFAULT_COMBINED_PER_MINUTE
    combined_safety_reserve: int = 0
    window_seconds: float = _MIN_WINDOW_SECONDS
    cold_start_seconds: float | None = None
    tier: BetdaqRateTier = field(init=False, default=BetdaqRateTier.DEFAULT)

    def __post_init__(self) -> None:
        _canonical_text(self.policy_revision, "policy_revision")
        if type(self.methods) is not tuple or not self.methods:
            raise BetdaqRateGovernorError(
                "methods must be a non-empty tuple of BetdaqMethodRatePolicy"
            )
        seen: set[str] = set()
        for method in self.methods:
            if type(method) is not BetdaqMethodRatePolicy:
                raise BetdaqRateGovernorError(
                    "methods must contain exact BetdaqMethodRatePolicy values"
                )
            if method.method in seen:
                raise BetdaqRateGovernorError("method policies must be unique")
            seen.add(method.method)
        if (
            isinstance(self.combined_capacity, bool)
            or type(self.combined_capacity) is not int
            or self.combined_capacity <= 0
            or self.combined_capacity > _DEFAULT_COMBINED_PER_MINUTE
        ):
            raise BetdaqRateGovernorError(
                f"combined_capacity must be within 1..{_DEFAULT_COMBINED_PER_MINUTE}"
            )
        if (
            isinstance(self.combined_safety_reserve, bool)
            or type(self.combined_safety_reserve) is not int
            or self.combined_safety_reserve < 0
            or self.combined_safety_reserve >= self.combined_capacity
        ):
            raise BetdaqRateGovernorError(
                "combined_safety_reserve must be a non-negative integer "
                "strictly below combined capacity"
            )
        window = _positive_finite(self.window_seconds, "window_seconds")
        if window < _MIN_WINDOW_SECONDS:
            raise BetdaqRateGovernorError(
                "window_seconds must be at least 60 seconds"
            )
        cold = self.cold_start_seconds
        if cold is None:
            cold = window
        else:
            cold = _nonnegative_finite(cold, "cold_start_seconds")
            if cold < window:
                raise BetdaqRateGovernorError(
                    "cold_start_seconds must cover the configured rate window"
                )
        object.__setattr__(self, "window_seconds", window)
        object.__setattr__(self, "cold_start_seconds", cold)

    def by_method(self) -> dict[str, BetdaqMethodRatePolicy]:
        return {item.method: item for item in self.methods}

    def fingerprint(self) -> str:
        return _digest(
            {
                "schema": _POLICY_SCHEMA,
                "policy_revision": self.policy_revision,
                "tier": self.tier.value,
                "methods": [
                    {
                        "method": item.method,
                        "capacity": item.capacity,
                        "safety_reserve": item.safety_reserve,
                    }
                    for item in sorted(self.methods, key=lambda value: value.method)
                ],
                "combined_capacity": self.combined_capacity,
                "combined_safety_reserve": self.combined_safety_reserve,
                "window_seconds": self.window_seconds,
                "cold_start_seconds": self.cold_start_seconds,
                "documented_policy_sha256": _POLICY_SOURCE_SHA256,
            }
        )


def default_betdaq_rate_policy(
    *,
    policy_revision: str = "betdaq-default-documented-v1",
    safety_reserve_by_method: Mapping[str, int] | None = None,
    combined_safety_reserve: int = 0,
) -> BetdaqRatePolicy:
    reserves = {} if safety_reserve_by_method is None else dict(safety_reserve_by_method)
    unknown = set(reserves) - set(_DEFAULT_METHOD_PER_MINUTE)
    if unknown:
        raise BetdaqRateGovernorError(
            "safety reserve includes method without exact documented mapping"
        )
    return BetdaqRatePolicy(
        policy_revision=policy_revision,
        methods=tuple(
            BetdaqMethodRatePolicy(
                method=method,
                capacity=capacity,
                safety_reserve=reserves.get(method, 0),
            )
            for method, capacity in sorted(_DEFAULT_METHOD_PER_MINUTE.items())
        ),
        combined_capacity=_DEFAULT_COMBINED_PER_MINUTE,
        combined_safety_reserve=combined_safety_reserve,
    )


@dataclass(frozen=True, slots=True)
class BetdaqBlacklistObservation:
    api_name: str
    observed_at: str
    blocked_until: str
    provider_observation_sha256: str

    def __post_init__(self) -> None:
        _canonical_text(self.api_name, "api_name")
        observed = _parse_utc_text(self.observed_at, "observed_at")
        blocked = _parse_utc_text(self.blocked_until, "blocked_until")
        if blocked < observed:
            raise BetdaqRateGovernorError(
                "blocked_until cannot predate blacklist observation"
            )
        _sha256(
            self.provider_observation_sha256,
            "provider_observation_sha256",
        )

    def payload(self) -> dict[str, str]:
        return {
            "api_name": self.api_name,
            "observed_at": self.observed_at,
            "blocked_until": self.blocked_until,
            "provider_observation_sha256": self.provider_observation_sha256,
        }


@dataclass(frozen=True, slots=True)
class BetdaqRateAdmission:
    governor_id: str
    policy_revision: str
    policy_fingerprint: str
    documented_policy_sha256: str
    tier: BetdaqRateTier
    method: str
    priority: BetdaqRatePriority
    sequence: int
    admitted_monotonic: float
    admitted_at: str
    method_active: int
    combined_active: int
    method_remaining_total: int
    combined_remaining_total: int
    method_remaining_background: int
    combined_remaining_background: int
    blacklist_status: BetdaqBlacklistStatus
    any_axis_status: str = field(init=False, default="UNKNOWN_UNMODELED")
    grants_execution_authority: bool = field(init=False, default=False)
    grants_write_permission: bool = field(init=False, default=False)
    grants_freshness: bool = field(init=False, default=False)
    multi_process_safe: bool = field(init=False, default=False)

    @property
    def receipt_sha256(self) -> str:
        return _digest(
            {
                "governor_id": self.governor_id,
                "policy_revision": self.policy_revision,
                "policy_fingerprint": self.policy_fingerprint,
                "documented_policy_sha256": self.documented_policy_sha256,
                "tier": self.tier.value,
                "method": self.method,
                "priority": self.priority.value,
                "sequence": self.sequence,
                "admitted_monotonic": self.admitted_monotonic,
                "admitted_at": self.admitted_at,
                "method_active": self.method_active,
                "combined_active": self.combined_active,
                "method_remaining_total": self.method_remaining_total,
                "combined_remaining_total": self.combined_remaining_total,
                "method_remaining_background": self.method_remaining_background,
                "combined_remaining_background": self.combined_remaining_background,
                "blacklist_status": self.blacklist_status.value,
                "any_axis_status": self.any_axis_status,
                "grants_execution_authority": self.grants_execution_authority,
                "grants_write_permission": self.grants_write_permission,
                "grants_freshness": self.grants_freshness,
                "multi_process_safe": self.multi_process_safe,
            }
        )


@dataclass(slots=True)
class _Window:
    admitted_at: deque[float]
    blocked_until: float


@dataclass(slots=True)
class _RuntimeState:
    workspace_key: str
    policy_fingerprint: str
    clock: Clock
    wall_clock: WallClock
    methods: dict[str, _Window]
    combined: _Window
    last_monotonic: float
    clock_failed_closed: bool
    sequence: int = 0


_REGISTRY_LOCK: Final = threading.RLock()
_GOVERNORS: Final[dict[str, "BetdaqRateGovernor"]] = {}
_RUNTIME: Final[dict[str, _RuntimeState]] = {}
_CONSTRUCTION_TOKEN: Final = object()


class _BlacklistStore:
    def __init__(
        self,
        workspace: Path,
        *,
        authority_root: Path | None,
    ) -> None:
        self.workspace = workspace
        self.path = workspace / _BLACKLIST_FILE
        self._authority = MonotonicWorkspaceAuthority(
            workspace=workspace,
            domain=_BLACKLIST_AUTHORITY_DOMAIN,
            key=_BLACKLIST_FILE,
            authority_root=authority_root,
        )
        self._recover_or_initialize()

    @staticmethod
    def _empty_body() -> dict[str, object]:
        return {
            "schema": _BLACKLIST_SCHEMA,
            "version": 1,
            "observations": [],
        }

    @staticmethod
    def _envelope(body: dict[str, object]) -> dict[str, object]:
        return {**body, "state_sha256": _digest(body)}

    def _read(self) -> tuple[dict[str, object] | None, str | None]:
        if not self.path.exists():
            return None, None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BetdaqRateGovernorError(
                "BETDAQ blacklist state is unreadable"
            ) from exc
        if type(raw) is not dict or set(raw) != {
            "schema",
            "version",
            "observations",
            "state_sha256",
        }:
            raise BetdaqRateGovernorError(
                "BETDAQ blacklist state schema is invalid"
            )
        if raw.get("schema") != _BLACKLIST_SCHEMA or raw.get("version") != 1:
            raise BetdaqRateGovernorError(
                "BETDAQ blacklist state schema/version mismatch"
            )
        observations = raw.get("observations")
        if type(observations) is not list:
            raise BetdaqRateGovernorError(
                "BETDAQ blacklist observations must be a list"
            )
        seen: set[str] = set()
        canonical: list[dict[str, str]] = []
        for item in observations:
            if type(item) is not dict or set(item) != {
                "api_name",
                "observed_at",
                "blocked_until",
                "provider_observation_sha256",
            }:
                raise BetdaqRateGovernorError(
                    "BETDAQ blacklist observation schema is invalid"
                )
            observation = BetdaqBlacklistObservation(
                api_name=item["api_name"],
                observed_at=item["observed_at"],
                blocked_until=item["blocked_until"],
                provider_observation_sha256=item[
                    "provider_observation_sha256"
                ],
            )
            if observation.api_name in seen:
                raise BetdaqRateGovernorError(
                    "BETDAQ blacklist API identity is duplicated"
                )
            seen.add(observation.api_name)
            canonical.append(observation.payload())
        canonical.sort(key=lambda item: item["api_name"])
        body: dict[str, object] = {
            "schema": _BLACKLIST_SCHEMA,
            "version": 1,
            "observations": canonical,
        }
        state_sha = _sha256(raw.get("state_sha256"), "state_sha256")
        if state_sha != _digest(body):
            raise BetdaqRateGovernorError(
                "BETDAQ blacklist state SHA-256 mismatch"
            )
        return body, state_sha

    @staticmethod
    def _binding(
        observed: str | None,
        intended: str,
    ) -> str:
        return _digest(
            {
                "domain": _BLACKLIST_AUTHORITY_DOMAIN,
                "observed": observed,
                "intended": intended,
            }
        )

    @staticmethod
    def _tx_id(observed: str | None, intended: str) -> str:
        return "betdaq-blacklist-" + _digest(
            {"observed": observed, "intended": intended}
        )

    def _recover_or_initialize(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        with durable_path_lock(self.path):
            body, observed = self._read()
            history = self._authority.read_history()
            if not history:
                if body is not None or observed is not None:
                    raise BetdaqRateGovernorError(
                        "unproven pre-existing BETDAQ blacklist state"
                    )
                body = self._empty_body()
                intended = _digest(body)
                binding = self._binding(None, intended)
                tx_id = self._tx_id(None, intended)
                try:
                    self._authority.prepare(
                        tx_id=tx_id,
                        observed_state_sha256=None,
                        intended_state_sha256=intended,
                        semantic_binding_sha256=binding,
                    )
                    atomic_write_json(self.path, self._envelope(body))
                    self._authority.commit(
                        tx_id=tx_id,
                        observed_state_sha256=intended,
                        semantic_binding_sha256=binding,
                    )
                except MonotonicWorkspaceAuthorityError as exc:
                    raise BetdaqRateGovernorError(
                        "BETDAQ blacklist authority initialization failed"
                    ) from exc
                return

            try:
                latest = history[-1]
                if latest.phase is AuthorityPhase.PREPARE:
                    self._authority.recover(
                        observed_state_sha256=observed,
                        tx_id=latest.tx_id,
                        semantic_binding_sha256=latest.semantic_binding_sha256,
                    )
                else:
                    self._authority.recover(
                        observed_state_sha256=observed
                    )
            except MonotonicAuthorityRecoveryRequiredError as exc:
                raise BetdaqRateGovernorError(
                    "BETDAQ blacklist publication requires recovery"
                ) from exc
            except MonotonicWorkspaceAuthorityError as exc:
                raise BetdaqRateGovernorError(
                    "BETDAQ blacklist state is missing, rolled back, or unproven"
                ) from exc

    def observations(self) -> dict[str, BetdaqBlacklistObservation]:
        with durable_path_lock(self.path):
            body, observed = self._read()
            if body is None or observed is None:
                raise BetdaqRateGovernorError(
                    "BETDAQ blacklist state disappeared"
                )
            try:
                self._authority.recover(
                    observed_state_sha256=observed
                )
            except MonotonicWorkspaceAuthorityError as exc:
                raise BetdaqRateGovernorError(
                    "BETDAQ blacklist state is not current authority"
                ) from exc
            values = body["observations"]
            assert isinstance(values, list)
            return {
                item["api_name"]: BetdaqBlacklistObservation(
                    api_name=item["api_name"],
                    observed_at=item["observed_at"],
                    blocked_until=item["blocked_until"],
                    provider_observation_sha256=item[
                        "provider_observation_sha256"
                    ],
                )
                for item in values
            }

    def extend(
        self,
        observation: BetdaqBlacklistObservation,
    ) -> BetdaqBlacklistObservation:
        if type(observation) is not BetdaqBlacklistObservation:
            raise TypeError("observation must be BetdaqBlacklistObservation")
        with durable_path_lock(self.path):
            body, observed = self._read()
            if body is None or observed is None:
                raise BetdaqRateGovernorError(
                    "BETDAQ blacklist state disappeared"
                )
            try:
                self._authority.recover(
                    observed_state_sha256=observed
                )
            except MonotonicWorkspaceAuthorityError as exc:
                raise BetdaqRateGovernorError(
                    "BETDAQ blacklist state is not current authority"
                ) from exc

            rows = {
                item["api_name"]: dict(item)
                for item in body["observations"]
            }
            existing_raw = rows.get(observation.api_name)
            if existing_raw is not None:
                existing = BetdaqBlacklistObservation(
                    api_name=existing_raw["api_name"],
                    observed_at=existing_raw["observed_at"],
                    blocked_until=existing_raw["blocked_until"],
                    provider_observation_sha256=existing_raw[
                        "provider_observation_sha256"
                    ],
                )
                if _parse_utc_text(
                    existing.blocked_until, "existing blocked_until"
                ) >= _parse_utc_text(
                    observation.blocked_until, "new blocked_until"
                ):
                    return existing
            rows[observation.api_name] = observation.payload()
            new_body: dict[str, object] = {
                "schema": _BLACKLIST_SCHEMA,
                "version": 1,
                "observations": [
                    rows[name] for name in sorted(rows)
                ],
            }
            intended = _digest(new_body)
            binding = self._binding(observed, intended)
            tx_id = self._tx_id(observed, intended)
            try:
                self._authority.prepare(
                    tx_id=tx_id,
                    observed_state_sha256=observed,
                    intended_state_sha256=intended,
                    semantic_binding_sha256=binding,
                )
                atomic_write_json(self.path, self._envelope(new_body))
                self._authority.commit(
                    tx_id=tx_id,
                    observed_state_sha256=intended,
                    semantic_binding_sha256=binding,
                )
            except MonotonicWorkspaceAuthorityError as exc:
                raise BetdaqRateGovernorError(
                    "BETDAQ blacklist authority publication failed"
                ) from exc
            return observation


class BetdaqRateGovernor:
    """Shared process-local request budget plus durable blacklist fence."""

    def __init__(
        self,
        workspace: Path,
        policy: BetdaqRatePolicy,
        *,
        runtime: _RuntimeState,
        blacklist_store: _BlacklistStore,
        _token: object,
    ) -> None:
        if _token is not _CONSTRUCTION_TOKEN:
            raise BetdaqRateGovernorError(
                "BetdaqRateGovernor must be resolved through "
                "resolve_betdaq_rate_governor()"
            )
        self.workspace = workspace
        self.policy = policy
        self._method_policies = policy.by_method()
        self._runtime = runtime
        self._blacklist_store = blacklist_store
        self.policy_fingerprint = policy.fingerprint()
        self.governor_id = "betdaq-rate:" + _digest(
            {
                "workspace_key": runtime.workspace_key,
                "policy_fingerprint": self.policy_fingerprint,
            }
        )

    @property
    def multi_process_safe(self) -> bool:
        return False

    @property
    def tier(self) -> BetdaqRateTier:
        return BetdaqRateTier.DEFAULT

    @property
    def documented_policy_sha256(self) -> str:
        return _POLICY_SOURCE_SHA256

    def blacklist_status(self, api_name: str) -> BetdaqBlacklistStatus:
        name = _canonical_text(api_name, "api_name")
        observations = self._blacklist_store.observations()
        observation = observations.get(name)
        if observation is None:
            return BetdaqBlacklistStatus.UNKNOWN
        now = _utc(self._runtime.wall_clock(), "wall_clock")
        blocked_until = _parse_utc_text(
            observation.blocked_until, "blocked_until"
        )
        if blocked_until > now:
            return BetdaqBlacklistStatus.BLACKLISTED
        return BetdaqBlacklistStatus.EXPIRED_OBSERVATION

    def observe_blacklist(
        self,
        *,
        api_name: str,
        remaining_ms: int,
        provider_observation_sha256: str,
    ) -> BetdaqBlacklistObservation:
        name = _canonical_text(api_name, "api_name")
        if (
            isinstance(remaining_ms, bool)
            or type(remaining_ms) is not int
            or remaining_ms < 0
        ):
            raise BetdaqRateGovernorError(
                "remaining_ms must be a non-negative integer"
            )
        digest = _sha256(
            provider_observation_sha256,
            "provider_observation_sha256",
        )
        observed = _utc(self._runtime.wall_clock(), "wall_clock")
        blocked = observed + timedelta(milliseconds=remaining_ms)
        observation = BetdaqBlacklistObservation(
            api_name=name,
            observed_at=_utc_text(observed),
            blocked_until=_utc_text(blocked),
            provider_observation_sha256=digest,
        )
        return self._blacklist_store.extend(observation)

    def admit(
        self,
        method: str,
        *,
        priority: BetdaqRatePriority = BetdaqRatePriority.BACKGROUND_READ,
    ) -> BetdaqRateAdmission:
        name = _canonical_text(method, "method")
        if type(priority) is not BetdaqRatePriority:
            raise BetdaqRateGovernorError(
                "priority must be BetdaqRatePriority"
            )
        method_policy = self._method_policies.get(name)
        if method_policy is None:
            raise BetdaqRateDeferred(
                method=name,
                reason="unmodeled_provider_rate_axis",
                retry_after_seconds=None,
            )

        with _REGISTRY_LOCK:
            now = self._now(name)
            window = self._runtime.methods[name]
            self._prune(window.admitted_at, now)
            self._prune(self._runtime.combined.admitted_at, now)
            cold_blocked_until = max(
                window.blocked_until,
                self._runtime.combined.blocked_until,
            )
            if cold_blocked_until > now:
                raise BetdaqRateDeferred(
                    method=name,
                    reason="cold_start_rate_window_unproven",
                    retry_after_seconds=cold_blocked_until - now,
                )

            status = self.blacklist_status(name)
            if status is BetdaqBlacklistStatus.BLACKLISTED:
                observation = self._blacklist_store.observations()[name]
                wall_now = _utc(self._runtime.wall_clock(), "wall_clock")
                retry = (
                    _parse_utc_text(
                        observation.blocked_until, "blocked_until"
                    )
                    - wall_now
                ).total_seconds()
                raise BetdaqRateDeferred(
                    method=name,
                    reason="provider_api_blacklisted",
                    retry_after_seconds=max(0.0, retry),
                )

            method_limit = method_policy.capacity
            combined_limit = self.policy.combined_capacity
            if priority is BetdaqRatePriority.BACKGROUND_READ:
                method_limit -= method_policy.safety_reserve
                combined_limit -= self.policy.combined_safety_reserve

            method_active = len(window.admitted_at)
            combined_active = len(self._runtime.combined.admitted_at)
            retry_candidates: list[float] = []
            if method_active >= method_limit:
                retry_candidates.append(
                    window.admitted_at[method_active - method_limit]
                    + self.policy.window_seconds
                )
            if combined_active >= combined_limit:
                retry_candidates.append(
                    self._runtime.combined.admitted_at[
                        combined_active - combined_limit
                    ]
                    + self.policy.window_seconds
                )
            if retry_candidates:
                raise BetdaqRateDeferred(
                    method=name,
                    reason=(
                        "safety_capacity_reserved"
                        if priority is BetdaqRatePriority.BACKGROUND_READ
                        and (
                            method_policy.safety_reserve
                            or self.policy.combined_safety_reserve
                        )
                        else "provider_rate_capacity_exhausted"
                    ),
                    retry_after_seconds=max(
                        0.0, max(retry_candidates) - now
                    ),
                )

            window.admitted_at.append(now)
            self._runtime.combined.admitted_at.append(now)
            self._runtime.sequence += 1
            method_after = len(window.admitted_at)
            combined_after = len(self._runtime.combined.admitted_at)
            method_background = (
                method_policy.capacity - method_policy.safety_reserve
            )
            combined_background = (
                self.policy.combined_capacity
                - self.policy.combined_safety_reserve
            )
            return BetdaqRateAdmission(
                governor_id=self.governor_id,
                policy_revision=self.policy.policy_revision,
                policy_fingerprint=self.policy_fingerprint,
                documented_policy_sha256=_POLICY_SOURCE_SHA256,
                tier=BetdaqRateTier.DEFAULT,
                method=name,
                priority=priority,
                sequence=self._runtime.sequence,
                admitted_monotonic=now,
                admitted_at=_utc_text(
                    _utc(self._runtime.wall_clock(), "wall_clock")
                ),
                method_active=method_after,
                combined_active=combined_after,
                method_remaining_total=max(
                    0, method_policy.capacity - method_after
                ),
                combined_remaining_total=max(
                    0, self.policy.combined_capacity - combined_after
                ),
                method_remaining_background=max(
                    0, method_background - method_after
                ),
                combined_remaining_background=max(
                    0, combined_background - combined_after
                ),
                blacklist_status=status,
            )

    def _now(self, method: str) -> float:
        if self._runtime.clock_failed_closed:
            raise BetdaqRateDeferred(
                method=method,
                reason="monotonic_clock_failed_closed",
                retry_after_seconds=None,
            )
        try:
            value = self._runtime.clock()
        except Exception as exc:
            self._runtime.clock_failed_closed = True
            raise BetdaqRateDeferred(
                method=method,
                reason="monotonic_clock_failed_closed",
                retry_after_seconds=None,
            ) from exc
        now = _nonnegative_finite(value, "monotonic clock")
        if now < self._runtime.last_monotonic:
            self._runtime.clock_failed_closed = True
            raise BetdaqRateDeferred(
                method=method,
                reason="monotonic_clock_rollback",
                retry_after_seconds=None,
            )
        self._runtime.last_monotonic = now
        return now

    def _prune(self, values: deque[float], now: float) -> None:
        cutoff = now - self.policy.window_seconds
        while values and values[0] <= cutoff:
            values.popleft()


def _workspace(path: str | Path) -> Path:
    try:
        value = Path(path).expanduser()
    except RuntimeError as exc:
        raise BetdaqRateGovernorError(
            "workspace path could not be resolved"
        ) from exc
    if not value.is_absolute():
        raise BetdaqRateGovernorError("workspace must be an absolute path")
    try:
        return value.resolve(strict=False)
    except OSError as exc:
        raise BetdaqRateGovernorError(
            "workspace path could not be normalized"
        ) from exc


def resolve_betdaq_rate_governor(
    workspace: str | Path,
    policy: BetdaqRatePolicy,
    *,
    clock: Clock,
    wall_clock: WallClock = _default_wall_clock,
    authority_root: str | Path | None = None,
) -> BetdaqRateGovernor:
    """Resolve one governor per canonical workspace inside this process.

    Standard-tier capacity is intentionally not an input.  Until a separate
    product-owned BETDAQ entitlement authority exists, this core can only enforce
    the public documented Default ceiling or a stricter local policy.
    """

    root = _workspace(workspace)
    if type(policy) is not BetdaqRatePolicy:
        raise BetdaqRateGovernorError(
            "policy must be exact BetdaqRatePolicy"
        )
    if not callable(clock) or not callable(wall_clock):
        raise BetdaqRateGovernorError(
            "clock and wall_clock must be callable"
        )
    resolved_authority = (
        None
        if authority_root is None
        else _workspace(authority_root)
    )
    workspace_key = str(root)
    fingerprint = policy.fingerprint()

    with _REGISTRY_LOCK:
        existing = _GOVERNORS.get(workspace_key)
        runtime = _RUNTIME.get(workspace_key)
        if existing is not None or runtime is not None:
            if existing is None or runtime is None:
                raise BetdaqRateGovernorError(
                    "BETDAQ governor registry is internally inconsistent"
                )
            if runtime.policy_fingerprint != fingerprint:
                raise BetdaqRateGovernorError(
                    "same BETDAQ workspace cannot be rebound to a different rate policy"
                )
            if runtime.clock is not clock or runtime.wall_clock is not wall_clock:
                raise BetdaqRateGovernorError(
                    "same BETDAQ workspace cannot be rebound to a different clock"
                )
            return existing

        now_value = clock()
        now = _nonnegative_finite(now_value, "monotonic clock")
        cold_until = now + float(policy.cold_start_seconds)
        runtime = _RuntimeState(
            workspace_key=workspace_key,
            policy_fingerprint=fingerprint,
            clock=clock,
            wall_clock=wall_clock,
            methods={
                method.method: _Window(
                    admitted_at=deque(),
                    blocked_until=cold_until,
                )
                for method in policy.methods
            },
            combined=_Window(
                admitted_at=deque(),
                blocked_until=cold_until,
            ),
            last_monotonic=now,
            clock_failed_closed=False,
        )
        blacklist_store = _BlacklistStore(
            root,
            authority_root=resolved_authority,
        )
        governor = BetdaqRateGovernor(
            root,
            policy,
            runtime=runtime,
            blacklist_store=blacklist_store,
            _token=_CONSTRUCTION_TOKEN,
        )
        _RUNTIME[workspace_key] = runtime
        _GOVERNORS[workspace_key] = governor
        return governor

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable


_MAX_SEQUENCE = (1 << 63) - 1


class ProviderHealthError(ValueError):
    """Raised when provider-health evidence is malformed or causally inconsistent."""


class ProviderHealthStatus(str, Enum):
    UNKNOWN = "UNKNOWN"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    OPEN = "OPEN"


class ProviderHealthOutcome(str, Enum):
    SUCCESS = "SUCCESS"
    TIMEOUT = "TIMEOUT"
    TRANSPORT_FAILURE = "TRANSPORT_FAILURE"
    AUTH_FAILURE = "AUTH_FAILURE"
    RATE_LIMIT = "RATE_LIMIT"


@dataclass(frozen=True, slots=True)
class ProviderHealthPolicy:
    operational_failures_to_open: int = 3
    consecutive_successes_to_recover: int = 2

    def __post_init__(self) -> None:
        _positive_int("operational_failures_to_open", self.operational_failures_to_open)
        _positive_int("consecutive_successes_to_recover", self.consecutive_successes_to_recover)


@dataclass(frozen=True, slots=True)
class ProviderHealthEvent:
    provider_id: str
    sequence: int
    event_id: str
    occurred_at: datetime
    outcome: ProviderHealthOutcome

    def __post_init__(self) -> None:
        _trimmed("provider_id", self.provider_id)
        _trimmed("event_id", self.event_id)
        _positive_int("sequence", self.sequence)
        if self.sequence > _MAX_SEQUENCE:
            raise ProviderHealthError("sequence exceeds signed 64-bit range")
        if type(self.occurred_at) is not datetime:
            raise ProviderHealthError("occurred_at must be an exact datetime")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ProviderHealthError("occurred_at must be timezone-aware")
        if type(self.outcome) is not ProviderHealthOutcome:
            raise ProviderHealthError("outcome must be ProviderHealthOutcome")

    @property
    def occurred_at_utc(self) -> datetime:
        return self.occurred_at.astimezone(timezone.utc)

    def fingerprint(self) -> str:
        payload = {
            "event_id": self.event_id,
            "occurred_at": self.occurred_at_utc.isoformat(),
            "outcome": self.outcome.value,
            "provider_id": self.provider_id,
            "sequence": self.sequence,
        }
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True, slots=True)
class ProviderHealthState:
    provider_id: str
    status: ProviderHealthStatus = ProviderHealthStatus.UNKNOWN
    health_epoch: int = 0
    last_sequence: int = 0
    last_occurred_at: datetime | None = None
    consecutive_operational_failures: int = 0
    consecutive_recovery_successes: int = 0
    last_event_id: str | None = None
    last_outcome: ProviderHealthOutcome | None = None

    def __post_init__(self) -> None:
        _trimmed("provider_id", self.provider_id)
        if type(self.status) is not ProviderHealthStatus:
            raise ProviderHealthError("status must be ProviderHealthStatus")
        _nonnegative_int("health_epoch", self.health_epoch)
        _nonnegative_int("last_sequence", self.last_sequence)
        _nonnegative_int("consecutive_operational_failures", self.consecutive_operational_failures)
        _nonnegative_int("consecutive_recovery_successes", self.consecutive_recovery_successes)
        if self.last_sequence > _MAX_SEQUENCE:
            raise ProviderHealthError("last_sequence exceeds signed 64-bit range")
        if self.last_occurred_at is not None:
            if type(self.last_occurred_at) is not datetime:
                raise ProviderHealthError("last_occurred_at must be an exact datetime or null")
            if self.last_occurred_at.tzinfo is None or self.last_occurred_at.utcoffset() is None:
                raise ProviderHealthError("last_occurred_at must be timezone-aware")
        if self.last_event_id is not None:
            _trimmed("last_event_id", self.last_event_id)
        if self.last_outcome is not None and type(self.last_outcome) is not ProviderHealthOutcome:
            raise ProviderHealthError("last_outcome must be ProviderHealthOutcome or null")
        if self.status is ProviderHealthStatus.UNKNOWN:
            if any(
                (
                    self.health_epoch,
                    self.last_sequence,
                    self.consecutive_operational_failures,
                    self.consecutive_recovery_successes,
                )
            ) or self.last_occurred_at is not None or self.last_event_id is not None or self.last_outcome is not None:
                raise ProviderHealthError("UNKNOWN state must be pristine")
        elif self.last_sequence == 0 or self.last_occurred_at is None or self.last_event_id is None or self.last_outcome is None:
            raise ProviderHealthError("non-UNKNOWN state requires last-event evidence")


@dataclass(frozen=True, slots=True)
class ProviderWriteBinding:
    provider_id: str
    health_epoch: int
    decision_id: str

    def __post_init__(self) -> None:
        _trimmed("provider_id", self.provider_id)
        _nonnegative_int("health_epoch", self.health_epoch)
        _trimmed("decision_id", self.decision_id)


@dataclass(frozen=True, slots=True)
class FallbackReadEvidence:
    requested_provider_id: str
    fallback_provider_id: str
    observed_at: datetime
    expires_at: datetime
    provenance_sha256: str
    payload_sha256: str

    def __post_init__(self) -> None:
        _trimmed("requested_provider_id", self.requested_provider_id)
        _trimmed("fallback_provider_id", self.fallback_provider_id)
        if self.requested_provider_id == self.fallback_provider_id:
            raise ProviderHealthError("fallback provider must differ from requested provider")
        observed = _aware_exact_datetime("observed_at", self.observed_at)
        expires = _aware_exact_datetime("expires_at", self.expires_at)
        if expires <= observed:
            raise ProviderHealthError("expires_at must be after observed_at")
        _sha256("provenance_sha256", self.provenance_sha256)
        _sha256("payload_sha256", self.payload_sha256)

    @property
    def advisory_only(self) -> bool:
        return True

    @property
    def write_authorized(self) -> bool:
        return False

    def is_fresh(self, *, as_of: datetime) -> bool:
        moment = _aware_exact_datetime("as_of", as_of).astimezone(timezone.utc)
        observed = self.observed_at.astimezone(timezone.utc)
        expires = self.expires_at.astimezone(timezone.utc)
        return observed <= moment < expires


class ProviderHealthAuthority:
    """Provider-neutral health reducer with a fail-closed write gate.

    The authority consumes ordered provider-health events for advisory
    degradation/recovery state and epoch invalidation.  Because
    ProviderHealthEvent is publicly constructible, this module alone cannot
    prove provider/transport origin and therefore cannot issue positive write
    authority.  A future composition must re-resolve a product-owned origin
    witness before positive write admission is possible.

    Fallback read evidence is deliberately separate and can never mint write
    authority.
    """

    def __init__(self, policy: ProviderHealthPolicy | None = None) -> None:
        if policy is None:
            self._policy = ProviderHealthPolicy()
        elif type(policy) is ProviderHealthPolicy:
            self._policy = policy
        else:
            raise ProviderHealthError("policy must be ProviderHealthPolicy or null")
        self._states: dict[str, ProviderHealthState] = {}
        self._events_by_id: dict[tuple[str, str], str] = {}
        self._events_by_sequence: dict[tuple[str, int], str] = {}

    @property
    def policy(self) -> ProviderHealthPolicy:
        return self._policy

    def state(self, provider_id: str) -> ProviderHealthState:
        provider = _trimmed("provider_id", provider_id)
        return self._states.get(provider, ProviderHealthState(provider))

    def apply(self, event: ProviderHealthEvent) -> ProviderHealthState:
        if type(event) is not ProviderHealthEvent:
            raise ProviderHealthError("event must be ProviderHealthEvent")
        fingerprint = event.fingerprint()
        id_key = (event.provider_id, event.event_id)
        seq_key = (event.provider_id, event.sequence)

        by_id = self._events_by_id.get(id_key)
        by_seq = self._events_by_sequence.get(seq_key)
        if by_id is not None or by_seq is not None:
            if by_id == fingerprint and by_seq == fingerprint:
                return self.state(event.provider_id)
            if by_id is not None and by_id != fingerprint:
                raise ProviderHealthError("conflicting reuse of event_id")
            if by_seq is not None and by_seq != fingerprint:
                raise ProviderHealthError("conflicting reuse of sequence")
            raise ProviderHealthError("event identity is only partially duplicated")

        previous = self.state(event.provider_id)
        if event.sequence <= previous.last_sequence:
            raise ProviderHealthError("provider health sequence rollback")
        if previous.last_occurred_at is not None and event.occurred_at_utc < previous.last_occurred_at.astimezone(timezone.utc):
            raise ProviderHealthError("provider health time rollback")

        next_state = self._transition(previous, event)
        self._events_by_id[id_key] = fingerprint
        self._events_by_sequence[seq_key] = fingerprint
        self._states[event.provider_id] = next_state
        return next_state

    def replay(self, events: Iterable[ProviderHealthEvent]) -> dict[str, ProviderHealthState]:
        staged = ProviderHealthAuthority(self._policy)
        staged._states = dict(self._states)
        staged._events_by_id = dict(self._events_by_id)
        staged._events_by_sequence = dict(self._events_by_sequence)
        for event in events:
            staged.apply(event)
        self._states = staged._states
        self._events_by_id = staged._events_by_id
        self._events_by_sequence = staged._events_by_sequence
        return dict(self._states)

    def bind_write_decision(self, *, provider_id: str, decision_id: str) -> ProviderWriteBinding:
        state = self.state(provider_id)
        _trimmed("decision_id", decision_id)
        if state.status is not ProviderHealthStatus.HEALTHY:
            raise ProviderHealthError("provider must be HEALTHY before binding a write decision")
        # ProviderHealthEvent is intentionally a public structural/advisory DTO.
        # Its sequence, timestamp and fingerprint prove consistency only; they do
        # not prove that a provider/transport operation actually occurred.
        #
        # Until this authority is composed with a product-owned origin witness,
        # fail closed instead of turning caller-authored HEALTHY state into write
        # admission.  A later composition must re-resolve canonical origin
        # evidence; it must not accept a caller-provided digest or boolean.
        raise ProviderHealthError(
            "provider-origin authority is required before binding a write decision"
        )

    def write_binding_is_current(
        self,
        binding: ProviderWriteBinding,
        *,
        target_provider_id: str | None = None,
    ) -> bool:
        if type(binding) is not ProviderWriteBinding:
            raise ProviderHealthError("binding must be ProviderWriteBinding")
        target = binding.provider_id if target_provider_id is None else _trimmed("target_provider_id", target_provider_id)
        if target != binding.provider_id:
            return False
        # ProviderWriteBinding is a public value type.  Matching a caller-chosen
        # provider/epoch therefore cannot be positive authority either.  Keep
        # this predicate fail-closed until a product-issued origin-bound binding
        # exists; negative invalidation remains safe and deterministic.
        return False

    def _transition(self, previous: ProviderHealthState, event: ProviderHealthEvent) -> ProviderHealthState:
        status = previous.status
        failures = previous.consecutive_operational_failures
        recovery = previous.consecutive_recovery_successes

        if event.outcome is ProviderHealthOutcome.SUCCESS:
            failures = 0
            if status is ProviderHealthStatus.UNKNOWN:
                next_status = ProviderHealthStatus.HEALTHY
                recovery = 0
            elif status is ProviderHealthStatus.HEALTHY:
                next_status = ProviderHealthStatus.HEALTHY
                recovery = 0
            else:
                recovery += 1
                if recovery >= self._policy.consecutive_successes_to_recover:
                    next_status = ProviderHealthStatus.HEALTHY
                    recovery = 0
                else:
                    next_status = status
        elif event.outcome in {ProviderHealthOutcome.AUTH_FAILURE, ProviderHealthOutcome.RATE_LIMIT}:
            failures = 0
            recovery = 0
            next_status = ProviderHealthStatus.OPEN
        else:
            recovery = 0
            failures += 1
            next_status = (
                ProviderHealthStatus.OPEN
                if status is ProviderHealthStatus.OPEN
                or failures >= self._policy.operational_failures_to_open
                else ProviderHealthStatus.DEGRADED
            )

        epoch = previous.health_epoch
        if next_status is not status:
            epoch += 1

        return ProviderHealthState(
            provider_id=event.provider_id,
            status=next_status,
            health_epoch=epoch,
            last_sequence=event.sequence,
            last_occurred_at=event.occurred_at_utc,
            consecutive_operational_failures=failures,
            consecutive_recovery_successes=recovery,
            last_event_id=event.event_id,
            last_outcome=event.outcome,
        )


def _trimmed(name: str, value: object) -> str:
    if type(value) is not str or not value or value.strip() != value:
        raise ProviderHealthError(f"{name} must be a non-empty trimmed exact string")
    return value


def _positive_int(name: str, value: object) -> int:
    if type(value) is not int or value <= 0:
        raise ProviderHealthError(f"{name} must be a positive exact integer")
    return value


def _nonnegative_int(name: str, value: object) -> int:
    if type(value) is not int or value < 0:
        raise ProviderHealthError(f"{name} must be a non-negative exact integer")
    return value


def _aware_exact_datetime(name: str, value: object) -> datetime:
    if type(value) is not datetime:
        raise ProviderHealthError(f"{name} must be an exact datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProviderHealthError(f"{name} must be timezone-aware")
    return value


def _sha256(name: str, value: object) -> str:
    text = _trimmed(name, value)
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise ProviderHealthError(f"{name} must be a lowercase SHA-256 hex digest")
    return text

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping


_HEX = frozenset("0123456789abcdef")
_PROVIDER = "matchbook"
_REGISTRATION_SCHEMA_VERSION = 1
_UNSUBSCRIBE_SCHEMA_VERSION = 1
_CANCELLATION_SCHEMA_VERSION = 1


class MatchbookHeartbeatSafetyError(RuntimeError):
    """Raised when heartbeat safety evidence is invalid or causally unusable."""


class MatchbookHeartbeatLeaseState(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    SESSION_ROTATED = "SESSION_ROTATED"
    UNSUBSCRIBED = "UNSUBSCRIBED"
    RESTART_REQUIRES_REREGISTRATION = "RESTART_REQUIRES_REREGISTRATION"


class MatchbookCancellationReason(StrEnum):
    USER_REQUEST = "user_request"
    HEARTBEAT_EXPIRY = "heartbeat_expiry"


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _sha256(value: object, name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"{name} must be a canonical SHA-256 string")
    text = value.lower()
    if value != text or len(text) != 64 or any(char not in _HEX for char in text):
        raise ValueError(f"{name} must be a lowercase SHA-256 hex string")
    return text


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _session_generation(value: object) -> int:
    return _positive_int(value, "session_generation")


def _monotonic(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite non-negative monotonic value")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{name} must be a finite non-negative monotonic value")
    return number


def _canonical_utc(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip() or not value.endswith("Z"):
        raise ValueError(f"{name} must be canonical UTC text ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{name} must be canonical ISO-8601 UTC") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include UTC timezone")
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical != value:
        raise ValueError(f"{name} must use canonical UTC representation")
    return value


@dataclass(frozen=True, slots=True)
class MatchbookHeartbeatRegistration:
    """Durable non-secret evidence for one successful Matchbook heartbeat call.

    The raw session token and process-local monotonic clock are deliberately absent.
    session_generation is a product-owned non-secret generation counter supplied by
    the authentication authority; it is not a token, token hash, account id, or
    credential.
    """

    session_generation: int
    requested_timeout_seconds: int
    effective_timeout_seconds: int
    registered_at: str
    predecessor_registration_id: str | None = None

    def __post_init__(self) -> None:
        _session_generation(self.session_generation)
        _positive_int(self.requested_timeout_seconds, "requested_timeout_seconds")
        _positive_int(self.effective_timeout_seconds, "effective_timeout_seconds")
        _canonical_utc(self.registered_at, "registered_at")
        if self.predecessor_registration_id is not None:
            _sha256(self.predecessor_registration_id, "predecessor_registration_id")

    @property
    def registration_id(self) -> str:
        return _digest(self._identity_payload())

    def _identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _REGISTRATION_SCHEMA_VERSION,
            "provider": _PROVIDER,
            "session_generation": self.session_generation,
            "requested_timeout_seconds": self.requested_timeout_seconds,
            "effective_timeout_seconds": self.effective_timeout_seconds,
            "registered_at": self.registered_at,
            "predecessor_registration_id": self.predecessor_registration_id,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = self._identity_payload()
        payload["registration_id"] = self.registration_id
        return payload

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MatchbookHeartbeatRegistration":
        if type(raw) is not dict:
            raise ValueError("heartbeat registration must be a JSON object")
        required = {
            "schema_version",
            "provider",
            "session_generation",
            "requested_timeout_seconds",
            "effective_timeout_seconds",
            "registered_at",
            "predecessor_registration_id",
            "registration_id",
        }
        if set(raw) != required:
            raise ValueError("heartbeat registration fields mismatch")
        if (
            raw["schema_version"] != _REGISTRATION_SCHEMA_VERSION
            or type(raw["schema_version"]) is not int
        ):
            raise ValueError("heartbeat registration schema_version mismatch")
        if raw["provider"] != _PROVIDER or type(raw["provider"]) is not str:
            raise ValueError("heartbeat registration provider mismatch")
        value = cls(
            session_generation=raw["session_generation"],
            requested_timeout_seconds=raw["requested_timeout_seconds"],
            effective_timeout_seconds=raw["effective_timeout_seconds"],
            registered_at=raw["registered_at"],
            predecessor_registration_id=raw["predecessor_registration_id"],
        )
        if _sha256(raw["registration_id"], "registration_id") != value.registration_id:
            raise ValueError("heartbeat registration digest mismatch")
        return value

    def state_after_restart(
        self,
        *,
        current_session_generation: int,
    ) -> MatchbookHeartbeatLeaseState:
        current = _session_generation(current_session_generation)
        if current != self.session_generation:
            return MatchbookHeartbeatLeaseState.SESSION_ROTATED
        return MatchbookHeartbeatLeaseState.RESTART_REQUIRES_REREGISTRATION


@dataclass(frozen=True, slots=True)
class MatchbookHeartbeatUnsubscribeEvidence:
    registration_id: str
    session_generation: int
    unsubscribed_at: str

    def __post_init__(self) -> None:
        _sha256(self.registration_id, "registration_id")
        _session_generation(self.session_generation)
        _canonical_utc(self.unsubscribed_at, "unsubscribed_at")

    @property
    def offers_cancelled_proven(self) -> bool:
        # Matchbook documents DELETE heartbeat as an unsubscribe operation only.
        return False

    @property
    def evidence_id(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _UNSUBSCRIBE_SCHEMA_VERSION,
            "provider": _PROVIDER,
            "registration_id": self.registration_id,
            "session_generation": self.session_generation,
            "unsubscribed_at": self.unsubscribed_at,
            "offers_cancelled_proven": False,
        }


@dataclass(frozen=True, slots=True)
class MatchbookHeartbeatCancellationObservation:
    offer_id: int
    cancellation_reason: MatchbookCancellationReason
    observed_at: str
    provider_observation_sha256: str

    def __post_init__(self) -> None:
        _positive_int(self.offer_id, "offer_id")
        if not isinstance(self.cancellation_reason, MatchbookCancellationReason):
            raise ValueError("cancellation_reason must be a MatchbookCancellationReason")
        _canonical_utc(self.observed_at, "observed_at")
        _sha256(self.provider_observation_sha256, "provider_observation_sha256")

    @property
    def heartbeat_expiry_proven(self) -> bool:
        return self.cancellation_reason is MatchbookCancellationReason.HEARTBEAT_EXPIRY

    @property
    def evidence_id(self) -> str:
        return _digest(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _CANCELLATION_SCHEMA_VERSION,
            "provider": _PROVIDER,
            "offer_id": self.offer_id,
            "cancellation_reason": self.cancellation_reason.value,
            "observed_at": self.observed_at,
            "provider_observation_sha256": self.provider_observation_sha256,
        }


@dataclass(frozen=True, slots=True)
class MatchbookHeartbeatLease:
    """Same-process heartbeat lease.

    Process-local monotonic values are intentionally never serializable. A durable
    registration can be audited after restart, but it cannot be converted back into
    an ACTIVE lease without a new successful provider heartbeat.
    """

    registration: MatchbookHeartbeatRegistration
    registered_monotonic: float
    unsubscribed_monotonic: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.registration, MatchbookHeartbeatRegistration):
            raise ValueError("registration must be MatchbookHeartbeatRegistration")
        start = _monotonic(self.registered_monotonic, "registered_monotonic")
        if self.unsubscribed_monotonic is not None:
            stopped = _monotonic(
                self.unsubscribed_monotonic,
                "unsubscribed_monotonic",
            )
            if stopped < start:
                raise ValueError(
                    "unsubscribed_monotonic must not precede registration"
                )

    @property
    def expires_monotonic(self) -> float:
        return (
            self.registered_monotonic
            + self.registration.effective_timeout_seconds
        )

    def state(
        self,
        *,
        now_monotonic: float,
        current_session_generation: int,
    ) -> MatchbookHeartbeatLeaseState:
        now = _monotonic(now_monotonic, "now_monotonic")
        current = _session_generation(current_session_generation)
        if current != self.registration.session_generation:
            return MatchbookHeartbeatLeaseState.SESSION_ROTATED
        if now < self.registered_monotonic:
            raise MatchbookHeartbeatSafetyError(
                "monotonic clock moved backwards within the heartbeat lease"
            )
        if self.unsubscribed_monotonic is not None:
            if now < self.unsubscribed_monotonic:
                raise MatchbookHeartbeatSafetyError(
                    "current monotonic time precedes durable in-process unsubscribe"
                )
            return MatchbookHeartbeatLeaseState.UNSUBSCRIBED
        if now >= self.expires_monotonic:
            return MatchbookHeartbeatLeaseState.EXPIRED
        return MatchbookHeartbeatLeaseState.ACTIVE

    def remaining_seconds(
        self,
        *,
        now_monotonic: float,
        current_session_generation: int,
    ) -> float:
        state = self.state(
            now_monotonic=now_monotonic,
            current_session_generation=current_session_generation,
        )
        if state is not MatchbookHeartbeatLeaseState.ACTIVE:
            return 0.0
        return self.expires_monotonic - float(now_monotonic)

    def refreshed_after_provider_success(
        self,
        *,
        requested_timeout_seconds: int,
        effective_timeout_seconds: int,
        registered_at: str,
        now_monotonic: float,
        current_session_generation: int,
    ) -> "MatchbookHeartbeatLease":
        now = _monotonic(now_monotonic, "now_monotonic")
        current = _session_generation(current_session_generation)
        if current != self.registration.session_generation:
            raise MatchbookHeartbeatSafetyError(
                "heartbeat success belongs to a different session generation"
            )
        if now < self.registered_monotonic:
            raise MatchbookHeartbeatSafetyError(
                "monotonic clock moved backwards before heartbeat refresh"
            )
        registration = MatchbookHeartbeatRegistration(
            session_generation=current,
            requested_timeout_seconds=requested_timeout_seconds,
            effective_timeout_seconds=effective_timeout_seconds,
            registered_at=registered_at,
            predecessor_registration_id=self.registration.registration_id,
        )
        return MatchbookHeartbeatLease(
            registration=registration,
            registered_monotonic=now,
        )

    def mark_unsubscribed_after_provider_success(
        self,
        *,
        unsubscribed_at: str,
        now_monotonic: float,
        current_session_generation: int,
    ) -> tuple[
        "MatchbookHeartbeatLease",
        MatchbookHeartbeatUnsubscribeEvidence,
    ]:
        now = _monotonic(now_monotonic, "now_monotonic")
        current = _session_generation(current_session_generation)
        if current != self.registration.session_generation:
            raise MatchbookHeartbeatSafetyError(
                "heartbeat unsubscribe belongs to a different session generation"
            )
        if now < self.registered_monotonic:
            raise MatchbookHeartbeatSafetyError(
                "monotonic clock moved backwards before heartbeat unsubscribe"
            )
        if self.unsubscribed_monotonic is not None:
            raise MatchbookHeartbeatSafetyError(
                "heartbeat lease is already unsubscribed"
            )
        at = _canonical_utc(unsubscribed_at, "unsubscribed_at")
        evidence = MatchbookHeartbeatUnsubscribeEvidence(
            registration_id=self.registration.registration_id,
            session_generation=current,
            unsubscribed_at=at,
        )
        return replace(self, unsubscribed_monotonic=now), evidence


def register_after_provider_success(
    *,
    session_generation: int,
    requested_timeout_seconds: int,
    effective_timeout_seconds: int,
    registered_at: str,
    now_monotonic: float,
) -> MatchbookHeartbeatLease:
    """Create an ACTIVE-capable same-process lease after a successful POST heartbeat."""
    registration = MatchbookHeartbeatRegistration(
        session_generation=session_generation,
        requested_timeout_seconds=requested_timeout_seconds,
        effective_timeout_seconds=effective_timeout_seconds,
        registered_at=registered_at,
    )
    return MatchbookHeartbeatLease(
        registration=registration,
        registered_monotonic=_monotonic(now_monotonic, "now_monotonic"),
    )

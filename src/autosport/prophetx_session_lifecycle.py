"""Restart-safe, secret-free ProphetX Trading API session-pool admission.

This module owns only product-side admission and durable non-secret evidence for
creating a ProphetX market-maker Trading API session. It does not implement HTTP
login, token storage, participant Direct-Link authentication, refresh transport,
order execution, or real-money authorization.

The provider contract behind this boundary is conservative: a login can allocate
an additional per-access-key session slot, so restart/crash ambiguity must never
be treated as proof that a slot is free. Market-maker refresh is coordinated as a
separate effect; this module never substitutes another login or the participant
Direct-Link extend-session flow for provider renewal.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from hashlib import sha256
import json
import os
from pathlib import Path
from secrets import token_hex
import stat
from threading import RLock

from .workspace_lock import (
    WorkspaceEconomicLock,
    WorkspaceEconomicLockBusyError,
    WorkspaceEconomicLockError,
    _open_read_only_descriptor,
)


PROVIDER_ID = "prophetx"
STATE_SCHEMA_VERSION = 3
CONSERVATIVE_SESSION_SLOT_HOLD = timedelta(minutes=20)
RENEWAL_LEAD_TIME = timedelta(minutes=2)
_BASE_RETRY_SECONDS = 5
_MAX_RETRY_SECONDS = 300
_MAX_TEXT_CHARS = 4096
_MAX_STATE_FILE_BYTES = 64 * 1024


class ProphetXSessionLifecycleError(RuntimeError):
    """Malformed scope/state or unsafe session-lifecycle transition."""


class ProphetXSessionState(str, Enum):
    NO_SESSION = "no_session"
    LOGIN_IN_FLIGHT = "login_in_flight"
    ACTIVE = "active"
    RENEWAL_DUE = "renewal_due"
    RENEWING = "renewing"
    EXPIRED = "expired"
    AUTH_RETRYABLE_FAILURE = "auth_retryable_failure"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    SESSION_POOL_EXHAUSTED = "session_pool_exhausted"
    CREDENTIAL_REJECTED = "credential_rejected"
    WAIT_FOR_PROVIDER_SESSION_EXPIRY = "wait_for_provider_session_expiry"


class ProphetXLoginAdmissionAction(str, Enum):
    CREATE_LOGIN = "create_login"
    REUSE_ACTIVE = "reuse_active"
    WAIT_FOR_EXISTING_LOGIN = "wait_for_existing_login"
    WAIT_FOR_PROVIDER_SESSION_EXPIRY = "wait_for_provider_session_expiry"
    RETRY_LATER = "retry_later"
    RENEWAL_REQUIRED = "renewal_required"
    START_RENEWAL = "start_renewal"
    WAIT_FOR_EXISTING_RENEWAL = "wait_for_existing_renewal"
    CREDENTIAL_REJECTED = "credential_rejected"
    SHARED_ACCESS_KEY_CONFLICT = "shared_access_key_conflict"


class ProphetXLoginFailureClass(str, Enum):
    SESSION_POOL_EXHAUSTED = "session_pool_exhausted"
    CREDENTIAL_REJECTED = "credential_rejected"
    RETRYABLE_PRE_SESSION_FAILURE = "retryable_pre_session_failure"
    PROVIDER_UNAVAILABLE_PRE_SESSION = "provider_unavailable_pre_session"
    AMBIGUOUS_PROVIDER_RESULT = "ambiguous_provider_result"


class ProphetXRenewalFailureClass(str, Enum):
    RETRYABLE = "retryable"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    CREDENTIAL_REJECTED = "credential_rejected"
    AMBIGUOUS_PROVIDER_RESULT = "ambiguous_provider_result"


def _required_text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProphetXSessionLifecycleError(
            f"{field} must be a non-empty trimmed string"
        )
    if len(value) > _MAX_TEXT_CHARS:
        raise ProphetXSessionLifecycleError(
            f"{field} exceeds the bounded text contract"
        )
    return value


def _sha256_hex(value: object, field: str) -> str:
    text = _required_text(value, field)
    if len(text) != 64 or any(c not in "0123456789abcdef" for c in text):
        raise ProphetXSessionLifecycleError(
            f"{field} must be a lowercase 64-character SHA-256 digest"
        )
    return text


def _aware_utc(value: object, field: str) -> datetime:
    if type(value) is not datetime:
        raise ProphetXSessionLifecycleError(f"{field} must be an exact datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProphetXSessionLifecycleError(f"{field} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _aware_utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _parse_iso(value: object, field: str, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    text = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProphetXSessionLifecycleError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProphetXSessionLifecycleError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise ProphetXSessionLifecycleError(
            f"{field} must be a non-negative exact int"
        )
    return value


def _strict_json_object_pairs(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    """Reject ambiguous persisted objects instead of applying last-key-wins."""

    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProphetXSessionLifecycleError(
                f"ProphetX session state contains duplicate JSON key: {key}"
            )
        result[key] = value
    return result


def _reject_nonfinite_json_constant(value: str) -> object:
    raise ProphetXSessionLifecycleError(
        f"ProphetX session state contains non-standard JSON constant: {value}"
    )


@dataclass(frozen=True, slots=True)
class ProphetXSessionScope:
    """Secret-free identity for one provider access-key session pool."""

    environment: str
    access_key_identity_sha256: str
    credential_revision: str
    integration_role: str

    def __post_init__(self) -> None:
        _required_text(self.environment, "environment")
        _sha256_hex(
            self.access_key_identity_sha256,
            "access_key_identity_sha256",
        )
        _required_text(self.credential_revision, "credential_revision")
        _required_text(self.integration_role, "integration_role")

    @property
    def pool_id(self) -> str:
        # Credential rotation and integration-role changes deliberately do not
        # change the pool identity: old sessions on the same provider access key
        # may still consume provider capacity.
        payload = json.dumps(
            {
                "provider": PROVIDER_ID,
                "environment": self.environment,
                "access_key_identity_sha256": self.access_key_identity_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class ProphetXSessionSnapshot:
    state: ProphetXSessionState
    generation: int
    credential_revision: str
    integration_role: str
    last_transition_at: datetime
    attempt_id: str | None = None
    attempt_started_at: datetime | None = None
    session_lineage_id: str | None = None
    access_expires_at: datetime | None = None
    slot_hold_started_at: datetime | None = None
    slot_hold_until: datetime | None = None
    retry_not_before: datetime | None = None
    transient_failures: int = 0
    last_failure_class: ProphetXLoginFailureClass | None = None
    last_renewal_failure_class: ProphetXRenewalFailureClass | None = None

    def __post_init__(self) -> None:
        if type(self.state) is not ProphetXSessionState:
            raise ProphetXSessionLifecycleError(
                "state must be an exact ProphetXSessionState"
            )
        _nonnegative_int(self.generation, "generation")
        _required_text(self.credential_revision, "credential_revision")
        _required_text(self.integration_role, "integration_role")
        _aware_utc(self.last_transition_at, "last_transition_at")
        _nonnegative_int(self.transient_failures, "transient_failures")
        for name in (
            "attempt_started_at",
            "access_expires_at",
            "slot_hold_started_at",
            "slot_hold_until",
            "retry_not_before",
        ):
            value = getattr(self, name)
            if value is not None:
                _aware_utc(value, name)

        if (self.slot_hold_started_at is None) != (self.slot_hold_until is None):
            raise ProphetXSessionLifecycleError(
                "provider-slot hold requires exact durable floor provenance"
            )
        if self.slot_hold_started_at is not None:
            if self.slot_hold_started_at > self.last_transition_at:
                raise ProphetXSessionLifecycleError(
                    "provider-slot floor provenance cannot be in the future"
                )
            if (
                self.slot_hold_until
                < self.slot_hold_started_at + CONSERVATIVE_SESSION_SLOT_HOLD
            ):
                raise ProphetXSessionLifecycleError(
                    "provider-slot hold is below its durable conservative floor"
                )

        for name in ("attempt_id", "session_lineage_id"):
            value = getattr(self, name)
            if value is not None:
                _sha256_hex(value, name)
        if self.last_failure_class is not None and type(
            self.last_failure_class
        ) is not ProphetXLoginFailureClass:
            raise ProphetXSessionLifecycleError(
                "last_failure_class must be an exact ProphetXLoginFailureClass"
            )
        if self.last_renewal_failure_class is not None and type(
            self.last_renewal_failure_class
        ) is not ProphetXRenewalFailureClass:
            raise ProphetXSessionLifecycleError(
                "last_renewal_failure_class must be an exact ProphetXRenewalFailureClass"
            )

        if self.state is ProphetXSessionState.AUTH_RETRYABLE_FAILURE:
            valid_failure_evidence = (
                self.last_failure_class
                is ProphetXLoginFailureClass.RETRYABLE_PRE_SESSION_FAILURE
                and self.last_renewal_failure_class is None
            ) or (
                self.last_failure_class is None
                and self.last_renewal_failure_class
                is ProphetXRenewalFailureClass.RETRYABLE
            )
            if not valid_failure_evidence:
                raise ProphetXSessionLifecycleError(
                    "failure evidence does not match durable state"
                )
        elif self.state is ProphetXSessionState.PROVIDER_UNAVAILABLE:
            valid_failure_evidence = (
                self.last_failure_class
                is ProphetXLoginFailureClass.PROVIDER_UNAVAILABLE_PRE_SESSION
                and self.last_renewal_failure_class is None
            ) or (
                self.last_failure_class is None
                and self.last_renewal_failure_class
                is ProphetXRenewalFailureClass.PROVIDER_UNAVAILABLE
            )
            if not valid_failure_evidence:
                raise ProphetXSessionLifecycleError(
                    "failure evidence does not match durable state"
                )
        elif self.state is ProphetXSessionState.SESSION_POOL_EXHAUSTED:
            if (
                self.last_failure_class
                is not ProphetXLoginFailureClass.SESSION_POOL_EXHAUSTED
                or self.last_renewal_failure_class is not None
            ):
                raise ProphetXSessionLifecycleError(
                    "failure evidence does not match durable state"
                )

        if self.state is ProphetXSessionState.CREDENTIAL_REJECTED:
            login_rejected = (
                self.last_failure_class
                is ProphetXLoginFailureClass.CREDENTIAL_REJECTED
                and self.last_renewal_failure_class is None
            )
            renewal_rejected = (
                self.last_renewal_failure_class
                is ProphetXRenewalFailureClass.CREDENTIAL_REJECTED
                and self.last_failure_class is None
            )
            if login_rejected == renewal_rejected:
                raise ProphetXSessionLifecycleError(
                    "credential-rejected state requires exact rejection evidence"
                )
            if (
                self.slot_hold_until is not None
                and self.slot_hold_until <= self.last_transition_at
            ):
                raise ProphetXSessionLifecycleError(
                    "credential-rejected provider-slot hold must be future"
                )
        elif (
            self.last_failure_class
            is ProphetXLoginFailureClass.CREDENTIAL_REJECTED
            or self.last_renewal_failure_class
            is ProphetXRenewalFailureClass.CREDENTIAL_REJECTED
        ):
            raise ProphetXSessionLifecycleError(
                "credential-rejection evidence cannot appear outside "
                "credential-rejected state"
            )

        if self.last_failure_class is not None:
            allowed_login_failure_states = {
                ProphetXLoginFailureClass.RETRYABLE_PRE_SESSION_FAILURE: {
                    ProphetXSessionState.AUTH_RETRYABLE_FAILURE,
                },
                ProphetXLoginFailureClass.PROVIDER_UNAVAILABLE_PRE_SESSION: {
                    ProphetXSessionState.PROVIDER_UNAVAILABLE,
                },
                ProphetXLoginFailureClass.SESSION_POOL_EXHAUSTED: {
                    ProphetXSessionState.SESSION_POOL_EXHAUSTED,
                },
                ProphetXLoginFailureClass.CREDENTIAL_REJECTED: {
                    ProphetXSessionState.CREDENTIAL_REJECTED,
                },
                ProphetXLoginFailureClass.AMBIGUOUS_PROVIDER_RESULT: {
                    ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY,
                },
            }
            if self.state not in allowed_login_failure_states[self.last_failure_class]:
                raise ProphetXSessionLifecycleError(
                    "login failure evidence is incompatible with durable state"
                )

        if self.last_renewal_failure_class is not None:
            allowed_renewal_failure_states = {
                ProphetXRenewalFailureClass.RETRYABLE: {
                    ProphetXSessionState.RENEWAL_DUE,
                    ProphetXSessionState.RENEWING,
                    ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY,
                    ProphetXSessionState.AUTH_RETRYABLE_FAILURE,
                },
                ProphetXRenewalFailureClass.PROVIDER_UNAVAILABLE: {
                    ProphetXSessionState.RENEWAL_DUE,
                    ProphetXSessionState.RENEWING,
                    ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY,
                    ProphetXSessionState.PROVIDER_UNAVAILABLE,
                },
                ProphetXRenewalFailureClass.CREDENTIAL_REJECTED: {
                    ProphetXSessionState.CREDENTIAL_REJECTED,
                },
                ProphetXRenewalFailureClass.AMBIGUOUS_PROVIDER_RESULT: {
                    ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY,
                },
            }
            if (
                self.state
                not in allowed_renewal_failure_states[
                    self.last_renewal_failure_class
                ]
            ):
                raise ProphetXSessionLifecycleError(
                    "renewal failure evidence is incompatible with durable state"
                )

        if self.state in {
            ProphetXSessionState.LOGIN_IN_FLIGHT,
            ProphetXSessionState.RENEWING,
        }:
            if (
                self.attempt_id is None
                or self.attempt_started_at is None
                or self.slot_hold_until is None
            ):
                raise ProphetXSessionLifecycleError(
                    "in-flight auth state requires attempt and slot-hold evidence"
                )
            if self.attempt_started_at != self.last_transition_at:
                raise ProphetXSessionLifecycleError(
                    "in-flight attempt start must equal transition time"
                )
        if self.state in {
            ProphetXSessionState.ACTIVE,
            ProphetXSessionState.RENEWAL_DUE,
            ProphetXSessionState.RENEWING,
        }:
            if (
                self.session_lineage_id is None
                or self.access_expires_at is None
                or self.slot_hold_until is None
            ):
                raise ProphetXSessionLifecycleError(
                    "active session state requires lineage and expiry evidence"
                )
        if self.state in {
            ProphetXSessionState.AUTH_RETRYABLE_FAILURE,
            ProphetXSessionState.PROVIDER_UNAVAILABLE,
        }:
            if self.retry_not_before is None:
                raise ProphetXSessionLifecycleError(
                    "retryable state requires retry_not_before"
                )
            if self.retry_not_before <= self.last_transition_at:
                raise ProphetXSessionLifecycleError(
                    "retryable state requires a future retry horizon"
                )

        if (
            self.state is ProphetXSessionState.RENEWAL_DUE
            and self.retry_not_before is not None
            and self.retry_not_before <= self.last_transition_at
        ):
            raise ProphetXSessionLifecycleError(
                "renewal retry horizon must follow the latest transition"
            )

        if self.state is ProphetXSessionState.LOGIN_IN_FLIGHT:
            if (
                self.session_lineage_id is not None
                or self.access_expires_at is not None
                or self.retry_not_before is not None
            ):
                raise ProphetXSessionLifecycleError(
                    "login_in_flight cannot carry session or retry evidence"
                )
            if self.slot_hold_until <= self.attempt_started_at:
                raise ProphetXSessionLifecycleError(
                    "login_in_flight slot hold must follow attempt start"
                )
            if (
                self.slot_hold_until
                < self.attempt_started_at + CONSERVATIVE_SESSION_SLOT_HOLD
            ):
                raise ProphetXSessionLifecycleError(
                    "login_in_flight slot hold is below the conservative floor"
                )
        elif self.state is ProphetXSessionState.RENEWING:
            if self.retry_not_before is not None:
                raise ProphetXSessionLifecycleError(
                    "renewing cannot carry retry_not_before"
                )
        elif self.attempt_id is not None or self.attempt_started_at is not None:
            raise ProphetXSessionLifecycleError(
                "attempt evidence is only valid for an in-flight auth operation"
            )

        if self.state in {
            ProphetXSessionState.ACTIVE,
            ProphetXSessionState.RENEWAL_DUE,
            ProphetXSessionState.RENEWING,
        }:
            if self.access_expires_at <= self.last_transition_at:
                raise ProphetXSessionLifecycleError(
                    "active access expiry must follow the latest transition"
                )
            if (
                self.state in {
                    ProphetXSessionState.RENEWAL_DUE,
                    ProphetXSessionState.RENEWING,
                }
                and self.last_transition_at
                < self.access_expires_at - RENEWAL_LEAD_TIME
            ):
                raise ProphetXSessionLifecycleError(
                    "renewal state cannot precede its lead window"
                )
            if self.slot_hold_until < self.access_expires_at:
                raise ProphetXSessionLifecycleError(
                    "active slot hold cannot precede access expiry"
                )
            if (
                self.state is ProphetXSessionState.ACTIVE
                and self.slot_hold_until
                < self.last_transition_at + CONSERVATIVE_SESSION_SLOT_HOLD
            ):
                raise ProphetXSessionLifecycleError(
                    "active slot hold is below the conservative floor"
                )
            if (
                self.state is not ProphetXSessionState.RENEWAL_DUE
                and self.retry_not_before is not None
            ):
                raise ProphetXSessionLifecycleError(
                    "active/renewing state cannot carry retry evidence"
                )

        if self.state in {
            ProphetXSessionState.SESSION_POOL_EXHAUSTED,
            ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY,
        }:
            if self.slot_hold_until is None:
                raise ProphetXSessionLifecycleError(
                    "provider-slot wait state requires slot_hold_until"
                )
            if self.slot_hold_until <= self.last_transition_at:
                raise ProphetXSessionLifecycleError(
                    "provider-slot wait state requires a future hold horizon"
                )
            if self.session_lineage_id is not None or self.retry_not_before is not None:
                raise ProphetXSessionLifecycleError(
                    "provider-slot wait cannot carry active-session or retry authority"
                )
            if self.state is ProphetXSessionState.SESSION_POOL_EXHAUSTED:
                if self.access_expires_at is not None:
                    raise ProphetXSessionLifecycleError(
                        "session-pool exhaustion cannot carry access-expiry evidence"
                    )
                if (
                    self.slot_hold_until
                    < self.last_transition_at + CONSERVATIVE_SESSION_SLOT_HOLD
                ):
                    raise ProphetXSessionLifecycleError(
                        "session-pool hold is below the conservative floor"
                    )
            elif self.access_expires_at is not None:
                if self.access_expires_at <= self.last_transition_at:
                    raise ProphetXSessionLifecycleError(
                        "wait-state access expiry must follow the latest transition"
                    )
                if self.access_expires_at > self.slot_hold_until:
                    raise ProphetXSessionLifecycleError(
                        "provider-slot hold cannot precede observed access expiry"
                    )
                if (
                    self.slot_hold_until
                    < self.last_transition_at + CONSERVATIVE_SESSION_SLOT_HOLD
                ):
                    raise ProphetXSessionLifecycleError(
                        "wait hold is below the conservative refresh floor"
                    )

        if (
            self.state is ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY
            and (
                self.last_failure_class
                is ProphetXLoginFailureClass.AMBIGUOUS_PROVIDER_RESULT
                or self.last_renewal_failure_class
                is ProphetXRenewalFailureClass.AMBIGUOUS_PROVIDER_RESULT
            )
            and self.slot_hold_until
            < self.last_transition_at + CONSERVATIVE_SESSION_SLOT_HOLD
        ):
            raise ProphetXSessionLifecycleError(
                "ambiguous provider result hold is below the conservative floor"
            )

        if self.state in {
            ProphetXSessionState.AUTH_RETRYABLE_FAILURE,
            ProphetXSessionState.PROVIDER_UNAVAILABLE,
        } and (
            self.session_lineage_id is not None
            or self.access_expires_at is not None
            or self.slot_hold_until is not None
        ):
            raise ProphetXSessionLifecycleError(
                "retryable login state cannot carry provider-slot authority"
            )

        if self.state is ProphetXSessionState.CREDENTIAL_REJECTED and (
            self.session_lineage_id is not None
            or self.access_expires_at is not None
            or self.retry_not_before is not None
        ):
            raise ProphetXSessionLifecycleError(
                "credential-rejected state cannot carry active-session or retry evidence"
            )

        if self.state in {
            ProphetXSessionState.NO_SESSION,
            ProphetXSessionState.EXPIRED,
        } and (
            self.session_lineage_id is not None
            or self.access_expires_at is not None
            or self.slot_hold_until is not None
            or self.retry_not_before is not None
        ):
            raise ProphetXSessionLifecycleError(
                "empty or expired state cannot carry session, slot, or retry evidence"
            )

    def to_json_dict(
        self,
        *,
        environment: str,
        access_key_identity_sha256: str,
    ) -> dict[str, object]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "provider": PROVIDER_ID,
            "environment": environment,
            "access_key_identity_sha256": access_key_identity_sha256,
            "state": self.state.value,
            "generation": self.generation,
            "credential_revision": self.credential_revision,
            "integration_role": self.integration_role,
            "last_transition_at": _iso(self.last_transition_at),
            "attempt_id": self.attempt_id,
            "attempt_started_at": _iso(self.attempt_started_at),
            "session_lineage_id": self.session_lineage_id,
            "access_expires_at": _iso(self.access_expires_at),
            "slot_hold_started_at": _iso(self.slot_hold_started_at),
            "slot_hold_until": _iso(self.slot_hold_until),
            "retry_not_before": _iso(self.retry_not_before),
            "transient_failures": self.transient_failures,
            "last_failure_class": (
                None
                if self.last_failure_class is None
                else self.last_failure_class.value
            ),
            "last_renewal_failure_class": (
                None
                if self.last_renewal_failure_class is None
                else self.last_renewal_failure_class.value
            ),
        }


@dataclass(frozen=True, slots=True)
class ProphetXLoginAdmission:
    action: ProphetXLoginAdmissionAction
    snapshot: ProphetXSessionSnapshot | None
    attempt_id: str | None = None
    retry_at: datetime | None = None

    def __post_init__(self) -> None:
        if type(self.action) is not ProphetXLoginAdmissionAction:
            raise ProphetXSessionLifecycleError(
                "action must be an exact ProphetXLoginAdmissionAction"
            )
        if self.snapshot is not None and type(
            self.snapshot
        ) is not ProphetXSessionSnapshot:
            raise ProphetXSessionLifecycleError(
                "snapshot must be an exact ProphetXSessionSnapshot"
            )
        if self.attempt_id is not None:
            _sha256_hex(self.attempt_id, "attempt_id")
        if self.retry_at is not None:
            _aware_utc(self.retry_at, "retry_at")

        if self.action is ProphetXLoginAdmissionAction.CREATE_LOGIN:
            if (
                self.snapshot is None
                or self.snapshot.state is not ProphetXSessionState.LOGIN_IN_FLIGHT
                or self.attempt_id is None
                or self.attempt_id != self.snapshot.attempt_id
                or self.retry_at != self.snapshot.slot_hold_until
            ):
                raise ProphetXSessionLifecycleError(
                    "create-login admission requires its exact durable reservation"
                )
        elif self.action is ProphetXLoginAdmissionAction.START_RENEWAL:
            if (
                self.snapshot is None
                or self.snapshot.state is not ProphetXSessionState.RENEWING
                or self.attempt_id is None
                or self.attempt_id != self.snapshot.attempt_id
            ):
                raise ProphetXSessionLifecycleError(
                    "start-renewal admission requires its exact durable reservation"
                )
        elif self.attempt_id is not None:
            raise ProphetXSessionLifecycleError(
                "only effect-start admissions may expose an attempt id"
            )

        if (
            self.action is ProphetXLoginAdmissionAction.REUSE_ACTIVE
            and (
                self.snapshot is None
                or self.snapshot.state is not ProphetXSessionState.ACTIVE
            )
        ):
            raise ProphetXSessionLifecycleError(
                "active reuse requires an active durable snapshot"
            )
        if (
            self.action is ProphetXLoginAdmissionAction.RENEWAL_REQUIRED
            and (
                self.snapshot is None
                or self.snapshot.state is not ProphetXSessionState.RENEWAL_DUE
            )
        ):
            raise ProphetXSessionLifecycleError(
                "renewal-required admission requires renewal-due state"
            )
        if (
            self.action is ProphetXLoginAdmissionAction.CREDENTIAL_REJECTED
            and (
                self.snapshot is None
                or self.snapshot.state is not ProphetXSessionState.CREDENTIAL_REJECTED
            )
        ):
            raise ProphetXSessionLifecycleError(
                "credential-rejected admission requires rejected durable state"
            )

    @property
    def login_authorized(self) -> bool:
        """Admission DTOs never carry standalone positive side-effect authority."""
        return False

    @property
    def real_money_execution(self) -> bool:
        return False


class ProphetXSessionLifecycle:
    """Durable per-access-key login admission coordinator."""

    _STATE_NAME = "prophetx-session-state.json"

    def __init__(
        self,
        workspace: str | Path,
        *,
        scope: ProphetXSessionScope,
    ) -> None:
        if type(scope) is not ProphetXSessionScope:
            raise ProphetXSessionLifecycleError(
                "scope must be an exact ProphetXSessionScope"
            )
        self.workspace = Path(workspace)
        self.scope = scope
        self._scope_dir = (
            self.workspace
            / ".provider-session-lifecycle"
            / self.scope.pool_id
        )
        self._state_path = self._scope_dir / self._STATE_NAME
        self._thread_lock = RLock()
        self._owned_attempts: set[str] = set()
        self._issued_effect_admissions: dict[str, ProphetXLoginAdmission] = {}

    @property
    def state_path(self) -> Path:
        return self._state_path

    def consume_effect_authority(
        self,
        admission: ProphetXLoginAdmission,
    ) -> bool:
        """Consume one coordinator-issued login/renewal side-effect authority."""

        if type(admission) is not ProphetXLoginAdmission:
            return False
        if admission.action not in {
            ProphetXLoginAdmissionAction.CREATE_LOGIN,
            ProphetXLoginAdmissionAction.START_RENEWAL,
        }:
            return False
        attempt = admission.attempt_id
        if attempt is None:
            return False

        with self._thread_lock:
            if (
                attempt not in self._owned_attempts
                or self._issued_effect_admissions.get(attempt) is not admission
            ):
                return False
            try:
                with WorkspaceEconomicLock(self._scope_dir):
                    current = self._load_state()
                    expected_state = (
                        ProphetXSessionState.LOGIN_IN_FLIGHT
                        if admission.action
                        is ProphetXLoginAdmissionAction.CREATE_LOGIN
                        else ProphetXSessionState.RENEWING
                    )
                    if (
                        current is None
                        or current.state is not expected_state
                        or current.attempt_id != attempt
                        or current.credential_revision
                        != self.scope.credential_revision
                        or current.integration_role != self.scope.integration_role
                        or admission.snapshot != current
                    ):
                        return False
                    expected_retry = (
                        current.slot_hold_until
                        if admission.action
                        is ProphetXLoginAdmissionAction.CREATE_LOGIN
                        else current.access_expires_at
                    )
                    if admission.retry_at != expected_retry:
                        return False
                    self._issued_effect_admissions.pop(attempt, None)
                    return True
            except WorkspaceEconomicLockBusyError:
                return False
            except WorkspaceEconomicLockError as exc:
                raise ProphetXSessionLifecycleError(
                    "cannot acquire ProphetX session-pool coordination lock"
                ) from exc

    def begin_login(
        self,
        *,
        now: datetime,
        access_token_available: bool,
        access_token_lineage_id: str | None = None,
    ) -> ProphetXLoginAdmission:
        timestamp = _aware_utc(now, "now")
        if type(access_token_available) is not bool:
            raise ProphetXSessionLifecycleError(
                "access_token_available must be an exact bool"
            )
        if access_token_available:
            if access_token_lineage_id is None:
                raise ProphetXSessionLifecycleError(
                    "available access token requires session lineage evidence"
                )
            token_lineage = _sha256_hex(
                access_token_lineage_id,
                "access_token_lineage_id",
            )
        else:
            if access_token_lineage_id is not None:
                raise ProphetXSessionLifecycleError(
                    "unavailable access token cannot carry session lineage evidence"
                )
            token_lineage = None

        with self._thread_lock:
            try:
                with WorkspaceEconomicLock(self._scope_dir):
                    current = self._load_state()
                    return self._begin_login_locked(
                        current,
                        timestamp,
                        access_token_available=access_token_available,
                        access_token_lineage_id=token_lineage,
                    )
            except WorkspaceEconomicLockBusyError:
                # Another process is changing this exact provider pool. Waiting is
                # safer than allocating another provider session.
                return ProphetXLoginAdmission(
                    action=ProphetXLoginAdmissionAction.WAIT_FOR_EXISTING_LOGIN,
                    snapshot=None,
                )
            except WorkspaceEconomicLockError as exc:
                raise ProphetXSessionLifecycleError(
                    "cannot acquire ProphetX session-pool coordination lock"
                ) from exc

    def begin_renewal(
        self,
        *,
        now: datetime,
        refresh_token_lineage_id: str | None = None,
    ) -> ProphetXLoginAdmission:
        """Reserve one refresh attempt without allocating a new provider session."""

        timestamp = _aware_utc(now, "now")
        if refresh_token_lineage_id is None:
            raise ProphetXSessionLifecycleError(
                "refresh token requires session lineage evidence"
            )
        refresh_lineage = _sha256_hex(
            refresh_token_lineage_id,
            "refresh_token_lineage_id",
        )
        with self._thread_lock:
            try:
                with WorkspaceEconomicLock(self._scope_dir):
                    current = self._load_state()
                    if current is None:
                        raise ProphetXSessionLifecycleError(
                            "cannot renew without durable session evidence"
                        )
                    self._require_monotonic_transition(current, timestamp)
                    if current.integration_role != self.scope.integration_role:
                        raise ProphetXSessionLifecycleError(
                            "access key is bound to a different integration role"
                        )
                    if current.credential_revision != self.scope.credential_revision:
                        raise ProphetXSessionLifecycleError(
                            "credential revision changed before renewal"
                        )
                    if current.session_lineage_id != refresh_lineage:
                        raise ProphetXSessionLifecycleError(
                            "refresh token lineage does not match active session"
                        )
                    if current.state is ProphetXSessionState.RENEWING:
                        uncertainty_deadline = self._renewal_uncertainty_deadline(current)
                        if (
                            current.attempt_id in self._owned_attempts
                            and timestamp < uncertainty_deadline
                        ):
                            return ProphetXLoginAdmission(
                                action=ProphetXLoginAdmissionAction.WAIT_FOR_EXISTING_RENEWAL,
                                snapshot=current,
                                retry_at=uncertainty_deadline,
                            )
                        if timestamp < uncertainty_deadline:
                            return ProphetXLoginAdmission(
                                action=(
                                    ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY
                                ),
                                snapshot=current,
                                retry_at=uncertainty_deadline,
                            )
                        if current.attempt_id is not None:
                            self._discard_owned_attempt(current.attempt_id)
                        raise ProphetXSessionLifecycleError(
                            "stale renewal ambiguity must re-enter login admission"
                        )
                    if current.state not in {
                        ProphetXSessionState.ACTIVE,
                        ProphetXSessionState.RENEWAL_DUE,
                    }:
                        raise ProphetXSessionLifecycleError(
                            "renewal requires an active or renewal-due session"
                        )
                    if current.access_expires_at is None:
                        raise ProphetXSessionLifecycleError(
                            "renewal requires exact provider access expiry"
                        )
                    if timestamp >= current.access_expires_at:
                        raise ProphetXSessionLifecycleError(
                            "cannot renew an already expired access session"
                        )
                    if (
                        current.state is ProphetXSessionState.ACTIVE
                        and timestamp
                        < current.access_expires_at - RENEWAL_LEAD_TIME
                    ):
                        raise ProphetXSessionLifecycleError(
                            "renewal is not due yet"
                        )
                    if (
                        current.retry_not_before is not None
                        and timestamp < current.retry_not_before
                    ):
                        return ProphetXLoginAdmission(
                            action=ProphetXLoginAdmissionAction.RETRY_LATER,
                            snapshot=current,
                            retry_at=current.retry_not_before,
                        )
                    attempt = token_hex(32)
                    updated = ProphetXSessionSnapshot(
                        state=ProphetXSessionState.RENEWING,
                        generation=current.generation + 1,
                        credential_revision=current.credential_revision,
                        integration_role=current.integration_role,
                        last_transition_at=timestamp,
                        attempt_id=attempt,
                        attempt_started_at=timestamp,
                        session_lineage_id=current.session_lineage_id,
                        access_expires_at=current.access_expires_at,
                        slot_hold_started_at=current.slot_hold_started_at,
                        slot_hold_until=current.slot_hold_until,
                        transient_failures=current.transient_failures,
                        last_failure_class=current.last_failure_class,
                        last_renewal_failure_class=current.last_renewal_failure_class,
                    )
                    self._write_state(updated)
                    self._owned_attempts.add(attempt)
                    admission = ProphetXLoginAdmission(
                        action=ProphetXLoginAdmissionAction.START_RENEWAL,
                        snapshot=updated,
                        attempt_id=attempt,
                        retry_at=current.access_expires_at,
                    )
                    self._issued_effect_admissions[attempt] = admission
                    return admission
            except WorkspaceEconomicLockBusyError:
                # Another process is mutating this exact provider pool. A concurrent
                # renewal caller must wait rather than treating lock contention as an
                # auth failure or falling back to a replacement login/session.
                return ProphetXLoginAdmission(
                    action=ProphetXLoginAdmissionAction.WAIT_FOR_EXISTING_RENEWAL,
                    snapshot=None,
                )
            except WorkspaceEconomicLockError as exc:
                raise ProphetXSessionLifecycleError(
                    "cannot acquire ProphetX session-pool coordination lock"
                ) from exc

    def complete_renewal_success(
        self,
        *,
        attempt_id: str,
        now: datetime,
        access_expires_at: datetime,
    ) -> ProphetXSessionSnapshot:
        """Record refresh success without inventing provider slot semantics.

        The provider response can establish that refresh authentication succeeded and
        can carry an exact new access-token expiry.  It does not, by itself, prove
        whether the refresh preserved the existing per-access-key provider session slot
        or allocated another one.  Until a separately qualified product-owned provider
        contract can prove that semantic, do not publish ACTIVE/reusable session
        authority.  Preserve a conservative durable slot hold instead.
        """

        attempt = _sha256_hex(attempt_id, "attempt_id")
        timestamp = _aware_utc(now, "now")
        expires = _aware_utc(access_expires_at, "access_expires_at")
        if expires <= timestamp:
            raise ProphetXSessionLifecycleError(
                "provider renewal access_expires_at must follow completion"
            )
        with self._thread_lock:
            if attempt not in self._owned_attempts:
                raise ProphetXSessionLifecycleError(
                    "renewal attempt was not issued by this coordinator"
                )
            try:
                with WorkspaceEconomicLock(self._scope_dir):
                    current = self._require_owned_renewal(attempt)
                    self._require_monotonic_transition(current, timestamp)
                    conservative_hold = max(
                        expires,
                        timestamp + CONSERVATIVE_SESSION_SLOT_HOLD,
                    )
                    updated = ProphetXSessionSnapshot(
                        state=ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY,
                        generation=current.generation + 1,
                        credential_revision=current.credential_revision,
                        integration_role=current.integration_role,
                        last_transition_at=timestamp,
                        access_expires_at=expires,
                        slot_hold_started_at=timestamp,
                        slot_hold_until=conservative_hold,
                        transient_failures=0,
                        last_failure_class=current.last_failure_class,
                    )
                    self._write_state(updated)
            except WorkspaceEconomicLockError as exc:
                raise ProphetXSessionLifecycleError(
                    "cannot acquire ProphetX session-pool coordination lock"
                ) from exc
            self._discard_owned_attempt(attempt)
            return updated

    def complete_renewal_failure(
        self,
        *,
        attempt_id: str,
        now: datetime,
        failure: ProphetXRenewalFailureClass,
    ) -> ProphetXSessionSnapshot:
        """Fail closed without converting refresh failure into a replacement login."""

        attempt = _sha256_hex(attempt_id, "attempt_id")
        timestamp = _aware_utc(now, "now")
        if type(failure) is not ProphetXRenewalFailureClass:
            raise ProphetXSessionLifecycleError(
                "failure must be an exact ProphetXRenewalFailureClass"
            )
        with self._thread_lock:
            if attempt not in self._owned_attempts:
                raise ProphetXSessionLifecycleError(
                    "renewal attempt was not issued by this coordinator"
                )
            try:
                with WorkspaceEconomicLock(self._scope_dir):
                    current = self._require_owned_renewal(attempt)
                    self._require_monotonic_transition(current, timestamp)
                    failures = current.transient_failures + 1
                    if failure is ProphetXRenewalFailureClass.AMBIGUOUS_PROVIDER_RESULT:
                        state = ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY
                        hold = timestamp + CONSERVATIVE_SESSION_SLOT_HOLD
                        retry = None
                        lineage = None
                        expiry = None
                    elif failure is ProphetXRenewalFailureClass.CREDENTIAL_REJECTED:
                        state = ProphetXSessionState.CREDENTIAL_REJECTED
                        hold = (
                            current.slot_hold_until
                            if current.slot_hold_until is not None
                            and current.slot_hold_until > timestamp
                            else None
                        )
                        retry = None
                        lineage = None
                        expiry = None
                    elif timestamp >= current.access_expires_at:
                        retry_horizon = timestamp + self._retry_delay(failures)
                        if timestamp < current.slot_hold_until:
                            state = (
                                ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY
                            )
                            # The old provider slot is still conservatively occupied.
                            # Never let its natural-expiry horizon undercut the bounded
                            # retry/provider-health delay produced by this failed refresh.
                            hold = max(current.slot_hold_until, retry_horizon)
                            retry = None
                        else:
                            state = (
                                ProphetXSessionState.PROVIDER_UNAVAILABLE
                                if failure
                                is ProphetXRenewalFailureClass.PROVIDER_UNAVAILABLE
                                else ProphetXSessionState.AUTH_RETRYABLE_FAILURE
                            )
                            hold = None
                            retry = retry_horizon
                        lineage = None
                        expiry = None
                    else:
                        state = ProphetXSessionState.RENEWAL_DUE
                        retry = timestamp + self._retry_delay(failures)
                        # Preserve the bounded no-login horizon across access-token
                        # expiry. Otherwise a failure just before a long-lived token
                        # expires can be laundered into an immediate replacement login
                        # when slot_hold_until equals access_expires_at.
                        hold = max(current.slot_hold_until, retry)
                        lineage = current.session_lineage_id
                        expiry = current.access_expires_at

                    hold_started_at = (
                        timestamp
                        if failure
                        is ProphetXRenewalFailureClass.AMBIGUOUS_PROVIDER_RESULT
                        else current.slot_hold_started_at
                        if hold is not None
                        else None
                    )

                    login_failure_evidence = current.last_failure_class
                    if state in {
                        ProphetXSessionState.AUTH_RETRYABLE_FAILURE,
                        ProphetXSessionState.PROVIDER_UNAVAILABLE,
                        ProphetXSessionState.CREDENTIAL_REJECTED,
                    }:
                        login_failure_evidence = None

                    updated = ProphetXSessionSnapshot(
                        state=state,
                        generation=current.generation + 1,
                        credential_revision=current.credential_revision,
                        integration_role=current.integration_role,
                        last_transition_at=timestamp,
                        session_lineage_id=lineage,
                        access_expires_at=expiry,
                        slot_hold_started_at=hold_started_at,
                        slot_hold_until=hold,
                        retry_not_before=retry,
                        transient_failures=failures,
                        last_failure_class=login_failure_evidence,
                        last_renewal_failure_class=failure,
                    )
                    self._write_state(updated)
            except WorkspaceEconomicLockError as exc:
                raise ProphetXSessionLifecycleError(
                    "cannot acquire ProphetX session-pool coordination lock"
                ) from exc
            self._discard_owned_attempt(attempt)
            return updated

    def complete_login_success(
        self,
        *,
        attempt_id: str,
        now: datetime,
        access_expires_at: datetime,
    ) -> ProphetXSessionSnapshot:
        """Record provider-issued expiry; never infer token lifetime locally."""

        attempt = _sha256_hex(attempt_id, "attempt_id")
        timestamp = _aware_utc(now, "now")
        expires = _aware_utc(access_expires_at, "access_expires_at")
        if expires <= timestamp:
            raise ProphetXSessionLifecycleError(
                "provider access_expires_at must be later than login completion"
            )
        with self._thread_lock:
            if attempt not in self._owned_attempts:
                raise ProphetXSessionLifecycleError(
                    "login attempt was not issued by this coordinator"
                )
            try:
                with WorkspaceEconomicLock(self._scope_dir):
                    current = self._require_owned_inflight(attempt)
                    self._require_monotonic_transition(current, timestamp)
                    updated = ProphetXSessionSnapshot(
                        state=ProphetXSessionState.ACTIVE,
                        generation=current.generation + 1,
                        credential_revision=self.scope.credential_revision,
                        integration_role=self.scope.integration_role,
                        last_transition_at=timestamp,
                        session_lineage_id=attempt,
                        access_expires_at=expires,
                        slot_hold_started_at=timestamp,
                        slot_hold_until=max(
                            expires,
                            timestamp + CONSERVATIVE_SESSION_SLOT_HOLD,
                        ),
                    )
                    self._write_state(updated)
            except WorkspaceEconomicLockError as exc:
                raise ProphetXSessionLifecycleError(
                    "cannot acquire ProphetX session-pool coordination lock"
                ) from exc
            self._discard_owned_attempt(attempt)
            return updated

    def complete_login_failure(
        self,
        *,
        attempt_id: str,
        now: datetime,
        failure: ProphetXLoginFailureClass,
    ) -> ProphetXSessionSnapshot:
        attempt = _sha256_hex(attempt_id, "attempt_id")
        timestamp = _aware_utc(now, "now")
        if type(failure) is not ProphetXLoginFailureClass:
            raise ProphetXSessionLifecycleError(
                "failure must be an exact ProphetXLoginFailureClass"
            )
        with self._thread_lock:
            if attempt not in self._owned_attempts:
                raise ProphetXSessionLifecycleError(
                    "login attempt was not issued by this coordinator"
                )
            try:
                with WorkspaceEconomicLock(self._scope_dir):
                    current = self._require_owned_inflight(attempt)
                    self._require_monotonic_transition(current, timestamp)
                    failures = current.transient_failures + 1
                    retry_not_before: datetime | None = None
                    slot_hold_started_at: datetime | None = None
                    slot_hold_until: datetime | None = None

                    if failure is ProphetXLoginFailureClass.SESSION_POOL_EXHAUSTED:
                        state = ProphetXSessionState.SESSION_POOL_EXHAUSTED
                        slot_hold_started_at = timestamp
                        slot_hold_until = timestamp + CONSERVATIVE_SESSION_SLOT_HOLD
                    elif failure is ProphetXLoginFailureClass.CREDENTIAL_REJECTED:
                        state = ProphetXSessionState.CREDENTIAL_REJECTED
                        failures = current.transient_failures
                    elif failure is ProphetXLoginFailureClass.RETRYABLE_PRE_SESSION_FAILURE:
                        state = ProphetXSessionState.AUTH_RETRYABLE_FAILURE
                        retry_not_before = timestamp + self._retry_delay(failures)
                    elif failure is ProphetXLoginFailureClass.PROVIDER_UNAVAILABLE_PRE_SESSION:
                        state = ProphetXSessionState.PROVIDER_UNAVAILABLE
                        retry_not_before = timestamp + self._retry_delay(failures)
                    else:
                        state = ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY
                        slot_hold_started_at = timestamp
                        slot_hold_until = timestamp + CONSERVATIVE_SESSION_SLOT_HOLD

                    updated = ProphetXSessionSnapshot(
                        state=state,
                        generation=current.generation + 1,
                        credential_revision=self.scope.credential_revision,
                        integration_role=self.scope.integration_role,
                        last_transition_at=timestamp,
                        slot_hold_started_at=slot_hold_started_at,
                        slot_hold_until=slot_hold_until,
                        retry_not_before=retry_not_before,
                        transient_failures=failures,
                        last_failure_class=failure,
                    )
                    self._write_state(updated)
            except WorkspaceEconomicLockError as exc:
                raise ProphetXSessionLifecycleError(
                    "cannot acquire ProphetX session-pool coordination lock"
                ) from exc
            self._discard_owned_attempt(attempt)
            return updated

    def record_credential_revoked(
        self,
        *,
        now: datetime,
    ) -> ProphetXSessionSnapshot:
        """Invalidate capability without pretending revocation frees a provider slot."""

        timestamp = _aware_utc(now, "now")
        with self._thread_lock:
            try:
                with WorkspaceEconomicLock(self._scope_dir):
                    current = self._load_state()
                    if current is None:
                        generation = 0
                        hold_started_at = None
                        hold = None
                        failures = 0
                    else:
                        self._require_monotonic_transition(current, timestamp)
                        self._require_role_compatible(current)
                        generation = current.generation + 1
                        hold_started_at = current.slot_hold_started_at
                        hold = current.slot_hold_until
                        if current.state is ProphetXSessionState.RENEWING:
                            renewal_hold = self._renewal_uncertainty_deadline(current)
                            if hold is None or renewal_hold > hold:
                                hold = renewal_hold
                        if hold is not None and hold <= timestamp:
                            hold_started_at = None
                            hold = None
                        failures = current.transient_failures
                    updated = ProphetXSessionSnapshot(
                        state=ProphetXSessionState.CREDENTIAL_REJECTED,
                        generation=generation,
                        credential_revision=self.scope.credential_revision,
                        integration_role=self.scope.integration_role,
                        last_transition_at=timestamp,
                        slot_hold_started_at=hold_started_at,
                        slot_hold_until=hold,
                        transient_failures=failures,
                        last_failure_class=ProphetXLoginFailureClass.CREDENTIAL_REJECTED,
                    )
                    self._write_state(updated)
                    if current is not None and current.attempt_id is not None:
                        self._discard_owned_attempt(current.attempt_id)
                    return updated
            except WorkspaceEconomicLockError as exc:
                raise ProphetXSessionLifecycleError(
                    "cannot acquire ProphetX session-pool coordination lock"
                ) from exc

    def read_snapshot(self) -> ProphetXSessionSnapshot | None:
        with self._thread_lock:
            try:
                with WorkspaceEconomicLock(self._scope_dir):
                    return self._load_state()
            except WorkspaceEconomicLockBusyError:
                raise ProphetXSessionLifecycleError(
                    "ProphetX session-pool state is currently being updated"
                ) from None
            except WorkspaceEconomicLockError as exc:
                raise ProphetXSessionLifecycleError(
                    "cannot acquire ProphetX session-pool coordination lock"
                ) from exc

    def _require_monotonic_transition(
        self,
        current: ProphetXSessionSnapshot,
        now: datetime,
    ) -> None:
        if now < current.last_transition_at:
            raise ProphetXSessionLifecycleError(
                "transition time cannot precede persisted lifecycle time"
            )

    def _begin_login_locked(
        self,
        current: ProphetXSessionSnapshot | None,
        now: datetime,
        *,
        access_token_available: bool,
        access_token_lineage_id: str | None,
    ) -> ProphetXLoginAdmission:
        if current is None:
            if access_token_available:
                raise ProphetXSessionLifecycleError(
                    "available access token cannot be reconciled without durable session state"
                )
            return self._grant_login(now, generation=0, transient_failures=0)

        self._require_monotonic_transition(current, now)

        if current.integration_role != self.scope.integration_role:
            return ProphetXLoginAdmission(
                action=ProphetXLoginAdmissionAction.SHARED_ACCESS_KEY_CONFLICT,
                snapshot=current,
                retry_at=current.slot_hold_until,
            )

        if current.credential_revision != self.scope.credential_revision:
            if (
                current.state
                in {
                    ProphetXSessionState.AUTH_RETRYABLE_FAILURE,
                    ProphetXSessionState.PROVIDER_UNAVAILABLE,
                }
                and current.retry_not_before is not None
                and now < current.retry_not_before
            ):
                # Credential rotation must not launder transient transport/provider
                # health backoff into a fresh login attempt. Only an explicit
                # credential-rejection path is recoverable merely by rotating secrets.
                return ProphetXLoginAdmission(
                    action=ProphetXLoginAdmissionAction.RETRY_LATER,
                    snapshot=current,
                    retry_at=current.retry_not_before,
                )
            rotation_hold = current.slot_hold_until
            if current.state is ProphetXSessionState.RENEWING:
                # An in-flight refresh may have extended provider occupancy beyond
                # the pre-refresh slot hold even when credentials rotate meanwhile.
                rotation_hold = self._renewal_uncertainty_deadline(current)
            if rotation_hold is not None and now < rotation_hold:
                return ProphetXLoginAdmission(
                    action=ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY,
                    snapshot=current,
                    retry_at=rotation_hold,
                )
            return self._grant_login(
                now,
                generation=current.generation + 1,
                transient_failures=0,
            )

        if current.state is ProphetXSessionState.LOGIN_IN_FLIGHT:
            if current.slot_hold_until is not None and now < current.slot_hold_until:
                action = (
                    ProphetXLoginAdmissionAction.WAIT_FOR_EXISTING_LOGIN
                    if current.attempt_id in self._owned_attempts
                    else ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY
                )
                return ProphetXLoginAdmission(
                    action=action,
                    snapshot=current,
                    retry_at=current.slot_hold_until,
                )
            if current.attempt_id is not None:
                self._discard_owned_attempt(current.attempt_id)
            return self._grant_login(
                now,
                generation=current.generation + 1,
                transient_failures=current.transient_failures,
            )

        if current.state is ProphetXSessionState.RENEWING:
            uncertainty_deadline = self._renewal_uncertainty_deadline(current)
            if (
                current.attempt_id in self._owned_attempts
                and now < uncertainty_deadline
            ):
                return ProphetXLoginAdmission(
                    action=ProphetXLoginAdmissionAction.WAIT_FOR_EXISTING_RENEWAL,
                    snapshot=current,
                    retry_at=uncertainty_deadline,
                )
            if now < uncertainty_deadline:
                return ProphetXLoginAdmission(
                    action=ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY,
                    snapshot=current,
                    retry_at=uncertainty_deadline,
                )
            if current.attempt_id is not None:
                self._discard_owned_attempt(current.attempt_id)
            return self._grant_login(
                now,
                generation=current.generation + 1,
                transient_failures=current.transient_failures,
            )

        if current.state in {
            ProphetXSessionState.ACTIVE,
            ProphetXSessionState.RENEWAL_DUE,
        }:
            if current.access_expires_at is None or current.slot_hold_until is None:
                raise ProphetXSessionLifecycleError(
                    "active session state is missing expiry evidence"
                )
            if now >= current.access_expires_at:
                if now < current.slot_hold_until:
                    waiting = ProphetXSessionSnapshot(
                        state=(
                            ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY
                        ),
                        generation=current.generation + 1,
                        credential_revision=current.credential_revision,
                        integration_role=current.integration_role,
                        last_transition_at=now,
                        slot_hold_started_at=current.slot_hold_started_at,
                        slot_hold_until=current.slot_hold_until,
                        transient_failures=current.transient_failures,
                        last_failure_class=current.last_failure_class,
                        last_renewal_failure_class=current.last_renewal_failure_class,
                    )
                    self._write_state(waiting)
                    return ProphetXLoginAdmission(
                        action=(
                            ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY
                        ),
                        snapshot=waiting,
                        retry_at=waiting.slot_hold_until,
                    )
                return self._grant_login(
                    now,
                    generation=current.generation + 1,
                    transient_failures=0,
                )
            if (
                not access_token_available
                or access_token_lineage_id != current.session_lineage_id
            ):
                return ProphetXLoginAdmission(
                    action=ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY,
                    snapshot=current,
                    retry_at=current.slot_hold_until,
                )
            if now >= current.access_expires_at - RENEWAL_LEAD_TIME:
                if (
                    current.state is ProphetXSessionState.RENEWAL_DUE
                    and current.retry_not_before is not None
                    and now < current.retry_not_before
                ):
                    return ProphetXLoginAdmission(
                        action=ProphetXLoginAdmissionAction.RENEWAL_REQUIRED,
                        snapshot=current,
                        retry_at=current.retry_not_before,
                    )
                if current.state is not ProphetXSessionState.RENEWAL_DUE:
                    current = ProphetXSessionSnapshot(
                        state=ProphetXSessionState.RENEWAL_DUE,
                        generation=current.generation + 1,
                        credential_revision=current.credential_revision,
                        integration_role=current.integration_role,
                        last_transition_at=now,
                        session_lineage_id=current.session_lineage_id,
                        access_expires_at=current.access_expires_at,
                        slot_hold_started_at=current.slot_hold_started_at,
                        slot_hold_until=current.slot_hold_until,
                        transient_failures=current.transient_failures,
                        last_failure_class=current.last_failure_class,
                    )
                    self._write_state(current)
                return ProphetXLoginAdmission(
                    action=ProphetXLoginAdmissionAction.RENEWAL_REQUIRED,
                    snapshot=current,
                    retry_at=current.access_expires_at,
                )
            return ProphetXLoginAdmission(
                action=ProphetXLoginAdmissionAction.REUSE_ACTIVE,
                snapshot=current,
            )

        if current.state in {
            ProphetXSessionState.SESSION_POOL_EXHAUSTED,
            ProphetXSessionState.WAIT_FOR_PROVIDER_SESSION_EXPIRY,
        }:
            if current.slot_hold_until is not None and now < current.slot_hold_until:
                return ProphetXLoginAdmission(
                    action=ProphetXLoginAdmissionAction.WAIT_FOR_PROVIDER_SESSION_EXPIRY,
                    snapshot=current,
                    retry_at=current.slot_hold_until,
                )
            return self._grant_login(
                now,
                generation=current.generation + 1,
                transient_failures=current.transient_failures,
            )

        if current.state in {
            ProphetXSessionState.AUTH_RETRYABLE_FAILURE,
            ProphetXSessionState.PROVIDER_UNAVAILABLE,
        }:
            if current.retry_not_before is None:
                raise ProphetXSessionLifecycleError(
                    "retryable failure is missing retry_not_before"
                )
            if now < current.retry_not_before:
                return ProphetXLoginAdmission(
                    action=ProphetXLoginAdmissionAction.RETRY_LATER,
                    snapshot=current,
                    retry_at=current.retry_not_before,
                )
            return self._grant_login(
                now,
                generation=current.generation + 1,
                transient_failures=current.transient_failures,
            )

        if current.state is ProphetXSessionState.CREDENTIAL_REJECTED:
            return ProphetXLoginAdmission(
                action=ProphetXLoginAdmissionAction.CREDENTIAL_REJECTED,
                snapshot=current,
            )

        return self._grant_login(
            now,
            generation=current.generation + 1,
            transient_failures=current.transient_failures,
        )

    def _grant_login(
        self,
        now: datetime,
        *,
        generation: int,
        transient_failures: int,
    ) -> ProphetXLoginAdmission:
        attempt = token_hex(32)
        hold = now + CONSERVATIVE_SESSION_SLOT_HOLD
        updated = ProphetXSessionSnapshot(
            state=ProphetXSessionState.LOGIN_IN_FLIGHT,
            generation=generation,
            credential_revision=self.scope.credential_revision,
            integration_role=self.scope.integration_role,
            last_transition_at=now,
            attempt_id=attempt,
            attempt_started_at=now,
            slot_hold_started_at=now,
            slot_hold_until=hold,
            transient_failures=transient_failures,
        )
        self._write_state(updated)
        self._owned_attempts.add(attempt)
        admission = ProphetXLoginAdmission(
            action=ProphetXLoginAdmissionAction.CREATE_LOGIN,
            snapshot=updated,
            attempt_id=attempt,
            retry_at=hold,
        )
        self._issued_effect_admissions[attempt] = admission
        return admission

    def _discard_owned_attempt(self, attempt_id: str) -> None:
        self._owned_attempts.discard(attempt_id)
        self._issued_effect_admissions.pop(attempt_id, None)

    def _require_owned_inflight(
        self,
        attempt_id: str,
    ) -> ProphetXSessionSnapshot:
        current = self._load_state()
        if (
            current is None
            or current.state is not ProphetXSessionState.LOGIN_IN_FLIGHT
            or current.attempt_id != attempt_id
            or current.credential_revision != self.scope.credential_revision
            or current.integration_role != self.scope.integration_role
        ):
            raise ProphetXSessionLifecycleError(
                "login attempt no longer owns current session-pool admission"
            )
        return current

    def _renewal_uncertainty_deadline(
        self,
        current: ProphetXSessionSnapshot,
    ) -> datetime:
        if (
            current.state is not ProphetXSessionState.RENEWING
            or current.attempt_started_at is None
        ):
            raise ProphetXSessionLifecycleError(
                "renewal uncertainty deadline requires renewing attempt evidence"
            )
        candidate = current.attempt_started_at + CONSERVATIVE_SESSION_SLOT_HOLD
        if current.slot_hold_until is not None and current.slot_hold_until > candidate:
            return current.slot_hold_until
        return candidate

    def _require_owned_renewal(
        self,
        attempt_id: str,
    ) -> ProphetXSessionSnapshot:
        current = self._load_state()
        if (
            current is None
            or current.state is not ProphetXSessionState.RENEWING
            or current.attempt_id != attempt_id
            or current.credential_revision != self.scope.credential_revision
            or current.integration_role != self.scope.integration_role
        ):
            raise ProphetXSessionLifecycleError(
                "renewal attempt no longer owns current session authority"
            )
        return current

    def _require_role_compatible(
        self,
        current: ProphetXSessionSnapshot,
    ) -> None:
        if current.integration_role != self.scope.integration_role:
            raise ProphetXSessionLifecycleError(
                "access key is already bound to a different integration role"
            )

    def _retry_delay(self, failures: int) -> timedelta:
        count = max(1, min(failures, 32))
        base = min(_BASE_RETRY_SECONDS * (2 ** (count - 1)), 240)
        material = f"{self.scope.pool_id}:{count}".encode("ascii")
        jitter_window = max(1, base // 4)
        jitter = int.from_bytes(sha256(material).digest()[:4], "big") % (
            jitter_window + 1
        )
        return timedelta(
            seconds=min(base + jitter, _MAX_RETRY_SECONDS)
        )

    def _load_state(self) -> ProphetXSessionSnapshot | None:
        try:
            descriptor = _open_read_only_descriptor(self._state_path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ProphetXSessionLifecycleError(
                "cannot safely open ProphetX session state"
            ) from exc

        try:
            path_stat = os.fstat(descriptor)
            if not stat.S_ISREG(path_stat.st_mode) or path_stat.st_nlink != 1:
                raise ProphetXSessionLifecycleError(
                    "ProphetX session state must be a single-link regular file"
                )
            if path_stat.st_size > _MAX_STATE_FILE_BYTES:
                raise ProphetXSessionLifecycleError(
                    "ProphetX session state exceeds the bounded file-size contract"
                )
            with os.fdopen(descriptor, "r", encoding="utf-8", closefd=True) as handle:
                descriptor = -1
                raw = handle.read(_MAX_STATE_FILE_BYTES + 1)
            if len(raw.encode("utf-8")) > _MAX_STATE_FILE_BYTES:
                raise ProphetXSessionLifecycleError(
                    "ProphetX session state exceeds the bounded file-size contract"
                )
            payload = json.loads(
                raw,
                object_pairs_hook=_strict_json_object_pairs,
                parse_constant=_reject_nonfinite_json_constant,
            )
        except ProphetXSessionLifecycleError:
            raise
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            ValueError,
            RecursionError,
        ) as exc:
            raise ProphetXSessionLifecycleError(
                "ProphetX session state is unreadable or corrupt"
            ) from exc
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        if type(payload) is not dict:
            raise ProphetXSessionLifecycleError(
                "ProphetX session state root must be an object"
            )
        expected_fields = {
            "schema_version",
            "provider",
            "environment",
            "access_key_identity_sha256",
            "state",
            "generation",
            "credential_revision",
            "integration_role",
            "last_transition_at",
            "attempt_id",
            "attempt_started_at",
            "session_lineage_id",
            "access_expires_at",
            "slot_hold_started_at",
            "slot_hold_until",
            "retry_not_before",
            "transient_failures",
            "last_failure_class",
            "last_renewal_failure_class",
        }
        if set(payload) != expected_fields:
            raise ProphetXSessionLifecycleError(
                "ProphetX session state has an unexpected schema"
            )
        if payload["schema_version"] != STATE_SCHEMA_VERSION:
            raise ProphetXSessionLifecycleError(
                "unsupported ProphetX session state schema"
            )
        if payload["provider"] != PROVIDER_ID:
            raise ProphetXSessionLifecycleError(
                "ProphetX session state provider mismatch"
            )
        if payload["environment"] != self.scope.environment:
            raise ProphetXSessionLifecycleError(
                "ProphetX session state environment mismatch"
            )
        if (
            payload["access_key_identity_sha256"]
            != self.scope.access_key_identity_sha256
        ):
            raise ProphetXSessionLifecycleError(
                "ProphetX access-key identity mismatch"
            )
        try:
            state = ProphetXSessionState(payload["state"])
        except (TypeError, ValueError) as exc:
            raise ProphetXSessionLifecycleError(
                "unknown ProphetX session state"
            ) from exc
        raw_failure = payload["last_failure_class"]
        try:
            failure = (
                None
                if raw_failure is None
                else ProphetXLoginFailureClass(raw_failure)
            )
        except (TypeError, ValueError) as exc:
            raise ProphetXSessionLifecycleError(
                "unknown ProphetX login failure class"
            ) from exc
        renewal_failure_raw = payload["last_renewal_failure_class"]
        try:
            renewal_failure = (
                None
                if renewal_failure_raw is None
                else ProphetXRenewalFailureClass(renewal_failure_raw)
            )
        except (TypeError, ValueError) as exc:
            raise ProphetXSessionLifecycleError(
                "unknown ProphetX renewal failure class"
            ) from exc
        return ProphetXSessionSnapshot(
            state=state,
            generation=_nonnegative_int(payload["generation"], "generation"),
            credential_revision=_required_text(
                payload["credential_revision"],
                "credential_revision",
            ),
            integration_role=_required_text(
                payload["integration_role"],
                "integration_role",
            ),
            last_transition_at=_parse_iso(
                payload["last_transition_at"],
                "last_transition_at",
            ),
            attempt_id=(
                None
                if payload["attempt_id"] is None
                else _sha256_hex(payload["attempt_id"], "attempt_id")
            ),
            attempt_started_at=_parse_iso(
                payload["attempt_started_at"],
                "attempt_started_at",
                optional=True,
            ),
            session_lineage_id=(
                None
                if payload["session_lineage_id"] is None
                else _sha256_hex(
                    payload["session_lineage_id"],
                    "session_lineage_id",
                )
            ),
            access_expires_at=_parse_iso(
                payload["access_expires_at"],
                "access_expires_at",
                optional=True,
            ),
            slot_hold_started_at=_parse_iso(
                payload["slot_hold_started_at"],
                "slot_hold_started_at",
                optional=True,
            ),
            slot_hold_until=_parse_iso(
                payload["slot_hold_until"],
                "slot_hold_until",
                optional=True,
            ),
            retry_not_before=_parse_iso(
                payload["retry_not_before"],
                "retry_not_before",
                optional=True,
            ),
            transient_failures=_nonnegative_int(
                payload["transient_failures"],
                "transient_failures",
            ),
            last_failure_class=failure,
            last_renewal_failure_class=renewal_failure,
        )

    def _write_state(self, snapshot: ProphetXSessionSnapshot) -> None:
        if type(snapshot) is not ProphetXSessionSnapshot:
            raise ProphetXSessionLifecycleError(
                "snapshot must be an exact ProphetXSessionSnapshot"
            )
        self._scope_dir.mkdir(parents=True, exist_ok=True)
        payload = snapshot.to_json_dict(
            environment=self.scope.environment,
            access_key_identity_sha256=self.scope.access_key_identity_sha256,
        )
        encoded = (
            json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
            + chr(10)
        )
        if len(encoded.encode("utf-8")) > _MAX_STATE_FILE_BYTES:
            raise ProphetXSessionLifecycleError(
                "ProphetX session state exceeds the bounded file-size contract"
            )
        temporary = self._scope_dir / (
            f".{self._STATE_NAME}.{os.getpid()}.{token_hex(8)}.tmp"
        )
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._state_path)
            if os.name != "nt":
                directory_fd = os.open(self._scope_dir, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise ProphetXSessionLifecycleError(
                "cannot durably write ProphetX session state"
            ) from exc

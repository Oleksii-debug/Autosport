"""Fail-closed classification for degraded bookmaker account readback.

This module intentionally owns *negative observability* only.  It can classify a
failed or incomplete readback into a conservative degradation state, but it has
no API that can issue FRESH_COMPLETE or prove an account/position set empty.
Positive completeness must come from the canonical provider acquisition and
account-snapshot authorities.
"""

from __future__ import annotations

from dataclasses import InitVar, dataclass
from enum import Enum
import hashlib
import json
import re


_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_PROVIDER_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class ReadbackDegradationError(ValueError):
    """Raised when degraded-readback evidence is malformed or ambiguous."""


class ReadbackDegradationState(str, Enum):
    """Closed set of non-positive account-readback states."""

    INCOMPLETE_PARTIAL = "incomplete_partial"
    STALE_LAST_KNOWN = "stale_last_known"
    UNAVAILABLE_TRANSIENT = "unavailable_transient"
    UNAUTHORIZED_OR_SESSION_EXPIRED = "unauthorized_or_session_expired"
    CONFIGURATION_DENIED = "configuration_denied"
    CONFLICTING = "conflicting"
    UNKNOWN = "unknown"


class ReadbackFailureKind(str, Enum):
    """Provider-neutral reason category supplied by a trusted adapter boundary."""

    TRANSPORT = "transport"
    PROVIDER_ERROR = "provider_error"
    MALFORMED_RESPONSE = "malformed_response"
    IDENTITY_CONFLICT = "identity_conflict"
    INCOMPLETE_PAGINATION = "incomplete_pagination"
    STALE_CACHE = "stale_cache"
    UNKNOWN = "unknown"


_BETFAIR_UNAUTHORIZED_CODES = frozenset(
    {
        "INVALID_SESSION_INFORMATION",
        "NO_SESSION",
    }
)
_BETFAIR_CONFIGURATION_CODES = frozenset(
    {
        "INVALID_APP_KEY",
        "NO_APP_KEY",
        "ACCESS_DENIED",
        "SUBSCRIPTION_EXPIRED",
        "INVALID_SUBSCRIPTION_TOKEN",
        "USER_NOT_SUBSCRIBED",
    }
)
_BETFAIR_TRANSIENT_CODES = frozenset(
    {
        "TOO_MANY_REQUESTS",
        "SERVICE_BUSY",
        "TIMEOUT_ERROR",
    }
)
_TRANSIENT_HTTP_STATUSES = frozenset({408, 429, 500, 502, 503, 504})
_UNAUTHORIZED_HTTP_STATUSES = frozenset({401, 403})
_ISSUANCE_TOKEN = object()


def _require_token(value: object, *, field: str) -> str:
    if type(value) is not str or not _TOKEN_RE.fullmatch(value):
        raise ReadbackDegradationError(f"{field} must be a bounded canonical token")
    return value


def _require_provider_id(value: object) -> str:
    provider_id = _require_token(value, field="provider_id")
    if provider_id != provider_id.lower():
        raise ReadbackDegradationError("provider_id must use canonical lowercase form")
    return provider_id


def _require_provider_code(value: object | None) -> str | None:
    if value is None:
        return None
    if type(value) is not str or not _PROVIDER_CODE_RE.fullmatch(value):
        raise ReadbackDegradationError(
            "provider_code must be an uppercase provider error token"
        )
    return value


def _require_http_status(value: object | None) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 100 <= value <= 599:
        raise ReadbackDegradationError("http_status must be an integer in 100..599")
    return value


@dataclass(frozen=True, slots=True)
class ReadbackFailureSignal:
    """Sanitized adapter evidence for one failed/incomplete account read.

    Raw response bodies, exception text, credentials, session tokens and headers
    are intentionally absent. pages_completed is bookkeeping only; it never
    proves that the returned prefix is an authoritative complete account view.
    """

    provider_id: str
    operation: str
    kind: ReadbackFailureKind
    provider_code: str | None = None
    http_status: int | None = None
    pages_completed: int = 0
    prior_snapshot_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "provider_id",
            _require_provider_id(self.provider_id),
        )
        object.__setattr__(
            self,
            "operation",
            _require_token(self.operation, field="operation"),
        )
        if type(self.kind) is not ReadbackFailureKind:
            raise ReadbackDegradationError("kind must be an exact ReadbackFailureKind")
        object.__setattr__(self, "provider_code", _require_provider_code(self.provider_code))
        object.__setattr__(self, "http_status", _require_http_status(self.http_status))
        if type(self.pages_completed) is not int or self.pages_completed < 0:
            raise ReadbackDegradationError("pages_completed must be a non-negative integer")
        if self.prior_snapshot_id is not None:
            object.__setattr__(
                self,
                "prior_snapshot_id",
                _require_token(self.prior_snapshot_id, field="prior_snapshot_id"),
            )


@dataclass(frozen=True, slots=True)
class AccountReadbackDegradationEvidence:
    """Conservative product result for a degraded account-readback attempt.

    Every authority-bearing property is intentionally restrictive.  This type is
    not a substitute for canonical account snapshot/acquisition evidence.
    """

    state: ReadbackDegradationState
    provider_id: str
    operation: str
    provider_code: str | None
    http_status: int | None
    partial_observation_present: bool
    prior_snapshot_id: str | None
    evidence_sha256: str
    _issuance_token: InitVar[object] = None

    def __post_init__(self, _issuance_token: object) -> None:
        if _issuance_token is not _ISSUANCE_TOKEN:
            raise ReadbackDegradationError(
                "degradation evidence must be issued by the classifier"
            )

    @property
    def freshness_proven(self) -> bool:
        return False

    @property
    def fresh_complete_proven(self) -> bool:
        return False

    @property
    def empty_scope_proven(self) -> bool:
        return False

    @property
    def current_balance_proven(self) -> bool:
        return False

    @property
    def current_exposure_proven(self) -> bool:
        return False

    @property
    def may_release_contingent_exposure(self) -> bool:
        return False

    @property
    def may_authorize_provider_failover(self) -> bool:
        return False

    @property
    def may_authorize_new_stake(self) -> bool:
        return False


def _classify_state(signal: ReadbackFailureSignal) -> ReadbackDegradationState:
    if signal.kind is ReadbackFailureKind.IDENTITY_CONFLICT:
        return ReadbackDegradationState.CONFLICTING
    if signal.kind is ReadbackFailureKind.MALFORMED_RESPONSE:
        return ReadbackDegradationState.UNKNOWN

    # Preserve actionable provider failure truth even when a prefix of a paged
    # read succeeded. partial_observation_present separately records that
    # useful-but-incomplete prefix; it never upgrades current authority.
    code = signal.provider_code
    if signal.provider_id == "betfair" and code in _BETFAIR_UNAUTHORIZED_CODES:
        return ReadbackDegradationState.UNAUTHORIZED_OR_SESSION_EXPIRED
    if signal.provider_id == "betfair" and code in _BETFAIR_CONFIGURATION_CODES:
        return ReadbackDegradationState.CONFIGURATION_DENIED
    if signal.provider_id == "betfair" and code in _BETFAIR_TRANSIENT_CODES:
        return ReadbackDegradationState.UNAVAILABLE_TRANSIENT

    status = signal.http_status
    if status in _UNAUTHORIZED_HTTP_STATUSES:
        return ReadbackDegradationState.UNAUTHORIZED_OR_SESSION_EXPIRED
    if status in _TRANSIENT_HTTP_STATUSES:
        return ReadbackDegradationState.UNAVAILABLE_TRANSIENT
    if signal.kind is ReadbackFailureKind.INCOMPLETE_PAGINATION:
        return ReadbackDegradationState.INCOMPLETE_PARTIAL
    if signal.kind is ReadbackFailureKind.STALE_CACHE:
        if signal.prior_snapshot_id is None:
            return ReadbackDegradationState.UNKNOWN
        return ReadbackDegradationState.STALE_LAST_KNOWN
    if signal.kind is ReadbackFailureKind.TRANSPORT:
        return ReadbackDegradationState.UNAVAILABLE_TRANSIENT
    return ReadbackDegradationState.UNKNOWN


def classify_account_readback_degradation(
    signal: ReadbackFailureSignal,
) -> AccountReadbackDegradationEvidence:
    """Classify one non-successful account read without granting positive truth.

    The caller cannot turn a timeout, HTTP/provider error, stale cache or partial
    page chain into a zero balance/position result through this API.  A later
    successful read must be re-issued by the canonical positive acquisition path.
    """

    if type(signal) is not ReadbackFailureSignal:
        raise ReadbackDegradationError("signal must be an exact ReadbackFailureSignal")

    state = _classify_state(signal)
    core = {
        "schema": "autosport.account-readback-degradation.v1",
        "schema_version": 1,
        "state": state.value,
        "provider_id": signal.provider_id,
        "operation": signal.operation,
        "provider_code": signal.provider_code,
        "http_status": signal.http_status,
        "pages_completed": signal.pages_completed,
        "prior_snapshot_id": signal.prior_snapshot_id,
    }
    canonical = json.dumps(
        core,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return AccountReadbackDegradationEvidence(
        state=state,
        provider_id=signal.provider_id,
        operation=signal.operation,
        provider_code=signal.provider_code,
        http_status=signal.http_status,
        partial_observation_present=signal.pages_completed > 0,
        prior_snapshot_id=signal.prior_snapshot_id,
        evidence_sha256=hashlib.sha256(canonical).hexdigest(),
        _issuance_token=_ISSUANCE_TOKEN,
    )

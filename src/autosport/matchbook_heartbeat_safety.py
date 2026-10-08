"""Fail-closed Matchbook heartbeat liveness/restart authority.

Positive heartbeat authority is an ephemeral exact-object capability minted only by
the fixed canonical HTTPS heartbeat transport. Durable registration bytes remain
audit evidence after restart but cannot recreate provider-origin liveness authority.
This is an application authority boundary against caller-created objects and ordinary
consumer misuse, not an OS sandbox against arbitrary in-process code injection.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


_HEX = frozenset("0123456789abcdef")
_PROVIDER = "matchbook"
_HEARTBEAT_URL = "https://api.matchbook.com/edge/rest/v1/heartbeat"
_HEARTBEAT_HOST = "api.matchbook.com"
_HEARTBEAT_PATH = "/edge/rest/v1/heartbeat"
_MAX_RESPONSE_BYTES = 16 * 1024
_REGISTRATION_SCHEMA_VERSION = 2
_UNSUBSCRIBE_SCHEMA_VERSION = 2
_CANCELLATION_SCHEMA_VERSION = 1
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


class MatchbookHeartbeatSafetyError(RuntimeError):
    """Raised when heartbeat safety evidence is invalid or causally unusable."""


class MatchbookHeartbeatTransportError(MatchbookHeartbeatSafetyError):
    """Raised when canonical Matchbook heartbeat transport cannot prove success."""


class MatchbookHeartbeatLeaseState(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    DEGRADED_UNKNOWN = "DEGRADED_UNKNOWN"
    SESSION_ROTATED = "SESSION_ROTATED"
    SUPERSEDED = "SUPERSEDED"
    UNSUBSCRIBED = "UNSUBSCRIBED"
    RESTART_REQUIRES_REREGISTRATION = "RESTART_REQUIRES_REREGISTRATION"


class MatchbookCancellationReason(StrEnum):
    USER_REQUEST = "user_request"
    HEARTBEAT_EXPIRY = "heartbeat_expiry"


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        del req, fp, msg, headers, newurl
        if code in _REDIRECT_CODES:
            return None
        return None


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _digest(payload: Mapping[str, Any]) -> str:
    return _digest_bytes(_canonical_json(payload).encode("utf-8"))


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


def _positive_timeout(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive finite number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return number


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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _monotonic_now() -> float:
    return time.monotonic()


def _session_token(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("session_token must be non-empty trimmed text")
    if any(character.isspace() for character in value):
        raise ValueError("session_token must not contain whitespace")
    return value


def _strict_json_loads(raw: bytes) -> Any:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MatchbookHeartbeatTransportError(
            "heartbeat provider returned invalid UTF-8"
        ) from None

    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate key: {key}")
            result[key] = value
        return result

    def constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    try:
        return json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        raise MatchbookHeartbeatTransportError(
            "heartbeat provider returned invalid JSON"
        ) from None


def _heartbeat_request(
    *,
    method: str,
    session_token: str,
    request_body: bytes | None,
) -> Request:
    token = _session_token(session_token)
    if method not in {"POST", "DELETE"}:
        raise ValueError("heartbeat method must be POST or DELETE")
    parsed = urlsplit(_HEARTBEAT_URL)
    if (
        parsed.scheme != "https"
        or parsed.netloc != _HEARTBEAT_HOST
        or parsed.path != _HEARTBEAT_PATH
        or parsed.query
        or parsed.fragment
    ):
        raise MatchbookHeartbeatSafetyError("heartbeat provider origin is not canonical")
    headers = {
        "Accept": "application/json",
        "session-token": token,
        "User-Agent": "Autosport/0.1 matchbook-heartbeat-safety",
    }
    if request_body is not None:
        headers["Content-Type"] = "application/json"
    return Request(
        _HEARTBEAT_URL,
        data=request_body,
        headers=headers,
        method=method,
    )


def _open_heartbeat_request(request: Request, *, timeout: float):
    parsed = urlsplit(request.full_url)
    if (
        parsed.scheme != "https"
        or parsed.netloc != _HEARTBEAT_HOST
        or parsed.path != _HEARTBEAT_PATH
        or parsed.query
        or parsed.fragment
    ):
        raise MatchbookHeartbeatTransportError(
            "heartbeat request origin is not canonical"
        )
    if request.get_method() not in {"POST", "DELETE"}:
        raise MatchbookHeartbeatTransportError(
            "heartbeat request method is not permitted"
        )
    return build_opener(_RejectRedirects()).open(request, timeout=timeout)


def _read_bounded_response(response: object) -> bytes:
    status = getattr(response, "status", None)
    if type(status) is not int or status != 200:
        raise MatchbookHeartbeatTransportError(
            "heartbeat provider did not return HTTP 200"
        )
    headers = getattr(response, "headers", None)
    content_type = None if headers is None else headers.get("Content-Type")
    if (
        not isinstance(content_type, str)
        or "application/json" not in content_type.lower()
    ):
        raise MatchbookHeartbeatTransportError(
            "heartbeat provider did not return application/json"
        )
    try:
        raw = response.read(_MAX_RESPONSE_BYTES + 1)
    except (OSError, TimeoutError):
        raise MatchbookHeartbeatTransportError(
            "heartbeat provider response could not be read"
        ) from None
    if not isinstance(raw, bytes):
        raise MatchbookHeartbeatTransportError(
            "heartbeat provider response must be bytes"
        )
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise MatchbookHeartbeatTransportError(
            "heartbeat provider response exceeded bounded size"
        )
    return raw


def _perform_heartbeat_request(
    *,
    method: str,
    session_token: str,
    request_body: bytes | None,
    timeout_seconds: float,
) -> bytes:
    timeout = _positive_timeout(timeout_seconds, "timeout_seconds")
    request = _heartbeat_request(
        method=method,
        session_token=session_token,
        request_body=request_body,
    )
    try:
        with _open_heartbeat_request(request, timeout=timeout) as response:
            return _read_bounded_response(response)
    except MatchbookHeartbeatTransportError:
        raise
    except HTTPError as exc:
        if exc.code in {401, 403}:
            message = "heartbeat provider authentication is unavailable"
        elif 500 <= exc.code <= 599:
            message = "heartbeat provider is unavailable"
        else:
            message = "heartbeat provider rejected the request"
        raise MatchbookHeartbeatTransportError(message) from None
    except (URLError, OSError, TimeoutError):
        raise MatchbookHeartbeatTransportError(
            "heartbeat provider transport failed"
        ) from None


def _parse_effective_timeout(raw: bytes) -> int:
    payload = _strict_json_loads(raw)
    if type(payload) is not dict:
        raise MatchbookHeartbeatTransportError(
            "heartbeat provider response must be a JSON object"
        )
    effective = payload.get("timeout")
    try:
        return _positive_int(effective, "provider effective timeout")
    except ValueError:
        raise MatchbookHeartbeatTransportError(
            "heartbeat provider response has invalid effective timeout"
        ) from None


@dataclass(frozen=True, slots=True, weakref_slot=True)
class MatchbookHeartbeatRegistration:
    """Durable non-secret evidence for one successful canonical heartbeat POST.

    The session token and process-local monotonic clock are deliberately absent.
    session_generation is a product-owned non-secret generation counter supplied by
    the authentication authority; it is not a token, token hash, or account id.
    Durable readback is audit evidence only and cannot recreate live provider-origin
    authority after restart.
    """

    session_generation: int
    requested_timeout_seconds: int
    effective_timeout_seconds: int
    registered_at: str
    request_payload_sha256: str
    provider_response_sha256: str
    predecessor_registration_id: str | None = None

    def __post_init__(self) -> None:
        _session_generation(self.session_generation)
        _positive_int(self.requested_timeout_seconds, "requested_timeout_seconds")
        _positive_int(self.effective_timeout_seconds, "effective_timeout_seconds")
        _canonical_utc(self.registered_at, "registered_at")
        _sha256(self.request_payload_sha256, "request_payload_sha256")
        _sha256(self.provider_response_sha256, "provider_response_sha256")
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
            "request_payload_sha256": self.request_payload_sha256,
            "provider_response_sha256": self.provider_response_sha256,
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
            "request_payload_sha256",
            "provider_response_sha256",
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
            request_payload_sha256=raw["request_payload_sha256"],
            provider_response_sha256=raw["provider_response_sha256"],
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
    provider_response_sha256: str

    def __post_init__(self) -> None:
        _sha256(self.registration_id, "registration_id")
        _session_generation(self.session_generation)
        _canonical_utc(self.unsubscribed_at, "unsubscribed_at")
        _sha256(self.provider_response_sha256, "provider_response_sha256")

    @property
    def offers_cancelled_proven(self) -> bool:
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
            "provider_response_sha256": self.provider_response_sha256,
            "offers_cancelled_proven": False,
        }

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any]
    ) -> "MatchbookHeartbeatUnsubscribeEvidence":
        if type(raw) is not dict:
            raise ValueError("heartbeat unsubscribe evidence must be a JSON object")
        required = {
            "schema_version",
            "provider",
            "registration_id",
            "session_generation",
            "unsubscribed_at",
            "provider_response_sha256",
            "offers_cancelled_proven",
        }
        if set(raw) != required:
            raise ValueError("heartbeat unsubscribe evidence fields mismatch")
        if (
            raw["schema_version"] != _UNSUBSCRIBE_SCHEMA_VERSION
            or type(raw["schema_version"]) is not int
        ):
            raise ValueError("heartbeat unsubscribe schema_version mismatch")
        if raw["provider"] != _PROVIDER or type(raw["provider"]) is not str:
            raise ValueError("heartbeat unsubscribe provider mismatch")
        if raw["offers_cancelled_proven"] is not False:
            raise ValueError("heartbeat unsubscribe cannot prove offer cancellation")
        return cls(
            registration_id=raw["registration_id"],
            session_generation=raw["session_generation"],
            unsubscribed_at=raw["unsubscribed_at"],
            provider_response_sha256=raw["provider_response_sha256"],
        )


@dataclass(frozen=True, slots=True)
class MatchbookHeartbeatCancellationObservation:
    """Durable typed copy of cancellation reason from canonical offer readback.

    This record does not own matched/unmatched exposure truth. The canonical offer
    readback remains authoritative for matched fragments and remaining exposure.
    """

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
    def heartbeat_expiry_observed(self) -> bool:
        return self.cancellation_reason is MatchbookCancellationReason.HEARTBEAT_EXPIRY

    @property
    def provider_origin_proven(self) -> bool:
        """Detached structural evidence never proves authenticated provider origin."""

        return False

    @property
    def offers_cancelled_proven(self) -> bool:
        """Cancellation labels alone never prove provider-side offer cancellation."""

        return False

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

    @classmethod
    def from_dict(
        cls, raw: Mapping[str, Any]
    ) -> "MatchbookHeartbeatCancellationObservation":
        if type(raw) is not dict:
            raise ValueError("heartbeat cancellation observation must be a JSON object")
        required = {
            "schema_version",
            "provider",
            "offer_id",
            "cancellation_reason",
            "observed_at",
            "provider_observation_sha256",
        }
        if set(raw) != required:
            raise ValueError("heartbeat cancellation observation fields mismatch")
        if (
            raw["schema_version"] != _CANCELLATION_SCHEMA_VERSION
            or type(raw["schema_version"]) is not int
        ):
            raise ValueError("heartbeat cancellation schema_version mismatch")
        if raw["provider"] != _PROVIDER or type(raw["provider"]) is not str:
            raise ValueError("heartbeat cancellation provider mismatch")
        try:
            reason = MatchbookCancellationReason(raw["cancellation_reason"])
        except (TypeError, ValueError):
            raise ValueError("heartbeat cancellation reason is unsupported") from None
        return cls(
            offer_id=raw["offer_id"],
            cancellation_reason=reason,
            observed_at=raw["observed_at"],
            provider_observation_sha256=raw["provider_observation_sha256"],
        )


@dataclass(frozen=True, slots=True, weakref_slot=True)
class MatchbookHeartbeatLease:
    """Ephemeral same-process provider-origin heartbeat authority."""

    registration: MatchbookHeartbeatRegistration
    registered_monotonic: float

    def __post_init__(self) -> None:
        if not isinstance(self.registration, MatchbookHeartbeatRegistration):
            raise ValueError("registration must be MatchbookHeartbeatRegistration")
        _monotonic(self.registered_monotonic, "registered_monotonic")

    @property
    def expires_monotonic(self) -> float:
        return self.registered_monotonic + self.registration.effective_timeout_seconds

    def state(
        self,
        *,
        now_monotonic: float,
        current_session_generation: int,
    ) -> MatchbookHeartbeatLeaseState:
        _assert_lease_authoritative(self)
        now = _monotonic(now_monotonic, "now_monotonic")
        current = _session_generation(current_session_generation)
        if current != self.registration.session_generation:
            return MatchbookHeartbeatLeaseState.SESSION_ROTATED
        override = _lease_override(self)
        if override is not None:
            return override
        if now < self.registered_monotonic:
            raise MatchbookHeartbeatSafetyError(
                "monotonic clock moved backwards within the heartbeat lease"
            )
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


_ISSUED_LEASES: dict[
    int,
    tuple[
        weakref.ReferenceType[MatchbookHeartbeatLease],
        str,
        MatchbookHeartbeatLeaseState | None,
    ],
] = {}


def _forget_lease(
    lease_id: int,
    reference: weakref.ReferenceType[MatchbookHeartbeatLease],
) -> None:
    current = _ISSUED_LEASES.get(lease_id)
    if current is not None and current[0] is reference:
        _ISSUED_LEASES.pop(lease_id, None)


def _issue_lease(lease: MatchbookHeartbeatLease) -> MatchbookHeartbeatLease:
    lease_id = id(lease)
    reference = weakref.ref(
        lease,
        lambda current, lease_id=lease_id: _forget_lease(lease_id, current),
    )
    _ISSUED_LEASES[lease_id] = (
        reference,
        lease.registration.registration_id,
        None,
    )
    return lease


def _assert_lease_authoritative(lease: MatchbookHeartbeatLease) -> None:
    if not isinstance(lease, MatchbookHeartbeatLease):
        raise MatchbookHeartbeatSafetyError(
            "heartbeat authority requires MatchbookHeartbeatLease"
        )
    issued = _ISSUED_LEASES.get(id(lease))
    if (
        issued is None
        or issued[0]() is not lease
        or issued[1] != lease.registration.registration_id
    ):
        raise MatchbookHeartbeatSafetyError(
            "heartbeat lease was not issued by canonical Matchbook heartbeat transport"
        )


def _lease_override(
    lease: MatchbookHeartbeatLease,
) -> MatchbookHeartbeatLeaseState | None:
    _assert_lease_authoritative(lease)
    return _ISSUED_LEASES[id(lease)][2]


def _set_lease_override(
    lease: MatchbookHeartbeatLease,
    state: MatchbookHeartbeatLeaseState,
) -> None:
    _assert_lease_authoritative(lease)
    reference, registration_id, _ = _ISSUED_LEASES[id(lease)]
    _ISSUED_LEASES[id(lease)] = (reference, registration_id, state)


def _post_heartbeat(
    *,
    session_token: str,
    requested_timeout_seconds: int,
    timeout_seconds: float,
) -> tuple[int, str, str]:
    requested = _positive_int(
        requested_timeout_seconds,
        "requested_timeout_seconds",
    )
    request_body = _canonical_json({"timeout": requested}).encode("utf-8")
    raw = _perform_heartbeat_request(
        method="POST",
        session_token=session_token,
        request_body=request_body,
        timeout_seconds=timeout_seconds,
    )
    return (
        _parse_effective_timeout(raw),
        _digest_bytes(request_body),
        _digest_bytes(raw),
    )


def capture_matchbook_heartbeat(
    *,
    session_token: str,
    session_generation: int,
    requested_timeout_seconds: int,
    timeout_seconds: float = 10.0,
) -> MatchbookHeartbeatLease:
    """Acquire one canonical heartbeat lease from Matchbook's fixed HTTPS endpoint."""
    generation = _session_generation(session_generation)
    effective, request_sha, response_sha = _post_heartbeat(
        session_token=session_token,
        requested_timeout_seconds=requested_timeout_seconds,
        timeout_seconds=timeout_seconds,
    )
    registration = MatchbookHeartbeatRegistration(
        session_generation=generation,
        requested_timeout_seconds=requested_timeout_seconds,
        effective_timeout_seconds=effective,
        registered_at=_utc_now(),
        request_payload_sha256=request_sha,
        provider_response_sha256=response_sha,
    )
    return _issue_lease(
        MatchbookHeartbeatLease(
            registration=registration,
            registered_monotonic=_monotonic(
                _monotonic_now(),
                "current monotonic clock",
            ),
        )
    )


def refresh_matchbook_heartbeat(
    lease: MatchbookHeartbeatLease,
    *,
    session_token: str,
    current_session_generation: int,
    requested_timeout_seconds: int,
    timeout_seconds: float = 10.0,
) -> MatchbookHeartbeatLease:
    """Refresh through canonical transport and supersede the exact prior lease."""
    _assert_lease_authoritative(lease)
    generation = _session_generation(current_session_generation)
    if generation != lease.registration.session_generation:
        raise MatchbookHeartbeatSafetyError(
            "heartbeat refresh belongs to a different session generation"
        )
    if _lease_override(lease) is not None:
        raise MatchbookHeartbeatSafetyError(
            "heartbeat lease is no longer refreshable"
        )
    try:
        effective, request_sha, response_sha = _post_heartbeat(
            session_token=session_token,
            requested_timeout_seconds=requested_timeout_seconds,
            timeout_seconds=timeout_seconds,
        )
    except MatchbookHeartbeatTransportError:
        _set_lease_override(
            lease,
            MatchbookHeartbeatLeaseState.DEGRADED_UNKNOWN,
        )
        raise

    now = _monotonic(_monotonic_now(), "current monotonic clock")
    if now < lease.registered_monotonic:
        _set_lease_override(
            lease,
            MatchbookHeartbeatLeaseState.DEGRADED_UNKNOWN,
        )
        raise MatchbookHeartbeatSafetyError(
            "monotonic clock moved backwards before heartbeat refresh"
        )
    registration = MatchbookHeartbeatRegistration(
        session_generation=generation,
        requested_timeout_seconds=requested_timeout_seconds,
        effective_timeout_seconds=effective,
        registered_at=_utc_now(),
        request_payload_sha256=request_sha,
        provider_response_sha256=response_sha,
        predecessor_registration_id=lease.registration.registration_id,
    )
    refreshed = _issue_lease(
        MatchbookHeartbeatLease(
            registration=registration,
            registered_monotonic=now,
        )
    )
    _set_lease_override(lease, MatchbookHeartbeatLeaseState.SUPERSEDED)
    return refreshed


def unsubscribe_matchbook_heartbeat(
    lease: MatchbookHeartbeatLease,
    *,
    session_token: str,
    current_session_generation: int,
    timeout_seconds: float = 10.0,
) -> MatchbookHeartbeatUnsubscribeEvidence:
    """Unsubscribe heartbeat service; never infer cancellation of any offer."""
    _assert_lease_authoritative(lease)
    generation = _session_generation(current_session_generation)
    if generation != lease.registration.session_generation:
        raise MatchbookHeartbeatSafetyError(
            "heartbeat unsubscribe belongs to a different session generation"
        )
    if _lease_override(lease) is not None:
        raise MatchbookHeartbeatSafetyError(
            "heartbeat lease is no longer unsubscribable"
        )
    try:
        raw = _perform_heartbeat_request(
            method="DELETE",
            session_token=session_token,
            request_body=None,
            timeout_seconds=timeout_seconds,
        )
    except MatchbookHeartbeatTransportError:
        _set_lease_override(
            lease,
            MatchbookHeartbeatLeaseState.DEGRADED_UNKNOWN,
        )
        raise
    _set_lease_override(lease, MatchbookHeartbeatLeaseState.UNSUBSCRIBED)
    return MatchbookHeartbeatUnsubscribeEvidence(
        registration_id=lease.registration.registration_id,
        session_generation=generation,
        unsubscribed_at=_utc_now(),
        provider_response_sha256=_digest_bytes(raw),
    )


__all__ = [
    "MatchbookCancellationReason",
    "MatchbookHeartbeatCancellationObservation",
    "MatchbookHeartbeatLease",
    "MatchbookHeartbeatLeaseState",
    "MatchbookHeartbeatRegistration",
    "MatchbookHeartbeatSafetyError",
    "MatchbookHeartbeatTransportError",
    "MatchbookHeartbeatUnsubscribeEvidence",
    "capture_matchbook_heartbeat",
    "refresh_matchbook_heartbeat",
    "unsubscribe_matchbook_heartbeat",
]

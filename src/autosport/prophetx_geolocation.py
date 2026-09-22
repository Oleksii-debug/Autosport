"""Fail-closed ProphetX pre-order geolocation evidence.

This module is sandbox-only. It proves one narrow provider precondition for one
frozen ExecutionAction. It does not place/cancel/replace orders, persist IP
addresses, create execution authority, or qualify production access.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from hashlib import sha256
from ipaddress import ip_address
import json
from typing import Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from weakref import ref

from .real_execution_ledger import ExecutionAction

SANDBOX_GEOLOCATION_ENDPOINT = "https://sandbox.prophetx.dev/ip-geolocation/api/v1/check-ip"
ADAPTER_ID = "prophetx-geolocation-sandbox"
ADAPTER_VERSION = "1"


class ProphetXGeolocationError(RuntimeError):
    """Raised when geolocation evidence cannot be accepted safely."""


class ProphetXGeolocationState(str, Enum):
    ALLOWED_PROVIDER_CONFIRMED = "allowed_provider_confirmed"
    DENIED_PROVIDER_CONFIRMED = "denied_provider_confirmed"
    UNKNOWN_TRANSPORT = "unknown_transport"
    UNKNOWN_CONTRACT = "unknown_contract"
    UNKNOWN_LOCAL_IP = "unknown_local_ip"
    UNKNOWN_UNVERIFIED_PROVIDER_ORIGIN = "unknown_unverified_provider_origin"
    EXPIRED = "expired"


def _iso(value: str, name: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise ProphetXGeolocationError(f"{name} must be canonical ISO-8601 text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProphetXGeolocationError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProphetXGeolocationError(f"{name} must be timezone-aware")
    return parsed


def _sha(value: str, name: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(c not in "0123456789abcdef" for c in value)
    ):
        raise ProphetXGeolocationError(f"{name} must be lowercase SHA-256 hex")
    return value


def _canonical_sha(value: object) -> str:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ProphetXGeolocationError("evidence is not canonical JSON") from exc
    return sha256(raw).hexdigest()


def _action_sha(action: ExecutionAction) -> str:
    if not isinstance(action, ExecutionAction):
        raise ProphetXGeolocationError("action must be canonical ExecutionAction")
    return _canonical_sha(action.to_dict())


def _json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ProphetXGeolocationError(f"duplicate provider JSON key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ProphetXGeolocationError(f"non-finite provider JSON number {value!r}")


@dataclass(frozen=True, slots=True)
class ProphetXGeolocationHttpResponse:
    endpoint: str
    status_code: int
    content_type: str | None
    content_encoding: str | None
    content_length: str | None
    body: bytes


class ProphetXGeolocationTransport(Protocol):
    def post(
        self,
        endpoint: str,
        *,
        body: bytes,
        timeout_seconds: float,
    ) -> ProphetXGeolocationHttpResponse: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


class UrllibProphetXGeolocationTransport:
    """Fixed-origin, proxy-free transport. Error bodies are never surfaced."""

    def __init__(self, *, max_response_bytes: int = 32 * 1024) -> None:
        if type(max_response_bytes) is not int or max_response_bytes <= 0:
            raise ValueError("max_response_bytes must be a positive integer")
        self._max_response_bytes = max_response_bytes

    def post(
        self,
        endpoint: str,
        *,
        body: bytes,
        timeout_seconds: float,
    ) -> ProphetXGeolocationHttpResponse:
        if endpoint != SANDBOX_GEOLOCATION_ENDPOINT:
            raise ProphetXGeolocationError("unqualified ProphetX geolocation origin")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not (0 < float(timeout_seconds) <= 30)
        ):
            raise ValueError("timeout_seconds must be in (0,30]")
        request = Request(
            endpoint,
            data=body,
            headers={"Accept": "application/json", "Content-Type": "application/json"},
            method="POST",
        )
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        try:
            with opener.open(request, timeout=float(timeout_seconds)) as response:
                payload = response.read(self._max_response_bytes + 1)
                status = int(response.getcode())
                content_type = response.headers.get("Content-Type")
                content_encoding = response.headers.get("Content-Encoding")
                content_length = response.headers.get("Content-Length")
        except HTTPError as exc:
            raise ProphetXGeolocationError(
                f"ProphetX geolocation HTTP status {exc.code}"
            ) from None
        except (URLError, TimeoutError, OSError):
            raise ProphetXGeolocationError(
                "ProphetX geolocation network request failed"
            ) from None
        if len(payload) > self._max_response_bytes:
            raise ProphetXGeolocationError(
                "ProphetX geolocation response exceeded size limit"
            )
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError as exc:
                raise ProphetXGeolocationError("invalid Content-Length") from exc
            if declared < 0 or declared != len(payload):
                raise ProphetXGeolocationError("Content-Length mismatch")
        return ProphetXGeolocationHttpResponse(
            endpoint,
            status,
            content_type,
            content_encoding,
            content_length,
            payload,
        )


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ProphetXGeolocationAdmission:
    adapter_id: str
    adapter_version: str
    environment: str
    action_id: str
    action_sha256: str
    request_id: str
    observed_at: str
    endpoint: str
    state: ProphetXGeolocationState
    source_payload_sha256: str | None

    def __post_init__(self) -> None:
        if (
            self.adapter_id != ADAPTER_ID
            or self.adapter_version != ADAPTER_VERSION
            or self.environment != "sandbox"
        ):
            raise ProphetXGeolocationError("geolocation adapter identity mismatch")
        if type(self.action_id) is not str or not self.action_id:
            raise ProphetXGeolocationError("action_id must be non-empty")
        _sha(self.action_sha256, "action_sha256")
        if type(self.request_id) is not str or not self.request_id:
            raise ProphetXGeolocationError("request_id must be non-empty")
        _iso(self.observed_at, "observed_at")
        if self.endpoint != SANDBOX_GEOLOCATION_ENDPOINT:
            raise ProphetXGeolocationError(
                "geolocation endpoint is not qualified sandbox origin"
            )
        if not isinstance(self.state, ProphetXGeolocationState):
            raise ProphetXGeolocationError("state must be ProphetXGeolocationState")
        if self.source_payload_sha256 is not None:
            _sha(self.source_payload_sha256, "source_payload_sha256")

    def to_durable_dict(self) -> dict[str, str | None]:
        """Minimum durable audit envelope; excludes IP/location/reason/body."""
        return {
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "environment": self.environment,
            "action_id": self.action_id,
            "action_sha256": self.action_sha256,
            "request_id": self.request_id,
            "observed_at": self.observed_at,
            "endpoint": self.endpoint,
            "state": self.state.value,
            "source_payload_sha256": self.source_payload_sha256,
        }


def _parse_provider_response(
    response: ProphetXGeolocationHttpResponse,
) -> tuple[bool, str]:
    if not isinstance(response, ProphetXGeolocationHttpResponse):
        raise ProphetXGeolocationError(
            "geolocation transport returned non-canonical response"
        )
    if (
        response.endpoint != SANDBOX_GEOLOCATION_ENDPOINT
        or response.status_code != 200
    ):
        raise ProphetXGeolocationError(
            "geolocation provider response is not qualified HTTP 200"
        )
    media_type = (response.content_type or "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise ProphetXGeolocationError(
            "geolocation response must be application/json"
        )
    encoding = (response.content_encoding or "identity").strip().lower()
    if encoding not in {"", "identity"}:
        raise ProphetXGeolocationError(
            "compressed geolocation response is unsupported"
        )
    if response.content_length is not None:
        try:
            if int(response.content_length) != len(response.body):
                raise ProphetXGeolocationError("Content-Length mismatch")
        except ValueError as exc:
            raise ProphetXGeolocationError("invalid Content-Length") from exc
    try:
        text_value = response.body.decode("utf-8", errors="strict")
        payload = json.loads(
            text_value,
            object_pairs_hook=_json_pairs,
            parse_constant=_reject_nonfinite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProphetXGeolocationError(
            "invalid geolocation JSON representation"
        ) from exc
    if not isinstance(payload, dict):
        raise ProphetXGeolocationError(
            "geolocation response root must be an object"
        )
    required = ("success", "country", "state", "city", "reason")
    if any(key not in payload for key in required):
        raise ProphetXGeolocationError(
            "geolocation response misses required provider fields"
        )
    if type(payload["success"]) is not bool:
        raise ProphetXGeolocationError("geolocation success must be boolean")
    if any(type(payload[key]) is not str for key in required[1:]):
        raise ProphetXGeolocationError(
            "geolocation location/reason fields must be strings"
        )
    return payload["success"], sha256(response.body).hexdigest()


def _install_authority():
    issued: dict[int, tuple[object, str]] = {}

    def issue(
        admission: ProphetXGeolocationAdmission,
    ) -> ProphetXGeolocationAdmission:
        key = id(admission)
        fingerprint = _canonical_sha(admission.to_durable_dict())
        issued[key] = (
            ref(admission, lambda _r, k=key: issued.pop(k, None)),
            fingerprint,
        )
        return admission

    def assert_authoritative(
        admission: ProphetXGeolocationAdmission,
        action: ExecutionAction,
    ) -> None:
        if not isinstance(admission, ProphetXGeolocationAdmission):
            raise ProphetXGeolocationError(
                "geolocation evidence type is not canonical"
            )
        record = issued.get(id(admission))
        if record is None or record[0]() is not admission:
            raise ProphetXGeolocationError(
                "geolocation evidence was not issued by canonical client"
            )
        if record[1] != _canonical_sha(admission.to_durable_dict()):
            raise ProphetXGeolocationError(
                "geolocation evidence changed after issuance"
            )
        if (
            admission.action_id != action.action_id
            or admission.action_sha256 != _action_sha(action)
        ):
            raise ProphetXGeolocationError(
                "geolocation evidence is bound to a different action"
            )

    return issue, assert_authoritative


(
    _issue_admission,
    assert_prophetx_geolocation_admission_authoritative,
) = _install_authority()


class ProphetXGeolocationClient:
    """Issue action-bound sandbox evidence; custom transports stay unverified."""

    def __init__(
        self,
        transport: ProphetXGeolocationTransport | None = None,
        *,
        timeout_seconds: float = 5.0,
        clock: Callable[[], str] | None = None,
    ) -> None:
        self._provider_origin_transport = transport is None
        self._transport = (
            UrllibProphetXGeolocationTransport()
            if transport is None
            else transport
        )
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or not (0 < float(timeout_seconds) <= 30)
        ):
            raise ValueError("timeout_seconds must be in (0,30]")
        self._timeout_seconds = float(timeout_seconds)
        self._clock = clock or (
            lambda: datetime.now(timezone.utc).isoformat(timespec="microseconds")
        )

    def check(
        self,
        action: ExecutionAction,
        current_ip: str | None,
    ) -> ProphetXGeolocationAdmission:
        if (
            not isinstance(action, ExecutionAction)
            or action.bookmaker_id.lower() != "prophetx"
        ):
            raise ProphetXGeolocationError(
                "geolocation check requires ProphetX ExecutionAction"
            )
        action_sha = _action_sha(action)
        observed_at = self._clock()
        observed = _iso(observed_at, "observed_at")
        request_id = _canonical_sha(
            {"action_sha256": action_sha, "observed_at": observed_at}
        )
        state = ProphetXGeolocationState.UNKNOWN_LOCAL_IP
        source_sha: str | None = None

        try:
            if (
                type(current_ip) is not str
                or current_ip != current_ip.strip()
                or not current_ip
            ):
                raise ValueError("missing current IP")
            normalized_ip = str(ip_address(current_ip))
        except ValueError:
            return _issue_admission(
                ProphetXGeolocationAdmission(
                    ADAPTER_ID,
                    ADAPTER_VERSION,
                    "sandbox",
                    action.action_id,
                    action_sha,
                    request_id,
                    observed_at,
                    SANDBOX_GEOLOCATION_ENDPOINT,
                    state,
                    None,
                )
            )

        body = json.dumps(
            {"ip_address": normalized_ip},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            response = self._transport.post(
                SANDBOX_GEOLOCATION_ENDPOINT,
                body=body,
                timeout_seconds=self._timeout_seconds,
            )
        except Exception:
            state = ProphetXGeolocationState.UNKNOWN_TRANSPORT
        else:
            try:
                allowed, source_sha = _parse_provider_response(response)
            except ProphetXGeolocationError:
                state = ProphetXGeolocationState.UNKNOWN_CONTRACT
            else:
                if not self._provider_origin_transport:
                    state = (
                        ProphetXGeolocationState.UNKNOWN_UNVERIFIED_PROVIDER_ORIGIN
                    )
                else:
                    state = (
                        ProphetXGeolocationState.ALLOWED_PROVIDER_CONFIRMED
                        if allowed
                        else ProphetXGeolocationState.DENIED_PROVIDER_CONFIRMED
                    )

        if observed >= _iso(action.expires_at, "action expires_at"):
            state = ProphetXGeolocationState.EXPIRED
        return _issue_admission(
            ProphetXGeolocationAdmission(
                ADAPTER_ID,
                ADAPTER_VERSION,
                "sandbox",
                action.action_id,
                action_sha,
                request_id,
                observed_at,
                SANDBOX_GEOLOCATION_ENDPOINT,
                state,
                source_sha,
            )
        )

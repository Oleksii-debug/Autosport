"""Read-only fixed-origin acquisition evidence for the ParlayAPI sport catalog.

This module owns only the HTTP acquisition/origin boundary for ``GET /v1/sports``.
It does not parse sport semantics, admit a sport into the product, authorize a
provider write, or authorize real-money execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PARLAY_SPORTS_URL = "https://parlay-api.com/v1/sports"
_DEFAULT_TIMEOUT_SECONDS = 10.0
_DEFAULT_MAX_RESPONSE_BYTES = 1_048_576
_SCHEMA_VERSION = 1
_CANONICAL_ORIGIN_SEAL = object()


class ParlaySportCatalogAcquisitionError(ValueError):
    """Raised when acquisition evidence is malformed, ambiguous, or unsafe."""


class ParlaySportCatalogTransportError(RuntimeError):
    """Raised when the canonical read-only HTTP acquisition cannot complete."""


@dataclass(frozen=True, slots=True)
class ParlaySportCatalogHttpResponse:
    status_code: int
    headers: Mapping[str, str]
    final_url: str
    body: bytes


CatalogTransport = Callable[
    [str, Mapping[str, str], float, int],
    ParlaySportCatalogHttpResponse,
]


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _timestamp(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ParlaySportCatalogAcquisitionError(f"{field} must be a non-empty trimmed string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ParlaySportCatalogAcquisitionError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ParlaySportCatalogAcquisitionError(f"{field} must include a timezone offset")
    return value




def _timestamp_datetime(value: object, field: str) -> datetime:
    text = _timestamp(value, field)
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _sha256_hex(value: object, field: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise ParlaySportCatalogAcquisitionError(
            f"{field} must be lowercase 64-character SHA-256 hex"
        )
    return value


def _exact_url(value: object, field: str) -> str:
    if type(value) is not str or value != PARLAY_SPORTS_URL:
        raise ParlaySportCatalogAcquisitionError(
            f"{field} must be exact canonical ParlayAPI /v1/sports URL"
        )
    return value


def _etag(value: object, field: str = "ETag") -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ParlaySportCatalogAcquisitionError(f"{field} must be a non-empty trimmed string")
    if len(value) > 1024:
        raise ParlaySportCatalogAcquisitionError(f"{field} is too long")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ParlaySportCatalogAcquisitionError(f"{field} must not contain control characters")
    return value


def _header(headers: Mapping[str, str], name: str) -> str | None:
    if not isinstance(headers, Mapping):
        raise ParlaySportCatalogAcquisitionError("response headers must be a mapping")
    target = name.lower()
    found: list[str] = []
    for key, value in headers.items():
        if type(key) is not str or type(value) is not str:
            raise ParlaySportCatalogAcquisitionError("response header names and values must be strings")
        if key.lower() == target:
            found.append(value)
    if len(found) > 1:
        raise ParlaySportCatalogAcquisitionError(f"response contains duplicate {name} headers")
    if not found:
        return None
    return _etag(found[0], name)


def _positive_finite_float(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ParlaySportCatalogAcquisitionError(f"{field} must be a positive finite number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ParlaySportCatalogAcquisitionError(f"{field} must be a positive finite number")
    return result


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ParlaySportCatalogAcquisitionError(f"{field} must be a positive integer")
    return value


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True, init=False)
class ParlaySportCatalogAcquisition:
    acquired_at: str
    requested_url: str
    final_url: str
    http_status: int
    etag: str | None
    payload_sha256: str
    payload_bytes: bytes | None
    previous_acquisition_id: str | None
    payload_origin_acquisition_id: str | None
    provider_origin_verified: bool
    schema_version: int = _SCHEMA_VERSION
    provider_write_authorized: bool = False
    product_sport_admission_authorized: bool = False
    real_money_execution_authorized: bool = False

    def __init__(
        self,
        *,
        acquired_at: str,
        requested_url: str,
        final_url: str,
        http_status: int,
        etag: str | None,
        payload_sha256: str,
        payload_bytes: bytes | None,
        previous_acquisition_id: str | None,
        payload_origin_acquisition_id: str | None,
        provider_origin_verified: bool = False,
        schema_version: int = _SCHEMA_VERSION,
        provider_write_authorized: bool = False,
        product_sport_admission_authorized: bool = False,
        real_money_execution_authorized: bool = False,
        _origin_seal: object | None = None,
    ) -> None:
        if provider_origin_verified is True and _origin_seal is not _CANONICAL_ORIGIN_SEAL:
            raise ParlaySportCatalogAcquisitionError(
                "provider_origin_verified can be issued only by the canonical acquisition path"
            )
        values = {
            "acquired_at": acquired_at,
            "requested_url": requested_url,
            "final_url": final_url,
            "http_status": http_status,
            "etag": etag,
            "payload_sha256": payload_sha256,
            "payload_bytes": payload_bytes,
            "previous_acquisition_id": previous_acquisition_id,
            "payload_origin_acquisition_id": payload_origin_acquisition_id,
            "provider_origin_verified": provider_origin_verified,
            "schema_version": schema_version,
            "provider_write_authorized": provider_write_authorized,
            "product_sport_admission_authorized": product_sport_admission_authorized,
            "real_money_execution_authorized": real_money_execution_authorized,
        }
        for name, value in values.items():
            object.__setattr__(self, name, value)
        self.__post_init__()

    def __post_init__(self) -> None:
        _timestamp(self.acquired_at, "acquired_at")
        _exact_url(self.requested_url, "requested_url")
        _exact_url(self.final_url, "final_url")
        if type(self.http_status) is not int or self.http_status not in {200, 304}:
            raise ParlaySportCatalogAcquisitionError("http_status must be exactly 200 or 304")
        if self.etag is not None:
            _etag(self.etag)
        _sha256_hex(self.payload_sha256, "payload_sha256")
        if type(self.provider_origin_verified) is not bool:
            raise ParlaySportCatalogAcquisitionError("provider_origin_verified must be an exact bool")
        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            raise ParlaySportCatalogAcquisitionError("schema_version must be exactly 1")
        if self.provider_write_authorized is not False:
            raise ParlaySportCatalogAcquisitionError("catalog acquisition cannot authorize provider writes")
        if self.product_sport_admission_authorized is not False:
            raise ParlaySportCatalogAcquisitionError("catalog acquisition cannot admit a sport")
        if self.real_money_execution_authorized is not False:
            raise ParlaySportCatalogAcquisitionError(
                "catalog acquisition cannot authorize real-money execution"
            )

        if self.http_status == 200:
            if type(self.payload_bytes) is not bytes or not self.payload_bytes:
                raise ParlaySportCatalogAcquisitionError("HTTP 200 acquisition requires non-empty exact bytes")
            if sha256(self.payload_bytes).hexdigest() != self.payload_sha256:
                raise ParlaySportCatalogAcquisitionError("payload_sha256 does not match payload_bytes")
            if self.previous_acquisition_id is not None:
                raise ParlaySportCatalogAcquisitionError(
                    "HTTP 200 acquisition must not carry previous_acquisition_id"
                )
            if self.payload_origin_acquisition_id is not None:
                raise ParlaySportCatalogAcquisitionError(
                    "HTTP 200 acquisition must not carry payload_origin_acquisition_id"
                )
        else:
            if self.payload_bytes is not None:
                raise ParlaySportCatalogAcquisitionError(
                    "HTTP 304 acquisition must not fabricate payload bytes"
                )
            _sha256_hex(self.previous_acquisition_id, "previous_acquisition_id")
            _sha256_hex(self.payload_origin_acquisition_id, "payload_origin_acquisition_id")

    @property
    def acquisition_id(self) -> str:
        payload = {
            "acquired_at": self.acquired_at,
            "etag": self.etag,
            "final_url": self.final_url,
            "http_status": self.http_status,
            "payload_origin_acquisition_id": self.payload_origin_acquisition_id,
            "payload_sha256": self.payload_sha256,
            "previous_acquisition_id": self.previous_acquisition_id,
            "provider_origin_verified": self.provider_origin_verified,
            "requested_url": self.requested_url,
            "schema_version": self.schema_version,
        }
        return sha256(_canonical_json_bytes(payload)).hexdigest()

    @property
    def has_new_payload(self) -> bool:
        return self.http_status == 200

    @property
    def payload_root_acquisition_id(self) -> str:
        if self.http_status == 200:
            return self.acquisition_id
        assert self.payload_origin_acquisition_id is not None
        return self.payload_origin_acquisition_id


def _content_length(headers: Mapping[str, str]) -> int | None:
    if not isinstance(headers, Mapping):
        raise ParlaySportCatalogAcquisitionError("response headers must be a mapping")
    value = None
    for key, candidate in headers.items():
        if type(key) is not str or type(candidate) is not str:
            raise ParlaySportCatalogAcquisitionError("response header names and values must be strings")
        if key.lower() == "content-length":
            if value is not None:
                raise ParlaySportCatalogAcquisitionError("response contains duplicate Content-Length headers")
            value = candidate
    if value is None:
        return None
    if not value.isdigit():
        raise ParlaySportCatalogAcquisitionError("Content-Length must be a non-negative integer")
    return int(value)


def _read_bounded_response(response: object, max_response_bytes: int) -> bytes:
    headers = dict(getattr(response, "headers", {}).items())
    declared = _content_length(headers)
    if declared is not None and declared > max_response_bytes:
        raise ParlaySportCatalogAcquisitionError("provider response exceeds maximum response size")
    raw = response.read(max_response_bytes + 1)
    if type(raw) is not bytes:
        raise ParlaySportCatalogAcquisitionError("provider response body must be exact bytes")
    if len(raw) > max_response_bytes:
        raise ParlaySportCatalogAcquisitionError("provider response exceeds maximum response size")
    return raw


def _canonical_transport(
    url: str,
    headers: Mapping[str, str],
    timeout_seconds: float,
    max_response_bytes: int,
) -> ParlaySportCatalogHttpResponse:
    _exact_url(url, "request URL")
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with urlopen(request, timeout=timeout_seconds) as response:  # nosec B310 - fixed HTTPS URL
            response_headers = dict(response.headers.items())
            body = _read_bounded_response(response, max_response_bytes)
            return ParlaySportCatalogHttpResponse(
                status_code=int(response.status),
                headers=response_headers,
                final_url=str(response.geturl()),
                body=body,
            )
    except HTTPError as exc:
        if int(exc.code) == 304:
            response_headers = dict(exc.headers.items()) if exc.headers else {}
            body = _read_bounded_response(exc, max_response_bytes)
            return ParlaySportCatalogHttpResponse(
                status_code=304,
                headers=response_headers,
                final_url=str(exc.geturl()),
                body=body,
            )
        raise ParlaySportCatalogTransportError(f"provider HTTP {exc.code}") from exc
    except URLError as exc:
        raise ParlaySportCatalogTransportError(f"provider transport error: {exc.reason}") from exc
    except TimeoutError as exc:
        raise ParlaySportCatalogTransportError("provider transport timed out") from exc


def acquire_parlay_sport_catalog(
    *,
    previous: ParlaySportCatalogAcquisition | None = None,
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
    transport: CatalogTransport | None = None,
) -> ParlaySportCatalogAcquisition:
    """Acquire exact catalog bytes or a conditional 304 from the fixed provider origin.

    Supplying ``transport`` is a test/composition seam. Evidence returned through
    that seam is always origin-unverified, even when the supplied response claims
    the canonical URL. Positive origin verification is reserved for the internal
    fixed-origin transport path.
    """

    timeout = _positive_finite_float(timeout_seconds, "timeout_seconds")
    maximum = _positive_int(max_response_bytes, "max_response_bytes")
    if previous is not None and type(previous) is not ParlaySportCatalogAcquisition:
        raise ParlaySportCatalogAcquisitionError(
            "previous must be ParlaySportCatalogAcquisition or None"
        )

    headers = {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "User-Agent": "Autosport/0.1 read-only-sport-catalog",
    }
    if previous is not None and previous.etag is not None:
        headers["If-None-Match"] = previous.etag

    canonical_path = transport is None
    response = (transport or _canonical_transport)(
        PARLAY_SPORTS_URL,
        headers,
        timeout,
        maximum,
    )
    if type(response) is not ParlaySportCatalogHttpResponse:
        raise ParlaySportCatalogAcquisitionError(
            "transport must return ParlaySportCatalogHttpResponse"
        )
    if type(response.status_code) is not int:
        raise ParlaySportCatalogAcquisitionError("response status_code must be an exact int")
    _exact_url(response.final_url, "response final_url")
    if type(response.body) is not bytes:
        raise ParlaySportCatalogAcquisitionError("response body must be exact bytes")
    if len(response.body) > maximum:
        raise ParlaySportCatalogAcquisitionError("provider response exceeds maximum response size")
    response_etag = _header(response.headers, "ETag")
    acquired_at = _utc_now_iso()
    _timestamp(acquired_at, "acquired_at")
    if previous is not None and _timestamp_datetime(
        acquired_at, "acquired_at"
    ) < _timestamp_datetime(previous.acquired_at, "previous.acquired_at"):
        raise ParlaySportCatalogAcquisitionError(
            "acquisition clock regressed behind the prior acquisition"
        )

    if response.status_code == 200:
        if not response.body:
            raise ParlaySportCatalogAcquisitionError("HTTP 200 catalog response must not be empty")
        return ParlaySportCatalogAcquisition(
            acquired_at=acquired_at,
            requested_url=PARLAY_SPORTS_URL,
            final_url=response.final_url,
            http_status=200,
            etag=response_etag,
            payload_sha256=sha256(response.body).hexdigest(),
            payload_bytes=response.body,
            previous_acquisition_id=None,
            payload_origin_acquisition_id=None,
            provider_origin_verified=canonical_path,
            _origin_seal=_CANONICAL_ORIGIN_SEAL if canonical_path else None,
        )

    if response.status_code != 304:
        raise ParlaySportCatalogAcquisitionError(
            f"catalog acquisition requires HTTP 200 or 304, got {response.status_code}"
        )
    if response.body:
        raise ParlaySportCatalogAcquisitionError(
            "HTTP 304 catalog response must not contain payload bytes"
        )
    if previous is None or previous.etag is None:
        raise ParlaySportCatalogAcquisitionError(
            "HTTP 304 requires an exact prior acquisition with ETag"
        )
    if response_etag is not None and response_etag != previous.etag:
        raise ParlaySportCatalogAcquisitionError(
            "HTTP 304 ETag contradicts the conditional prior acquisition"
        )
    effective_etag = response_etag or previous.etag
    return ParlaySportCatalogAcquisition(
        acquired_at=acquired_at,
        requested_url=PARLAY_SPORTS_URL,
        final_url=response.final_url,
        http_status=304,
        etag=effective_etag,
        payload_sha256=previous.payload_sha256,
        payload_bytes=None,
        previous_acquisition_id=previous.acquisition_id,
        payload_origin_acquisition_id=previous.payload_root_acquisition_id,
        provider_origin_verified=canonical_path and previous.provider_origin_verified,
        _origin_seal=(
            _CANONICAL_ORIGIN_SEAL
            if canonical_path and previous.provider_origin_verified
            else None
        ),
    )

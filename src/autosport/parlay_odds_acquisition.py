from __future__ import annotations

import hashlib
import json
import math
import re
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode


CANONICAL_PARLAY_ORIGIN = "https://parlay-api.com"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_USER_AGENT = "Autosport/0.1 read-only-odds-acquisition"
_KEY_RE = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)*$")
_CAPABILITY_HEADER_NAMES = frozenset(
    {
        "x-api-release-date",
        "x-api-version",
        "x-markets-served",
        "x-markets-served-elsewhere",
        "x-markets-unservable",
    }
)


class ParlayOddsAcquisitionError(RuntimeError):
    """Base error for fixed-origin Parlay odds acquisition."""


class ParlayOddsTransportError(ParlayOddsAcquisitionError):
    """Network/redirect failure before trustworthy response evidence exists."""


class ParlayOddsEvidenceError(ParlayOddsAcquisitionError, ValueError):
    """Provider response/request evidence is malformed or authority-ambiguous."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Reject redirects before urllib can emit a credential-bearing second hop."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        del req, fp, code, msg, headers, newurl
        return None


@dataclass(frozen=True, slots=True)
class RawOddsHttpResponse:
    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    final_url: str


@dataclass(frozen=True, slots=True)
class ParlayOddsRequestScope:
    sport_key: str
    regions: tuple[str, ...]
    markets: tuple[str, ...]
    odds_format: str = "decimal"

    def __post_init__(self) -> None:
        object.__setattr__(self, "sport_key", _canonical_key("sport_key", self.sport_key))
        object.__setattr__(self, "regions", _canonical_key_tuple("regions", self.regions))
        object.__setattr__(self, "markets", _canonical_key_tuple("markets", self.markets))
        if self.odds_format != "decimal":
            raise ParlayOddsEvidenceError("odds_format is fixed to decimal")

    @property
    def request_url(self) -> str:
        query = urlencode(
            (
                ("regions", ",".join(self.regions)),
                ("markets", ",".join(self.markets)),
                ("oddsFormat", self.odds_format),
            ),
            safe=",",
        )
        return f"{CANONICAL_PARLAY_ORIGIN}/v1/sports/{self.sport_key}/odds?{query}"

    @property
    def request_sha256(self) -> str:
        payload = {
            "schema": "autosport.parlay_odds_request_scope",
            "schema_version": 1,
            "origin": CANONICAL_PARLAY_ORIGIN,
            "sport_key": self.sport_key,
            "regions": list(self.regions),
            "markets": list(self.markets),
            "odds_format": self.odds_format,
        }
        return hashlib.sha256(_canonical_json(payload)).hexdigest()


@dataclass(frozen=True, slots=True)
class ParlayOddsAcquisition:
    scope: ParlayOddsRequestScope
    acquired_at: str
    status_code: int
    final_url: str
    response_sha256: str
    capability_headers: tuple[tuple[str, str], ...]
    acquisition_id: str
    raw_body: bytes = field(repr=False)
    provider_origin_verified: bool = field(default=False, init=False)

    @property
    def request_sha256(self) -> str:
        return self.scope.request_sha256


Transport = Callable[[str, Mapping[str, str], float, int], RawOddsHttpResponse]
Clock = Callable[[], str]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _canonical_key(field_name: str, value: object) -> str:
    if type(value) is not str or not _KEY_RE.fullmatch(value):
        raise ParlayOddsEvidenceError(
            f"{field_name} must be a canonical lowercase underscore key"
        )
    return value


def _canonical_key_tuple(field_name: str, value: object) -> tuple[str, ...]:
    if type(value) is not tuple or not value:
        raise ParlayOddsEvidenceError(f"{field_name} must be a non-empty canonical tuple")
    normalized = tuple(_canonical_key(field_name, item) for item in value)
    if len(normalized) != len(set(normalized)):
        raise ParlayOddsEvidenceError(f"{field_name} must not contain duplicates")
    if normalized != tuple(sorted(normalized)):
        raise ParlayOddsEvidenceError(f"{field_name} must be canonically sorted")
    return normalized


def _api_key(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ParlayOddsEvidenceError("api_key must be non-empty trimmed text")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ParlayOddsEvidenceError("api_key must not contain control characters")
    try:
        encoded = value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ParlayOddsEvidenceError("api_key must be valid UTF-8") from exc
    if len(encoded) > 1024:
        raise ParlayOddsEvidenceError("api_key exceeds bounded size")
    return value


def _finite_positive_float(value: object, *, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field_name} must be a finite positive number")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0:
        raise ValueError(f"{field_name} must be a finite positive number")
    return numeric


def _positive_int(value: object, *, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive non-boolean integer")
    return value


def _canonical_timestamp(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ParlayOddsEvidenceError("acquired_at must be non-empty trimmed ISO-8601")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ParlayOddsEvidenceError("acquired_at must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ParlayOddsEvidenceError("acquired_at must be timezone-aware")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _bounded_read(stream: object, max_response_bytes: int) -> bytes:
    raw = stream.read(max_response_bytes + 1)
    if type(raw) is not bytes:
        raise ParlayOddsEvidenceError("provider response body must be exact bytes")
    if len(raw) > max_response_bytes:
        raise ParlayOddsEvidenceError("Parlay odds response exceeds bounded size")
    return raw


def _default_transport(
    url: str,
    headers: Mapping[str, str],
    timeout_seconds: float,
    max_response_bytes: int,
) -> RawOddsHttpResponse:
    if not url.startswith(f"{CANONICAL_PARLAY_ORIGIN}/v1/sports/"):
        raise ParlayOddsEvidenceError("odds transport received a non-canonical origin")
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    opener = urllib.request.build_opener(_RejectRedirects())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:  # nosec B310 - fixed HTTPS origin
            return RawOddsHttpResponse(
                status_code=int(response.status),
                headers=tuple((str(key), str(value)) for key, value in response.headers.items()),
                body=_bounded_read(response, max_response_bytes),
                final_url=str(response.geturl()),
            )
    except HTTPError as exc:
        if 300 <= int(exc.code) < 400:
            raise ParlayOddsTransportError(
                f"Parlay odds redirect blocked before credential-bearing second hop: HTTP {exc.code}"
            ) from exc
        return RawOddsHttpResponse(
            status_code=int(exc.code),
            headers=tuple(
                (str(key), str(value))
                for key, value in (exc.headers.items() if exc.headers is not None else ())
            ),
            body=_bounded_read(exc, max_response_bytes),
            final_url=str(exc.geturl()),
        )
    except URLError as exc:
        raise ParlayOddsTransportError("Parlay odds transport failed") from exc


def _validate_raw_response(
    response: object,
    *,
    expected_url: str,
    max_response_bytes: int,
) -> RawOddsHttpResponse:
    if type(response) is not RawOddsHttpResponse:
        raise ParlayOddsEvidenceError("transport must return exact RawOddsHttpResponse")
    if type(response.status_code) is not int or not (100 <= response.status_code <= 599):
        raise ParlayOddsEvidenceError("response status_code must be an exact HTTP integer")
    if response.final_url != expected_url:
        raise ParlayOddsEvidenceError(
            "Parlay odds response final URL does not match canonical request scope"
        )
    if type(response.body) is not bytes:
        raise ParlayOddsEvidenceError("response body must be exact bytes")
    if len(response.body) > max_response_bytes:
        raise ParlayOddsEvidenceError("Parlay odds response exceeds bounded size")
    if type(response.headers) is not tuple or any(
        type(item) is not tuple
        or len(item) != 2
        or type(item[0]) is not str
        or type(item[1]) is not str
        for item in response.headers
    ):
        raise ParlayOddsEvidenceError("response headers must preserve exact text pairs")
    return response


def _capability_headers(
    headers: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, str], ...]:
    selected: dict[str, str] = {}
    for raw_name, raw_value in headers:
        name = raw_name.lower()
        if name not in _CAPABILITY_HEADER_NAMES:
            continue
        if name in selected:
            raise ParlayOddsEvidenceError(
                f"response contains duplicate capability header {name}"
            )
        if not raw_value or raw_value != raw_value.strip():
            raise ParlayOddsEvidenceError(
                f"capability header {name} must be non-empty trimmed text"
            )
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in raw_value):
            raise ParlayOddsEvidenceError(
                f"capability header {name} must not contain control characters"
            )
        selected[name] = raw_value
    return tuple(sorted(selected.items()))


def _acquisition_id(
    *,
    scope: ParlayOddsRequestScope,
    acquired_at: str,
    status_code: int,
    final_url: str,
    response_sha256: str,
    capability_headers: tuple[tuple[str, str], ...],
    provider_origin_verified: bool,
) -> str:
    payload = {
        "schema": "autosport.parlay_odds_acquisition",
        "schema_version": 1,
        "request_sha256": scope.request_sha256,
        "acquired_at": acquired_at,
        "status_code": status_code,
        "final_url": final_url,
        "response_sha256": response_sha256,
        "capability_headers": [list(item) for item in capability_headers],
        "provider_origin_verified": provider_origin_verified,
    }
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def acquire_parlay_odds(
    scope: ParlayOddsRequestScope,
    *,
    api_key: str,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    transport: Transport | None = None,
    clock: Clock = _utc_now_iso,
) -> ParlayOddsAcquisition:
    """Acquire exact Parlay `/odds` response evidence without granting action authority.

    The API key is always sent in `X-API-Key`; it is never placed in the URL,
    request/acquisition identity, or returned evidence. Caller-injected transports or
    clocks are deterministic test/simulation seams and cannot mint provider-origin
    authority. This layer preserves capability headers verbatim but deliberately does
    not interpret them into market support/routing semantics.
    """

    if type(scope) is not ParlayOddsRequestScope:
        raise TypeError("scope must be exact ParlayOddsRequestScope")
    secret = _api_key(api_key)
    timeout_seconds = _finite_positive_float(timeout_seconds, field_name="timeout_seconds")
    max_response_bytes = _positive_int(max_response_bytes, field_name="max_response_bytes")
    if timeout_seconds > DEFAULT_TIMEOUT_SECONDS:
        raise ValueError(
            f"timeout_seconds must not exceed product maximum {DEFAULT_TIMEOUT_SECONDS}"
        )
    if max_response_bytes > DEFAULT_MAX_RESPONSE_BYTES:
        raise ValueError(
            "max_response_bytes must not exceed product maximum "
            f"{DEFAULT_MAX_RESPONSE_BYTES}"
        )
    if not callable(clock):
        raise TypeError("clock must be callable")
    if transport is not None and not callable(transport):
        raise TypeError("transport must be callable or None")

    request_url = scope.request_url
    headers = {
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
        "X-API-Key": secret,
    }
    using_product_transport = transport is None
    using_product_clock = clock is _utc_now_iso
    active_transport = _default_transport if transport is None else transport
    response = _validate_raw_response(
        active_transport(
            request_url,
            headers,
            timeout_seconds,
            max_response_bytes,
        ),
        expected_url=request_url,
        max_response_bytes=max_response_bytes,
    )
    acquired_at = _canonical_timestamp(clock())
    capability_headers = _capability_headers(response.headers)

    secret_bytes = secret.encode("utf-8")
    if secret in response.final_url or secret_bytes in response.body or any(
        secret in value for _, value in capability_headers
    ):
        raise ParlayOddsEvidenceError(
            "provider response echoed API credential into durable acquisition evidence"
        )

    response_sha256 = hashlib.sha256(response.body).hexdigest()
    provider_origin_verified = using_product_transport and using_product_clock
    acquisition_id = _acquisition_id(
        scope=scope,
        acquired_at=acquired_at,
        status_code=response.status_code,
        final_url=response.final_url,
        response_sha256=response_sha256,
        capability_headers=capability_headers,
        provider_origin_verified=provider_origin_verified,
    )
    result = ParlayOddsAcquisition(
        scope=scope,
        acquired_at=acquired_at,
        status_code=response.status_code,
        final_url=response.final_url,
        response_sha256=response_sha256,
        capability_headers=capability_headers,
        acquisition_id=acquisition_id,
        raw_body=response.body,
    )
    if provider_origin_verified:
        object.__setattr__(result, "provider_origin_verified", True)
    return result

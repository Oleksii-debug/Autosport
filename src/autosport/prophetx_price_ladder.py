from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Final
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


ADAPTER_ID: Final = "prophetx-price-ladder-readonly-v1"
_SANDBOX_URL: Final = "https://api.sandbox.prophetx.dev/partner/v4/mm/get_price_ladder"
_DEFAULT_TIMEOUT_SECONDS: Final = 10.0
_DEFAULT_MAX_RESPONSE_BYTES: Final = 256 * 1024
_REDIRECT_CODES: Final = frozenset({301, 302, 303, 307, 308})


class ProphetXPriceLadderError(ValueError):
    """Price-ladder evidence is malformed, incomplete, or unsafe to consume."""


class ProphetXPriceLadderTransportError(RuntimeError):
    """The fixed-origin read-only provider request could not be completed safely."""


@dataclass(frozen=True, slots=True)
class ProphetXPriceLadderHttpResponse:
    status_code: int
    final_url: str
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class ProphetXPriceLadderSnapshot:
    """Immutable membership evidence from one complete ladder acquisition.

    This snapshot is deliberately not execution authority. It proves only the
    exact ladder membership observed in one bounded read. Strike identity,
    account limits, exposure headroom, order freshness, and execution permission
    remain separate authorities.
    """

    adapter_id: str
    environment: str
    endpoint: str
    prices: tuple[int, ...]
    observed_at: str
    source_payload_sha256: str
    ladder_sha256: str
    provider_origin_verified: bool

    @property
    def current_execution_price_authority(self) -> bool:
        return False

    def contains(self, price: int | str | Decimal) -> bool:
        parsed = _exact_american_price(price, field="price")
        return parsed in self.prices


Transport = Callable[[str, Mapping[str, str], float, int], ProphetXPriceLadderHttpResponse]
Clock = Callable[[], datetime]


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        del req, fp, msg, headers, newurl
        if code in _REDIRECT_CODES:
            return None
        return None


def _default_transport(
    url: str,
    headers: Mapping[str, str],
    timeout_seconds: float,
    max_response_bytes: int,
) -> ProphetXPriceLadderHttpResponse:
    _assert_fixed_sandbox_url(url)
    request = Request(url, headers=dict(headers), method="GET")
    opener = build_opener(ProxyHandler({}), _RejectRedirects())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:
            status = int(response.status)
            response_headers = dict(response.headers.items())
            final_url = str(response.geturl())
            if final_url != _SANDBOX_URL:
                raise ProphetXPriceLadderTransportError(
                    "ProphetX price-ladder response origin changed unexpectedly"
                )
            _validate_response_metadata(
                status_code=status,
                headers=response_headers,
                max_response_bytes=max_response_bytes,
            )
            body = response.read(max_response_bytes + 1)
    except HTTPError as exc:
        raise ProphetXPriceLadderTransportError(
            f"ProphetX price-ladder HTTP {int(exc.code)}"
        ) from None
    except (URLError, TimeoutError, OSError, HTTPException):
        raise ProphetXPriceLadderTransportError(
            "ProphetX price-ladder transport failed"
        ) from None

    if len(body) > max_response_bytes:
        raise ProphetXPriceLadderTransportError(
            "ProphetX price-ladder response exceeds configured size limit"
        )
    return ProphetXPriceLadderHttpResponse(status, final_url, response_headers, body)


class ProphetXPriceLadderClient:
    """Sandbox-only authenticated, read-only ProphetX price-ladder client."""

    def __init__(
        self,
        access_token: str,
        *,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = _DEFAULT_MAX_RESPONSE_BYTES,
        transport: Transport | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._access_token = _secret_text(access_token, "access_token")
        self._timeout_seconds = _positive_finite_float(timeout_seconds, "timeout_seconds")
        self._max_response_bytes = _positive_int(max_response_bytes, "max_response_bytes")
        self._transport = transport or _default_transport
        self._provider_origin_verified = transport is None
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(adapter_id={ADAPTER_ID!r}, "
            "environment='SANDBOX')"
        )

    def read_ladder(self) -> ProphetXPriceLadderSnapshot:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Authorization": f"Bearer {self._access_token}",
            "User-Agent": "Autosport/0.1 ProphetX-price-ladder-readonly",
        }
        try:
            response = self._transport(
                _SANDBOX_URL,
                headers,
                self._timeout_seconds,
                self._max_response_bytes,
            )
        except ProphetXPriceLadderTransportError:
            raise
        except Exception:
            raise ProphetXPriceLadderTransportError(
                "ProphetX price-ladder transport failed"
            ) from None

        if not isinstance(response, ProphetXPriceLadderHttpResponse):
            raise ProphetXPriceLadderTransportError(
                "ProphetX price-ladder transport returned an invalid response"
            )
        if response.final_url != _SANDBOX_URL:
            raise ProphetXPriceLadderTransportError(
                "ProphetX price-ladder response origin changed unexpectedly"
            )
        _validate_response_metadata(
            status_code=response.status_code,
            headers=response.headers,
            max_response_bytes=self._max_response_bytes,
        )
        if not isinstance(response.body, bytes):
            raise ProphetXPriceLadderTransportError(
                "ProphetX price-ladder response body must be bytes"
            )
        if len(response.body) > self._max_response_bytes:
            raise ProphetXPriceLadderTransportError(
                "ProphetX price-ladder response exceeds configured size limit"
            )
        declared_length = _content_length(response.headers)
        if declared_length is not None and declared_length != len(response.body):
            raise ProphetXPriceLadderTransportError(
                "ProphetX price-ladder Content-Length does not match body"
            )

        payload = _decode_json(response.body)
        prices = _parse_ladder(payload)
        observed_at = _clock_timestamp(self._clock())
        source_payload_sha256 = hashlib.sha256(response.body).hexdigest()
        canonical_membership = json.dumps(
            {
                "adapter_id": ADAPTER_ID,
                "environment": "SANDBOX",
                "endpoint": _SANDBOX_URL,
                "prices": sorted(prices),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        ladder_sha256 = hashlib.sha256(canonical_membership).hexdigest()
        return ProphetXPriceLadderSnapshot(
            adapter_id=ADAPTER_ID,
            environment="SANDBOX",
            endpoint=_SANDBOX_URL,
            prices=prices,
            observed_at=observed_at,
            source_payload_sha256=source_payload_sha256,
            ladder_sha256=ladder_sha256,
            provider_origin_verified=self._provider_origin_verified,
        )


def _assert_fixed_sandbox_url(url: object) -> None:
    if not isinstance(url, str) or url != _SANDBOX_URL:
        raise ProphetXPriceLadderTransportError(
            "ProphetX price-ladder request must use the fixed sandbox endpoint"
        )
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.sandbox.prophetx.dev"
        or parsed.port is not None
        or parsed.path != "/partner/v4/mm/get_price_ladder"
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ProphetXPriceLadderTransportError(
            "ProphetX price-ladder request must use the fixed sandbox endpoint"
        )


def _validate_response_metadata(
    *,
    status_code: object,
    headers: Mapping[str, str],
    max_response_bytes: int,
) -> None:
    if type(status_code) is not int or status_code != 200:
        rendered = status_code if type(status_code) is int else "invalid"
        raise ProphetXPriceLadderTransportError(
            f"ProphetX price-ladder HTTP {rendered}"
        )
    if not isinstance(headers, Mapping):
        raise ProphetXPriceLadderTransportError(
            "ProphetX price-ladder response headers are invalid"
        )
    lowered: dict[str, str] = {}
    for key, value in headers.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ProphetXPriceLadderTransportError(
                "ProphetX price-ladder response headers are invalid"
            )
        lowered[key.lower()] = value.strip()

    content_type = lowered.get("content-type")
    if content_type is None:
        raise ProphetXPriceLadderTransportError(
            "ProphetX price-ladder response is missing Content-Type"
        )
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise ProphetXPriceLadderTransportError(
            "ProphetX price-ladder response is not JSON"
        )
    content_encoding = lowered.get("content-encoding")
    if content_encoding and content_encoding.lower() != "identity":
        raise ProphetXPriceLadderTransportError(
            "ProphetX price-ladder response encoding is unsupported"
        )
    declared = _content_length(headers)
    if declared is not None and declared > max_response_bytes:
        raise ProphetXPriceLadderTransportError(
            "ProphetX price-ladder response exceeds configured size limit"
        )


def _content_length(headers: Mapping[str, str]) -> int | None:
    values = [
        value.strip()
        for key, value in headers.items()
        if isinstance(key, str) and key.lower() == "content-length"
    ]
    if not values:
        return None
    if len(values) != 1:
        raise ProphetXPriceLadderTransportError(
            "ProphetX price-ladder Content-Length is ambiguous"
        )
    raw = values[0]
    if not raw or not raw.isascii() or not raw.isdigit():
        raise ProphetXPriceLadderTransportError(
            "ProphetX price-ladder Content-Length is invalid"
        )
    return int(raw)


def _decode_json(raw: bytes) -> object:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ProphetXPriceLadderError(
            "ProphetX price-ladder response is not valid UTF-8 JSON"
        ) from None

    def pairs_hook(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ProphetXPriceLadderError(
                    f"ProphetX price-ladder response contains duplicate key {key!r}"
                )
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ProphetXPriceLadderError(
            f"ProphetX price-ladder response contains non-finite number {value!r}"
        )

    try:
        return json.loads(
            text,
            object_pairs_hook=pairs_hook,
            parse_float=Decimal,
            parse_int=int,
            parse_constant=reject_constant,
        )
    except ProphetXPriceLadderError:
        raise
    except (json.JSONDecodeError, InvalidOperation):
        raise ProphetXPriceLadderError(
            "ProphetX price-ladder response is invalid JSON"
        ) from None


def _parse_ladder(payload: object) -> tuple[int, ...]:
    if not isinstance(payload, dict):
        raise ProphetXPriceLadderError(
            "ProphetX price-ladder response must be an object"
        )
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        raise ProphetXPriceLadderError(
            "ProphetX price-ladder response requires non-empty data[]"
        )

    prices = tuple(
        _exact_american_price(value, field=f"data[{index}]")
        for index, value in enumerate(data)
    )
    if len(set(prices)) != len(prices):
        raise ProphetXPriceLadderError(
            "ProphetX price-ladder response contains duplicate prices"
        )
    return prices


def _exact_american_price(value: object, *, field: str) -> int:
    if isinstance(value, bool) or isinstance(value, float):
        raise ProphetXPriceLadderError(f"{field} must be an exact integer American price")
    if type(value) is int:
        parsed = value
    elif isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            raise ProphetXPriceLadderError(
                f"{field} must be an exact integer American price"
            )
        parsed = int(value)
    elif isinstance(value, str):
        if not value or value != value.strip():
            raise ProphetXPriceLadderError(
                f"{field} must be an exact integer American price"
            )
        try:
            decimal = Decimal(value)
        except InvalidOperation:
            raise ProphetXPriceLadderError(
                f"{field} must be an exact integer American price"
            ) from None
        if not decimal.is_finite() or decimal != decimal.to_integral_value():
            raise ProphetXPriceLadderError(
                f"{field} must be an exact integer American price"
            )
        parsed = int(decimal)
    else:
        raise ProphetXPriceLadderError(f"{field} must be an exact integer American price")
    if parsed == 0 or abs(parsed) < 100:
        raise ProphetXPriceLadderError(
            f"{field} absolute American price must be at least 100"
        )
    return parsed


def _secret_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{field} must be a non-empty trimmed string")
    if any(character in value for character in ("\x00", "\r", "\n")):
        raise ValueError(f"{field} contains forbidden control characters")
    return value


def _positive_finite_float(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite positive number")
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0:
        raise ValueError(f"{field} must be a finite positive number")
    return numeric


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field} must be a positive integer")
    return value


def _clock_timestamp(value: object) -> str:
    if not isinstance(value, datetime):
        raise ProphetXPriceLadderError("clock must return datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProphetXPriceLadderError("clock must return timezone-aware datetime")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "ADAPTER_ID",
    "ProphetXPriceLadderClient",
    "ProphetXPriceLadderError",
    "ProphetXPriceLadderHttpResponse",
    "ProphetXPriceLadderSnapshot",
    "ProphetXPriceLadderTransportError",
]

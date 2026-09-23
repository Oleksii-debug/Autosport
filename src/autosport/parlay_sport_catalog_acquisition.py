from __future__ import annotations

import hashlib
import json
import math
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError


CANONICAL_PARLAY_SPORTS_URL = "https://parlay-api.com/v1/sports"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_RESPONSE_BYTES = 1_048_576
_USER_AGENT = "Autosport/0.1 read-only-sport-catalog"


class ParlaySportCatalogAcquisitionError(RuntimeError):
    """Base error for the read-only sport-catalog acquisition boundary."""


class ParlaySportCatalogTransportError(ParlaySportCatalogAcquisitionError):
    """Network/HTTP error before a trustworthy catalog response exists."""


class ParlaySportCatalogEvidenceError(ParlaySportCatalogAcquisitionError, ValueError):
    """Response evidence is structurally inconsistent or unsafe to trust."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    """Fail before urllib can issue a second request for any redirect response."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ParlaySportCatalogTransportError(
            f"Parlay sport-catalog redirect blocked before second hop: HTTP {code}"
        )


@dataclass(frozen=True, slots=True)
class RawCatalogHttpResponse:
    status_code: int
    headers: tuple[tuple[str, str], ...]
    body: bytes
    final_url: str


@dataclass(frozen=True, slots=True)
class ParlaySportCatalogAcquisition:
    acquired_at: str
    status_code: int
    final_url: str
    etag: str | None
    raw_body: bytes | None
    raw_body_sha256: str | None
    prior_acquisition_id: str | None
    acquisition_id: str
    provider_origin_verified: bool = field(default=False, init=False)

    @property
    def is_not_modified(self) -> bool:
        return self.status_code == 304


Transport = Callable[[str, Mapping[str, str], float, int], RawCatalogHttpResponse]
Clock = Callable[[], str]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _timestamp_instant(value: object) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ParlaySportCatalogEvidenceError("acquired_at must be a non-empty trimmed string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ParlaySportCatalogEvidenceError("acquired_at must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ParlaySportCatalogEvidenceError("acquired_at must be timezone-aware")
    return parsed


def _validate_timestamp(value: object) -> str:
    _timestamp_instant(value)
    return value


def _single_header(headers: tuple[tuple[str, str], ...], name: str) -> str | None:
    matches = [value for key, value in headers if key.lower() == name.lower()]
    if len(matches) > 1:
        raise ParlaySportCatalogEvidenceError(f"response contains duplicate {name} headers")
    if not matches:
        return None
    value = matches[0]
    if not isinstance(value, str):
        raise ParlaySportCatalogEvidenceError(f"{name} header must be text")
    if not value or value != value.strip():
        raise ParlaySportCatalogEvidenceError(f"{name} header must be non-empty trimmed text")
    return value


def _validate_raw_response(response: object, *, max_response_bytes: int) -> RawCatalogHttpResponse:
    if not isinstance(response, RawCatalogHttpResponse):
        raise ParlaySportCatalogEvidenceError("transport must return RawCatalogHttpResponse")
    if isinstance(response.status_code, bool) or not isinstance(response.status_code, int):
        raise ParlaySportCatalogEvidenceError("response status_code must be an integer")
    if response.status_code not in (200, 304):
        raise ParlaySportCatalogTransportError(
            f"unexpected Parlay sport-catalog HTTP status {response.status_code}"
        )
    if response.final_url != CANONICAL_PARLAY_SPORTS_URL:
        raise ParlaySportCatalogEvidenceError(
            "Parlay sport-catalog response final URL does not match the canonical origin/path"
        )
    if not isinstance(response.body, bytes):
        raise ParlaySportCatalogEvidenceError("response body must be exact bytes")
    if len(response.body) > max_response_bytes:
        raise ParlaySportCatalogEvidenceError("Parlay sport-catalog response exceeds bounded size")
    if not isinstance(response.headers, tuple) or any(
        not isinstance(item, tuple)
        or len(item) != 2
        or not isinstance(item[0], str)
        or not isinstance(item[1], str)
        for item in response.headers
    ):
        raise ParlaySportCatalogEvidenceError("response headers must preserve text header pairs")
    return response


def _bounded_read(stream: object, max_response_bytes: int) -> bytes:
    raw = stream.read(max_response_bytes + 1)
    if not isinstance(raw, bytes):
        raise ParlaySportCatalogEvidenceError("provider response body is not bytes")
    if len(raw) > max_response_bytes:
        raise ParlaySportCatalogEvidenceError("Parlay sport-catalog response exceeds bounded size")
    return raw


def _make_product_redirect_handler_factory() -> type[urllib.request.HTTPRedirectHandler]:
    """Freeze redirect refusal independently from the mutable module class binding."""

    class ProductRejectRedirects(urllib.request.HTTPRedirectHandler):
        redirect_request = _RejectRedirects.redirect_request

    return ProductRejectRedirects


def _perform_catalog_http_response(
    url: str,
    headers: Mapping[str, str],
    timeout_seconds: float,
    max_response_bytes: int,
    *,
    request_factory,
    opener_factory,
    redirect_handler_factory,
    bounded_reader,
    response_factory,
    evidence_error_type,
    transport_error_type,
    http_error_type,
    url_error_type,
    canonical_url: str,
) -> RawCatalogHttpResponse:
    """Perform one fixed-origin bounded read through explicit dependencies.

    This helper carries no provider-origin authority by itself. Positive origin is
    assigned only by the public acquirer when it uses the product transport closure
    created at module initialization.
    """

    if url != canonical_url:
        raise evidence_error_type("catalog transport received a non-canonical URL")
    request = request_factory(url, headers=dict(headers), method="GET")
    opener = opener_factory(redirect_handler_factory())
    try:
        with opener.open(request, timeout=timeout_seconds) as response:  # nosec B310 - fixed HTTPS URL
            return response_factory(
                status_code=int(response.status),
                headers=tuple(
                    (str(key), str(value)) for key, value in response.headers.items()
                ),
                body=bounded_reader(response, max_response_bytes),
                final_url=str(response.geturl()),
            )
    except http_error_type as exc:
        if exc.code == 304:
            return response_factory(
                status_code=304,
                headers=tuple(
                    (str(key), str(value))
                    for key, value in (
                        exc.headers.items() if exc.headers is not None else ()
                    )
                ),
                body=bounded_reader(exc, max_response_bytes),
                final_url=str(exc.geturl()),
            )
        raise transport_error_type(
            f"Parlay sport-catalog HTTP {exc.code}"
        ) from exc
    except url_error_type as exc:
        raise transport_error_type(
            f"Parlay sport-catalog transport error: {exc.reason}"
        ) from exc


def _build_product_transport(
    *,
    performer,
    request_factory,
    opener_factory,
    redirect_handler_factory,
    bounded_reader,
    response_factory,
    evidence_error_type,
    transport_error_type,
    http_error_type,
    url_error_type,
    canonical_url: str,
) -> Transport:
    """Capture the product network authority outside mutable module dispatch."""

    def product_transport(
        url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> RawCatalogHttpResponse:
        return performer(
            url,
            headers,
            timeout_seconds,
            max_response_bytes,
            request_factory=request_factory,
            opener_factory=opener_factory,
            redirect_handler_factory=redirect_handler_factory,
            bounded_reader=bounded_reader,
            response_factory=response_factory,
            evidence_error_type=evidence_error_type,
            transport_error_type=transport_error_type,
            http_error_type=http_error_type,
            url_error_type=url_error_type,
            canonical_url=canonical_url,
        )

    return product_transport


_PRODUCT_REDIRECT_HANDLER = _make_product_redirect_handler_factory()
_default_transport = _build_product_transport(
    performer=_perform_catalog_http_response,
    request_factory=urllib.request.Request,
    opener_factory=urllib.request.build_opener,
    redirect_handler_factory=_PRODUCT_REDIRECT_HANDLER,
    bounded_reader=_bounded_read,
    response_factory=RawCatalogHttpResponse,
    evidence_error_type=ParlaySportCatalogEvidenceError,
    transport_error_type=ParlaySportCatalogTransportError,
    http_error_type=HTTPError,
    url_error_type=URLError,
    canonical_url=CANONICAL_PARLAY_SPORTS_URL,
)


def _acquisition_id(
    *,
    acquired_at: str,
    status_code: int,
    final_url: str,
    etag: str | None,
    raw_body_sha256: str | None,
    prior_acquisition_id: str | None,
    provider_origin_verified: bool,
) -> str:
    payload = {
        "schema_version": 1,
        "acquired_at": acquired_at,
        "status_code": status_code,
        "final_url": final_url,
        "etag": etag,
        "raw_body_sha256": raw_body_sha256,
        "prior_acquisition_id": prior_acquisition_id,
        "provider_origin_verified": provider_origin_verified,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "parlay-sports-acquisition:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _validate_prior_acquisition(prior: ParlaySportCatalogAcquisition) -> str | None:
    if prior.status_code != 200 or prior.raw_body is None or prior.raw_body_sha256 is None:
        raise ParlaySportCatalogEvidenceError(
            "conditional acquisition requires an exact prior HTTP 200 body acquisition"
        )
    if prior.final_url != CANONICAL_PARLAY_SPORTS_URL:
        raise ParlaySportCatalogEvidenceError("prior acquisition final URL is not canonical")
    _validate_timestamp(prior.acquired_at)
    if prior.prior_acquisition_id is not None:
        raise ParlaySportCatalogEvidenceError(
            "prior HTTP 200 acquisition must not reference another acquisition"
        )
    if hashlib.sha256(prior.raw_body).hexdigest() != prior.raw_body_sha256:
        raise ParlaySportCatalogEvidenceError("prior acquisition raw-body digest mismatch")
    if prior.etag is not None:
        if not isinstance(prior.etag, str) or not prior.etag or prior.etag != prior.etag.strip():
            raise ParlaySportCatalogEvidenceError(
                "prior acquisition ETag must be non-empty trimmed text"
            )
    expected_id = _acquisition_id(
        acquired_at=prior.acquired_at,
        status_code=200,
        final_url=prior.final_url,
        etag=prior.etag,
        raw_body_sha256=prior.raw_body_sha256,
        prior_acquisition_id=None,
        provider_origin_verified=prior.provider_origin_verified,
    )
    if prior.acquisition_id != expected_id:
        raise ParlaySportCatalogEvidenceError("prior acquisition identity mismatch")
    return prior.etag


def _acquire_parlay_sport_catalog_impl(
    *,
    prior: ParlaySportCatalogAcquisition | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    transport: Transport | None = None,
    clock: Clock,
    product_transport: Transport,
    product_clock: Clock,
    finite_positive_float=_finite_positive_float,
    positive_int=_positive_int,
    prior_validator=_validate_prior_acquisition,
    raw_response_validator=_validate_raw_response,
    timestamp_validator=_validate_timestamp,
    timestamp_parser=_timestamp_instant,
    header_reader=_single_header,
    digest_factory=hashlib.sha256,
    acquisition_id_builder=_acquisition_id,
    acquisition_type=ParlaySportCatalogAcquisition,
    evidence_error_type=ParlaySportCatalogEvidenceError,
    canonical_url: str = CANONICAL_PARLAY_SPORTS_URL,
    user_agent: str = _USER_AGENT,
    timeout_limit: float = DEFAULT_TIMEOUT_SECONDS,
    response_size_limit: int = DEFAULT_MAX_RESPONSE_BYTES,
) -> ParlaySportCatalogAcquisition:
    """Acquire exact `/v1/sports` bytes without granting odds/write/product authority.

    Positive provider-origin evidence is issued only when both the product-owned
    default transport and product-owned clock are used. Injected transport/clock
    seams remain useful for deterministic tests and simulations but can never set
    ``provider_origin_verified``. A 304 conditional response also remains
    origin-unverified until prior HTTP-200 bytes can be re-resolved from a
    product-owned durable acquisition authority rather than a caller-supplied object.
    """

    timeout_seconds = finite_positive_float(
        timeout_seconds,
        field_name="timeout_seconds",
    )
    max_response_bytes = positive_int(
        max_response_bytes,
        field_name="max_response_bytes",
    )
    if timeout_seconds > timeout_limit:
        raise ValueError(
            f"timeout_seconds must not exceed product maximum {timeout_limit}"
        )
    if max_response_bytes > response_size_limit:
        raise ValueError(
            "max_response_bytes must not exceed product maximum "
            f"{response_size_limit}"
        )
    if prior is not None and not isinstance(prior, acquisition_type):
        raise TypeError("prior must be a ParlaySportCatalogAcquisition")

    headers: dict[str, str] = {
        "Accept": "application/json",
        "User-Agent": user_agent,
    }
    conditional_etag: str | None = None
    if prior is not None:
        conditional_etag = prior_validator(prior)
        if conditional_etag is not None:
            headers["If-None-Match"] = conditional_etag

    using_product_transport = transport is None
    using_product_clock = clock is product_clock
    active_transport = product_transport if transport is None else transport
    response = raw_response_validator(
        active_transport(
            canonical_url,
            headers,
            timeout_seconds,
            max_response_bytes,
        ),
        max_response_bytes=max_response_bytes,
    )
    acquired_at = timestamp_validator(clock())
    if prior is not None and timestamp_parser(acquired_at) < timestamp_parser(
        prior.acquired_at
    ):
        raise evidence_error_type(
            "acquired_at cannot precede the exact prior acquisition"
        )
    etag = header_reader(response.headers, "ETag")

    if response.status_code == 304:
        if prior is None or conditional_etag is None:
            raise evidence_error_type(
                "HTTP 304 requires an exact prior acquisition and If-None-Match witness"
            )
        if response.body:
            raise evidence_error_type("HTTP 304 must not carry a catalog body")
        # A caller-supplied prior object is only structurally self-consistent evidence.
        # Until a product-owned durable authority can re-resolve its exact HTTP-200
        # bytes by identity, a genuine provider 304 must not transfer positive origin
        # authority from that object.
        provider_origin_verified = False
        acquisition_id = acquisition_id_builder(
            acquired_at=acquired_at,
            status_code=304,
            final_url=response.final_url,
            etag=etag,
            raw_body_sha256=None,
            prior_acquisition_id=prior.acquisition_id,
            provider_origin_verified=provider_origin_verified,
        )
        result = acquisition_type(
            acquired_at=acquired_at,
            status_code=304,
            final_url=response.final_url,
            etag=etag,
            raw_body=None,
            raw_body_sha256=None,
            prior_acquisition_id=prior.acquisition_id,
            acquisition_id=acquisition_id,
        )
    else:
        digest = digest_factory(response.body).hexdigest()
        provider_origin_verified = using_product_transport and using_product_clock
        acquisition_id = acquisition_id_builder(
            acquired_at=acquired_at,
            status_code=200,
            final_url=response.final_url,
            etag=etag,
            raw_body_sha256=digest,
            prior_acquisition_id=None,
            provider_origin_verified=provider_origin_verified,
        )
        result = acquisition_type(
            acquired_at=acquired_at,
            status_code=200,
            final_url=response.final_url,
            etag=etag,
            raw_body=response.body,
            raw_body_sha256=digest,
            prior_acquisition_id=None,
            acquisition_id=acquisition_id,
        )

    if provider_origin_verified:
        object.__setattr__(result, "provider_origin_verified", True)
    return result


def _build_product_acquirer(
    *,
    product_transport: Transport,
    product_clock: Clock,
    implementation,
):
    """Install the public API with product authority captured in closure cells."""

    def acquire_parlay_sport_catalog(
        *,
        prior: ParlaySportCatalogAcquisition | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        transport: Transport | None = None,
        clock: Clock = product_clock,
    ) -> ParlaySportCatalogAcquisition:
        return implementation(
            prior=prior,
            timeout_seconds=timeout_seconds,
            max_response_bytes=max_response_bytes,
            transport=transport,
            clock=clock,
            product_transport=product_transport,
            product_clock=product_clock,
        )

    return acquire_parlay_sport_catalog


acquire_parlay_sport_catalog = _build_product_acquirer(
    product_transport=_default_transport,
    product_clock=_utc_now_iso,
    implementation=_acquire_parlay_sport_catalog_impl,
)

# These installer/implementation names are not dispatch seams. The public acquirer
# retains exact objects in closure cells; rebinding module symbols cannot replace
# the transport or clock used to mint positive origin evidence.
del _build_product_acquirer
del _acquire_parlay_sport_catalog_impl

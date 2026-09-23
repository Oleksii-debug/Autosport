"""Live, read-only BETDAQ GetOddsLadder acquisition and price-admission evidence.

This module composes the strict network-free parser with the already integrated BETDAQ
HTTPS transport.  It does not place/update/cancel orders and it deliberately refuses to
turn injected/test transport observations into live placement-price authority.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import IntEnum
from hashlib import sha256
import json
from secrets import token_hex
from time import monotonic_ns
from typing import Final
import xml.etree.ElementTree as ET

from .betdaq_account_readonly import (
    BetdaqCredentials,
    BetdaqSoapTransport,
    UrllibBetdaqSoapTransport,
)
from .betdaq_odds_ladder_wire import (
    BetdaqOddsLadderWireResponse,
    parse_get_odds_ladder_response,
)


BETDAQ_PROVIDER_ID: Final[str] = "BETDAQ"
READONLY_ENDPOINT: Final[str] = "https://api.betdaq.com/v2.0/ReadOnlyService.asmx"
_EXTERNAL_API_NS: Final[str] = "http://www.GlobalBettingExchange.com/ExternalAPI/"
_SOAP11_NS: Final[str] = "http://schemas.xmlsoap.org/soap/envelope/"
_SOAP_ACTION: Final[str] = f"{_EXTERNAL_API_NS}GetOddsLadder"
_CANONICAL_POST = UrllibBetdaqSoapTransport.post
_LIVE_AUTHORITY_TOKEN = object()


class BetdaqOddsLadderAcquisitionError(RuntimeError):
    """A live ladder request, transport, or evidence binding failed."""


class BetdaqPriceFormat(IntEnum):
    DECIMAL = 1
    FRACTIONAL = 2
    AMERICAN = 3


@dataclass(frozen=True, slots=True)
class BetdaqOddsLadderObservation:
    provider_id: str
    endpoint: str
    price_format: BetdaqPriceFormat
    request_identity_sha256: str
    response_sha256: str
    response_bytes: int
    content_sha256: str
    acquisition_generation: str
    observation_id: str
    received_at: str
    received_monotonic_ns: int
    provider_created_at: str | None
    entries: tuple[Decimal, ...]
    transport_origin: str
    grants_write_permission: bool = False
    _authority_token: object = field(default=None, repr=False, compare=False)

    @property
    def live_provider_origin_verified(self) -> bool:
        return (
            self.transport_origin == "CANONICAL_HTTPS"
            and self._authority_token is _LIVE_AUTHORITY_TOKEN
        )

    def admits_price(
        self,
        price: Decimal,
        *,
        max_age_seconds: Decimal,
        now_monotonic_ns: int | None = None,
    ) -> bool:
        """Return current exact membership authority; never round a requested price."""
        if not self.live_provider_origin_verified:
            return False
        if not isinstance(price, Decimal) or not price.is_finite() or price <= 1:
            return False
        if (
            not isinstance(max_age_seconds, Decimal)
            or not max_age_seconds.is_finite()
            or max_age_seconds <= 0
        ):
            return False
        current = monotonic_ns() if now_monotonic_ns is None else now_monotonic_ns
        if not isinstance(current, int) or current < self.received_monotonic_ns:
            return False
        age_ns = current - self.received_monotonic_ns
        max_age_ns = int(max_age_seconds * Decimal(1_000_000_000))
        if age_ns > max_age_ns:
            return False
        return price in self.entries

    def to_canonical_dict(self) -> dict[str, object]:
        """Serialize audit evidence without serializing live-origin authority.

        A restart may reconstruct audit truth from this material, but must reacquire a
        current ladder before positive execution-price admission.  Process-local origin
        authority is intentionally not durable.
        """
        return {
            "provider_id": self.provider_id,
            "endpoint": self.endpoint,
            "price_format": int(self.price_format),
            "request_identity_sha256": self.request_identity_sha256,
            "response_sha256": self.response_sha256,
            "response_bytes": self.response_bytes,
            "content_sha256": self.content_sha256,
            "acquisition_generation": self.acquisition_generation,
            "observation_id": self.observation_id,
            "received_at": self.received_at,
            "provider_created_at": self.provider_created_at,
            "entries": [str(item) for item in self.entries],
            "transport_origin": self.transport_origin,
            "live_provider_origin_verified": False,
            "grants_write_permission": False,
        }


def _canonical_transport(transport: object) -> bool:
    if type(transport) is not UrllibBetdaqSoapTransport:
        return False
    bound = getattr(transport, "post", None)
    return (
        getattr(bound, "__self__", None) is transport
        and getattr(bound, "__func__", None) is _CANONICAL_POST
    )


def _request_identity(price_format: BetdaqPriceFormat) -> str:
    payload = json.dumps(
        {
            "provider": BETDAQ_PROVIDER_ID,
            "endpoint": READONLY_ENDPOINT,
            "method": "GetOddsLadder",
            "price_format": int(price_format),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _request_body(credentials: BetdaqCredentials, price_format: BetdaqPriceFormat) -> bytes:
    if type(credentials) is not BetdaqCredentials:
        raise BetdaqOddsLadderAcquisitionError(
            "credentials must be canonical BetdaqCredentials"
        )
    envelope = ET.Element(f"{{{_SOAP11_NS}}}Envelope")
    header = ET.SubElement(envelope, f"{{{_SOAP11_NS}}}Header")
    ET.SubElement(
        header,
        f"{{{_EXTERNAL_API_NS}}}ExternalApiHeader",
        {
            "version": credentials.version,
            "languageCode": credentials.language_code,
            "username": credentials.username,
            "password": credentials.password,
            "applicationIdentifier": credentials.application_identifier,
        },
    )
    body = ET.SubElement(envelope, f"{{{_SOAP11_NS}}}Body")
    call = ET.SubElement(body, f"{{{_EXTERNAL_API_NS}}}GetOddsLadder")
    ET.SubElement(call, f"{{{_EXTERNAL_API_NS}}}priceFormat").text = str(
        int(price_format)
    )
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


def _observation_id(
    *,
    request_identity_sha256: str,
    response_sha256: str,
    content_sha256: str,
    acquisition_generation: str,
    transport_origin: str,
) -> str:
    material = json.dumps(
        {
            "schema": "autosport.betdaq-odds-ladder-observation-v1",
            "request": request_identity_sha256,
            "response": response_sha256,
            "content": content_sha256,
            "generation": acquisition_generation,
            "origin": transport_origin,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "betdaq-ladder:" + sha256(material).hexdigest()


def _bind_observation(
    parsed: BetdaqOddsLadderWireResponse,
    *,
    price_format: BetdaqPriceFormat,
    raw_response: bytes,
    canonical_origin: bool,
) -> BetdaqOddsLadderObservation:
    # Availability time is captured only after the full response has been validated.
    received_mono = monotonic_ns()
    received = datetime.now(timezone.utc).isoformat()
    generation = token_hex(16)
    request_id = _request_identity(price_format)
    response_id = sha256(raw_response).hexdigest()
    origin = "CANONICAL_HTTPS" if canonical_origin else "INJECTED_TEST"
    observation_id = _observation_id(
        request_identity_sha256=request_id,
        response_sha256=response_id,
        content_sha256=parsed.content_sha256,
        acquisition_generation=generation,
        transport_origin=origin,
    )
    provider_created_at = (
        None
        if parsed.provider_created_at is None
        else parsed.provider_created_at.isoformat()
    )
    return BetdaqOddsLadderObservation(
        provider_id=BETDAQ_PROVIDER_ID,
        endpoint=READONLY_ENDPOINT,
        price_format=price_format,
        request_identity_sha256=request_id,
        response_sha256=response_id,
        response_bytes=len(raw_response),
        content_sha256=parsed.content_sha256,
        acquisition_generation=generation,
        observation_id=observation_id,
        received_at=received,
        received_monotonic_ns=received_mono,
        provider_created_at=provider_created_at,
        entries=tuple(item.price for item in parsed.entries),
        transport_origin=origin,
        grants_write_permission=False,
        _authority_token=_LIVE_AUTHORITY_TOKEN if canonical_origin else None,
    )


def acquire_odds_ladder(
    credentials: BetdaqCredentials,
    *,
    price_format: BetdaqPriceFormat = BetdaqPriceFormat.DECIMAL,
    transport: BetdaqSoapTransport | None = None,
    timeout_seconds: float = 10.0,
) -> BetdaqOddsLadderObservation:
    """Acquire and strictly bind one current BETDAQ odds-ladder observation.

    Injected transports are intentionally useful for deterministic tests but produce
    observations that can never pass ``admits_price``.  Only the exact product-owned
    HTTPS transport can mint process-local live-origin authority.
    """
    if type(price_format) is not BetdaqPriceFormat:
        raise BetdaqOddsLadderAcquisitionError("price_format must be BetdaqPriceFormat")
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise BetdaqOddsLadderAcquisitionError("timeout_seconds must be positive")
    active_transport: BetdaqSoapTransport = (
        UrllibBetdaqSoapTransport() if transport is None else transport
    )
    canonical_origin = _canonical_transport(active_transport)
    request_body = _request_body(credentials, price_format)
    try:
        raw = active_transport.post(
            READONLY_ENDPOINT,
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f'"{_SOAP_ACTION}"',
            },
            body=request_body,
            timeout_seconds=float(timeout_seconds),
        )
    except Exception:
        # Deliberately discard arbitrary provider/custom transport text: credentials or
        # raw secure/header material must never leak into product diagnostics.
        raise BetdaqOddsLadderAcquisitionError(
            "BETDAQ GetOddsLadder transport failed"
        ) from None
    if type(raw) is not bytes or not raw:
        raise BetdaqOddsLadderAcquisitionError(
            "BETDAQ GetOddsLadder transport returned invalid payload"
        )
    try:
        parsed = parse_get_odds_ladder_response(raw)
    except Exception as exc:
        # Parser exceptions are already secret-free provider/protocol evidence; preserve
        # the stable class only, not potentially arbitrary string material.
        raise BetdaqOddsLadderAcquisitionError(
            f"BETDAQ GetOddsLadder validation failed: {type(exc).__name__}"
        ) from None
    return _bind_observation(
        parsed,
        price_format=price_format,
        raw_response=raw,
        canonical_origin=canonical_origin,
    )

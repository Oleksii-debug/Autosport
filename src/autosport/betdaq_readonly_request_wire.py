from __future__ import annotations

from decimal import Decimal, InvalidOperation
import xml.etree.ElementTree as ET

from .betdaq_account_readonly import BetdaqCredentials
from .betdaq_readonly_market_wire import EXTERNAL_API_NS, SOAP11_NS
from .betdaq_readonly_provider import (
    BETDAQ_GET_PRICES_ENDPOINT,
    BETDAQ_GET_PRICES_SOAP_ACTION,
    BetdaqGetPricesRequest,
    _fixed_decimal_text,
)

_MAX_HEADER_TEXT_CHARS = 4096


def _xml_attribute_text(value: object, field: str) -> str:
    """Validate opaque header text without echoing it in errors."""

    if type(value) is not str or not value:
        raise ValueError(f"{field} must be non-empty text")
    if len(value) > _MAX_HEADER_TEXT_CHARS:
        raise ValueError(f"{field} is too long")
    for character in value:
        codepoint = ord(character)
        if not (
            character in "\t\r\n"
            or codepoint == 0x20
            or 0x21 <= codepoint <= 0xD7FF
            or 0xE000 <= codepoint <= 0xFFFD
            or 0x10000 <= codepoint <= 0x10FFFF
        ):
            raise ValueError(f"{field} contains a character unsafe for XML")
    return value


def _version_text(value: str) -> str:
    _xml_attribute_text(value, "version")
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError("version must be finite decimal text") from None
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("version must be positive finite decimal text")
    rendered = _fixed_decimal_text(parsed, "version")
    if rendered != value:
        raise ValueError("version must use canonical fixed-point decimal text")
    return value


class BetdaqSoap11WireRequest:
    """One serialized SOAP 1.1 request; repr never includes credential-bearing XML."""

    __slots__ = ("_body",)

    endpoint = BETDAQ_GET_PRICES_ENDPOINT
    content_type = "text/xml; charset=utf-8"
    soap_action = f'"{BETDAQ_GET_PRICES_SOAP_ACTION}"'

    def __init__(self, body: bytes) -> None:
        if type(body) is not bytes or not body:
            raise ValueError("body must be non-empty bytes")
        self._body = body

    @property
    def body(self) -> bytes:
        return self._body

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Content-Type": self.content_type,
            "SOAPAction": self.soap_action,
        }

    def __repr__(self) -> str:
        return (
            "BetdaqSoap11WireRequest("
            f"endpoint={self.endpoint!r}, "
            f"body=<redacted {len(self._body)} bytes>)"
        )


def _tag(namespace: str, local_name: str) -> str:
    return f"{{{namespace}}}{local_name}"


def _xml_bool(value: bool) -> str:
    if type(value) is not bool:
        raise TypeError("SOAP boolean must be bool")
    return "true" if value else "false"


def build_get_prices_soap11_request(
    credentials: BetdaqCredentials,
    request: BetdaqGetPricesRequest,
) -> BetdaqSoap11WireRequest:
    """Serialize BETDAQ GetPrices using the canonical product credential object.

    BETDAQ documents ReadOnly methods as username-only authentication. The live ASMX
    header schema still exposes password/applicationIdentifier attributes, so they are
    emitted as empty schema-compatible values here. Secure credential material is never
    placed on this ReadOnly request wire. Transport, rate and entitlement authority
    remain outside this serializer.
    """

    if type(credentials) is not BetdaqCredentials:
        raise TypeError("credentials must be canonical BetdaqCredentials")
    if type(request) is not BetdaqGetPricesRequest:
        raise TypeError("request must be BetdaqGetPricesRequest")

    version = _version_text(credentials.version)
    language_code = _xml_attribute_text(credentials.language_code, "language_code")
    username = _xml_attribute_text(credentials.username, "username")

    envelope = ET.Element(_tag(SOAP11_NS, "Envelope"))
    soap_header = ET.SubElement(envelope, _tag(SOAP11_NS, "Header"))
    ET.SubElement(
        soap_header,
        _tag(EXTERNAL_API_NS, "ExternalApiHeader"),
        {
            "version": version,
            "languageCode": language_code,
            "username": username,
            "password": "",
            "applicationIdentifier": "",
        },
    )
    soap_body = ET.SubElement(envelope, _tag(SOAP11_NS, "Body"))
    method = ET.SubElement(soap_body, _tag(EXTERNAL_API_NS, "GetPrices"))
    method_request = ET.SubElement(
        method,
        _tag(EXTERNAL_API_NS, "getPricesRequest"),
        {
            "ThresholdAmount": _fixed_decimal_text(
                request.threshold_amount,
                "threshold_amount",
            ),
            "NumberForPricesRequired": str(request.number_for_prices_required),
            "NumberAgainstPricesRequired": str(request.number_against_prices_required),
            "WantMarketMatchedAmount": _xml_bool(request.want_market_matched_amount),
            "WantSelectionsMatchedAmounts": _xml_bool(
                request.want_selections_matched_amounts
            ),
            "WantSelectionMatchedDetails": _xml_bool(
                request.want_selection_matched_details
            ),
        },
    )
    for market_id in request.market_ids:
        child = ET.SubElement(method_request, _tag(EXTERNAL_API_NS, "MarketIds"))
        child.text = str(market_id)

    body = ET.tostring(
        envelope,
        encoding="utf-8",
        xml_declaration=True,
        short_empty_elements=True,
    )
    return BetdaqSoap11WireRequest(body)

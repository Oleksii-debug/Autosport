from __future__ import annotations

from decimal import Decimal
import xml.etree.ElementTree as ET

from .betdaq_readonly_market_wire import EXTERNAL_API_NS, SOAP11_NS
from .betdaq_readonly_provider import (
    BETDAQ_GET_PRICES_ENDPOINT,
    BETDAQ_GET_PRICES_SOAP_ACTION,
    BetdaqGetPricesRequest,
    _fixed_decimal_text,
)

_MAX_HEADER_TEXT_CHARS = 4096


def _xml_attribute_text(value: object, field: str) -> str:
    """Validate an opaque SOAP-header value without ever echoing it in errors."""

    if not isinstance(value, str):
        raise TypeError(f"{field} must be str")
    if not value:
        raise ValueError(f"{field} must be non-empty")
    if len(value) > _MAX_HEADER_TEXT_CHARS:
        raise ValueError(f"{field} is too long")
    for character in value:
        codepoint = ord(character)
        if character in "\t\r\n" or not (
            codepoint == 0x20
            or 0x21 <= codepoint <= 0xD7FF
            or 0xE000 <= codepoint <= 0xFFFD
            or 0x10000 <= codepoint <= 0x10FFFF
        ):
            raise ValueError(f"{field} contains a character unsafe for an XML attribute")
    return value


class BetdaqExternalApiHeader:
    """Ephemeral BETDAQ SOAP header material.

    Username, password and application identifier are intentionally excluded from
    ``repr``. This object is an in-memory request input, not durable provider
    evidence and not proof of API entitlement.
    """

    __slots__ = (
        "_version",
        "_language_code",
        "_username",
        "_password",
        "_application_identifier",
    )

    def __init__(
        self,
        *,
        username: str,
        password: str,
        application_identifier: str,
        language_code: str,
        version: Decimal = Decimal("2.0"),
    ) -> None:
        if not isinstance(version, Decimal) or not version.is_finite() or version <= 0:
            raise ValueError("version must be a positive finite Decimal")
        _fixed_decimal_text(version, "version")
        self._version = version
        self._language_code = _xml_attribute_text(language_code, "language_code")
        self._username = _xml_attribute_text(username, "username")
        self._password = _xml_attribute_text(password, "password")
        self._application_identifier = _xml_attribute_text(
            application_identifier,
            "application_identifier",
        )

    @property
    def version(self) -> Decimal:
        return self._version

    @property
    def language_code(self) -> str:
        return self._language_code

    @property
    def username(self) -> str:
        return self._username

    @property
    def password(self) -> str:
        return self._password

    @property
    def application_identifier(self) -> str:
        return self._application_identifier

    def __repr__(self) -> str:
        return (
            "BetdaqExternalApiHeader("
            f"version={self._version!r}, "
            f"language_code={self._language_code!r}, "
            "credentials=<redacted>)"
        )


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
    header: BetdaqExternalApiHeader,
    request: BetdaqGetPricesRequest,
) -> BetdaqSoap11WireRequest:
    """Serialize the documented BETDAQ GetPrices SOAP 1.1 request exactly once.

    The function performs no network I/O and emits no durable/loggable credential
    evidence. The caller owns transport, rate-budget and entitlement decisions.
    """

    if type(header) is not BetdaqExternalApiHeader:
        raise TypeError("header must be BetdaqExternalApiHeader")
    if type(request) is not BetdaqGetPricesRequest:
        raise TypeError("request must be BetdaqGetPricesRequest")

    envelope = ET.Element(_tag(SOAP11_NS, "Envelope"))
    soap_header = ET.SubElement(envelope, _tag(SOAP11_NS, "Header"))
    ET.SubElement(
        soap_header,
        _tag(EXTERNAL_API_NS, "ExternalApiHeader"),
        {
            "version": _fixed_decimal_text(header.version, "version"),
            "languageCode": header.language_code,
            "username": header.username,
            "password": header.password,
            "applicationIdentifier": header.application_identifier,
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

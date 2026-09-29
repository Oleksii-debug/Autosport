from __future__ import annotations

from dataclasses import dataclass
import xml.etree.ElementTree as ET

from .betdaq_account_readonly import BetdaqCredentials
from .betdaq_readonly_market_wire import EXTERNAL_API_NS, SOAP11_NS
from .betdaq_readonly_request_wire import _version_text, _xml_attribute_text

BETDAQ_EVENT_SUBTREE_ENDPOINT = "https://api.betdaq.com/v2.0/ReadOnlyService.asmx"
BETDAQ_EVENT_SUBTREE_SOAP_ACTION = (
    "http://www.GlobalBettingExchange.com/ExternalAPI/GetEventSubTreeNoSelections"
)
BETDAQ_PROVIDER_LONG_MAX = (1 << 63) - 1


def _tag(namespace: str, local_name: str) -> str:
    return f"{{{namespace}}}{local_name}"


@dataclass(frozen=True, slots=True)
class BetdaqEventSubTreeRequest:
    event_classifier_ids: tuple[int, ...]
    want_direct_descendents_only: bool = False
    want_play_markets: bool = True

    def __post_init__(self) -> None:
        if (
            type(self.event_classifier_ids) is not tuple
            or not self.event_classifier_ids
            or any(
                type(value) is not int
                or value < 0
                or value > BETDAQ_PROVIDER_LONG_MAX
                for value in self.event_classifier_ids
            )
            or len(set(self.event_classifier_ids)) != len(self.event_classifier_ids)
        ):
            raise ValueError(
                "event_classifier_ids must be unique provider long integers in 0..2^63-1"
            )
        if type(self.want_direct_descendents_only) is not bool:
            raise TypeError("want_direct_descendents_only must be bool")
        if type(self.want_play_markets) is not bool:
            raise TypeError("want_play_markets must be bool")


class BetdaqEventSubTreeSoap11WireRequest:
    __slots__ = ("_body",)

    endpoint = BETDAQ_EVENT_SUBTREE_ENDPOINT
    content_type = "text/xml; charset=utf-8"
    soap_action = f'"{BETDAQ_EVENT_SUBTREE_SOAP_ACTION}"'

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
            "BetdaqEventSubTreeSoap11WireRequest("
            f"endpoint={self.endpoint!r}, "
            f"body=<redacted {len(self._body)} bytes>)"
        )


def _xml_bool(value: bool) -> str:
    if type(value) is not bool:
        raise TypeError("SOAP boolean must be bool")
    return "true" if value else "false"


def build_get_event_subtree_no_selections_soap11_request(
    credentials: BetdaqCredentials,
    request: BetdaqEventSubTreeRequest,
) -> BetdaqEventSubTreeSoap11WireRequest:
    """Serialize the documented BETDAQ read-only event-tree request.

    ReadOnly calls use the canonical credential object but intentionally emit only the
    username-bearing public header semantics already used by GetPrices. Secure password
    and application-identifier material is never placed on this wire.
    """

    if type(credentials) is not BetdaqCredentials:
        raise TypeError("credentials must be canonical BetdaqCredentials")
    if type(request) is not BetdaqEventSubTreeRequest:
        raise TypeError("request must be BetdaqEventSubTreeRequest")

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
    method = ET.SubElement(
        soap_body,
        _tag(EXTERNAL_API_NS, "GetEventSubTreeNoSelections"),
    )
    method_request = ET.SubElement(
        method,
        _tag(EXTERNAL_API_NS, "getEventSubTreeNoSelectionsRequest"),
        {
            "WantDirectDescendentsOnly": _xml_bool(
                request.want_direct_descendents_only
            ),
            "WantPlayMarkets": _xml_bool(request.want_play_markets),
        },
    )
    for event_classifier_id in request.event_classifier_ids:
        child = ET.SubElement(
            method_request,
            _tag(EXTERNAL_API_NS, "EventClassifierIds"),
        )
        child.text = str(event_classifier_id)

    body = ET.tostring(
        envelope,
        encoding="utf-8",
        xml_declaration=True,
        short_empty_elements=True,
    )
    return BetdaqEventSubTreeSoap11WireRequest(body)

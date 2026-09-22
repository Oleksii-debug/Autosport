from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import xml.etree.ElementTree as ET

from .betdaq_readonly_market_wire import (
    EXTERNAL_API_NS,
    SOAP11_NS,
    SOAP12_NS,
    BetdaqProviderStatusError,
    BetdaqSoapProtocolError,
    _MAX_XML_BYTES,
    _children_exact,
    _decimal,
    _integer,
    _one_child,
    _optional_attr,
    _provider_created_at,
    _required_attr,
    _safe_text,
    _soap_fault,
    _tag,
)


_MAX_LADDER_ENTRIES = 10_000
_XSD_DECIMAL_LEXICAL_RE = re.compile(
    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\Z"
)


@dataclass(frozen=True, slots=True)
class BetdaqOddsLadderEntry:
    price: Decimal
    price_text: str
    representation: str


@dataclass(frozen=True, slots=True)
class BetdaqOddsLadderWireResponse:
    return_status_present: bool
    return_code: int | None
    return_description: str | None
    call_id: str | None
    provider_created_at: datetime | None
    provider_created_at_text: str | None
    entries: tuple[BetdaqOddsLadderEntry, ...]
    content_sha256: str


def _parse_return_status(
    result: ET.Element,
) -> tuple[bool, int | None, str | None, str | None]:
    statuses = _children_exact(result, _tag(EXTERNAL_API_NS, "ReturnStatus"))
    if len(statuses) > 1:
        raise BetdaqSoapProtocolError("ReturnStatus must occur at most once")
    if not statuses:
        # Current BETDAQ generated GetOddsLadder examples omit inherited
        # BaseResponse/ReturnStatus. Preserve absence rather than minting success.
        return False, None, None, None

    status = statuses[0]
    code = _integer(_required_attr(status, "Code"), "ReturnStatus Code")
    description = _safe_text(
        _required_attr(status, "Description"),
        "ReturnStatus Description",
    )
    call_id_raw = _optional_attr(status, "CallId")
    call_id = (
        None
        if call_id_raw is None
        else _safe_text(call_id_raw, "ReturnStatus CallId")
    )
    if code != 0:
        raise BetdaqProviderStatusError(
            code=code,
            description=description,
            call_id=call_id,
        )
    return True, code, description, call_id


def _content_digest(entries: tuple[BetdaqOddsLadderEntry, ...]) -> str:
    material = [
        {
            "price": entry.price_text,
            "representation": entry.representation,
        }
        for entry in entries
    ]
    payload = json.dumps(
        material,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def parse_get_odds_ladder_response(
    xml_payload: bytes | str,
) -> BetdaqOddsLadderWireResponse:
    """Parse provider-native GetOddsLadder content without authorizing placement.

    The returned content digest is only deterministic content identity. It is not a
    provider version, freshness proof, execution permission, or price-admission
    authority. A live consumer must bind this observation to its own acquisition
    evidence before using it as a current ladder prerequisite.
    """

    if isinstance(xml_payload, str):
        payload = xml_payload.encode("utf-8")
    elif isinstance(xml_payload, bytes):
        payload = xml_payload
    else:
        raise TypeError("xml_payload must be bytes or str")
    if not payload or len(payload) > _MAX_XML_BYTES:
        raise BetdaqSoapProtocolError("SOAP payload size is invalid")
    upper = payload.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise BetdaqSoapProtocolError("DTD/entity declarations are forbidden")

    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise BetdaqSoapProtocolError("malformed SOAP XML") from exc

    if root.tag == _tag(SOAP11_NS, "Envelope"):
        soap_ns = SOAP11_NS
    elif root.tag == _tag(SOAP12_NS, "Envelope"):
        soap_ns = SOAP12_NS
    else:
        raise BetdaqSoapProtocolError("unsupported SOAP envelope namespace")

    provider_created_at, provider_created_at_text = _provider_created_at(root, soap_ns)
    body = _one_child(root, _tag(soap_ns, "Body"), "SOAP Body")
    fault = _soap_fault(body, soap_ns)
    if fault is not None:
        raise fault

    response = _one_child(
        body,
        _tag(EXTERNAL_API_NS, "GetOddsLadderResponse"),
        "GetOddsLadderResponse",
    )
    if len(list(body)) != 1:
        raise BetdaqSoapProtocolError(
            "SOAP Body must contain exactly one GetOddsLadderResponse"
        )
    result = _one_child(
        response,
        _tag(EXTERNAL_API_NS, "GetOddsLadderResult"),
        "GetOddsLadderResult",
    )
    if len(list(response)) != 1:
        raise BetdaqSoapProtocolError(
            "GetOddsLadderResponse must contain exactly one result"
        )

    (
        return_status_present,
        return_code,
        return_description,
        call_id,
    ) = _parse_return_status(result)

    entries: list[BetdaqOddsLadderEntry] = []
    seen_prices: set[Decimal] = set()
    for child in list(result):
        if child.tag == _tag(EXTERNAL_API_NS, "ReturnStatus"):
            continue
        if child.tag != _tag(EXTERNAL_API_NS, "Ladder"):
            raise BetdaqSoapProtocolError(
                "GetOddsLadderResult contains unexpected child or namespace"
            )
        if len(entries) >= _MAX_LADDER_ENTRIES:
            raise BetdaqSoapProtocolError("BETDAQ odds ladder exceeds entry bound")
        if list(child) or (child.text and child.text.strip()):
            raise BetdaqSoapProtocolError("Ladder entry must not contain child content")
        if set(child.attrib) != {"price", "representation"}:
            raise BetdaqSoapProtocolError(
                "Ladder entry must contain exactly price and representation attributes"
            )

        price_text = _required_attr(child, "price")
        if price_text != price_text.strip():
            raise BetdaqSoapProtocolError("Ladder price must be trimmed")
        if _XSD_DECIMAL_LEXICAL_RE.fullmatch(price_text) is None:
            raise BetdaqSoapProtocolError(
                "Ladder price must use XML Schema decimal lexical form"
            )
        price = _decimal(
            price_text,
            "Ladder price",
            strictly_positive=True,
        )
        if price <= 1:
            raise BetdaqSoapProtocolError(
                "Ladder decimal odds must be greater than 1"
            )
        representation = _safe_text(
            _required_attr(child, "representation"),
            "Ladder representation",
        )
        if price in seen_prices:
            raise BetdaqSoapProtocolError("duplicate Ladder price")
        seen_prices.add(price)
        entries.append(
            BetdaqOddsLadderEntry(
                price=price,
                price_text=price_text,
                representation=representation,
            )
        )

    if not entries:
        raise BetdaqSoapProtocolError(
            "GetOddsLadder response must contain at least one Ladder entry"
        )

    frozen = tuple(entries)
    return BetdaqOddsLadderWireResponse(
        return_status_present=return_status_present,
        return_code=return_code,
        return_description=return_description,
        call_id=call_id,
        provider_created_at=provider_created_at,
        provider_created_at_text=provider_created_at_text,
        entries=frozen,
        content_sha256=_content_digest(frozen),
    )

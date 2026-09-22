from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import re
import xml.etree.ElementTree as ET

from .betdaq_readonly_market_wire import (
    EXTERNAL_API_NS,
    SOAP11_NS,
    SOAP12_NS,
    BetdaqProviderStatusError,
    BetdaqSoapProtocolError,
    _MAX_XML_BYTES,
    _boolean,
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


_MAX_SELECTION_ROWS = 100_000
_MAX_SETTLEMENT_ROWS = 1_000

_RETURN_STATUS_ATTRIBUTES = frozenset({"Code", "Description", "CallId"})
_CURRENT_SEQUENCE_RESULT_ATTRIBUTES = frozenset({"SelectionSequenceNumber"})
_CHANGED_RESULT_ATTRIBUTES = frozenset()
_CHANGED_SELECTION_ATTRIBUTES = frozenset(
    {
        "Id",
        "Name",
        "DisplayOrder",
        "IsHidden",
        "Status",
        "ResetCount",
        "WithdrawalFactor",
        "MarketId",
        "SelectionSequenceNumber",
        "CancelOrdersTime",
    }
)
_SETTLEMENT_INFORMATION_ATTRIBUTES = frozenset(
    {
        "SettledTime",
        "VoidPercentage",
        "LeftSideFactor",
        "RightSideFactor",
        "SettlementResultString",
    }
)


_XSD_DATETIME_LEXICAL = re.compile(
    r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?\Z"
)


@dataclass(frozen=True, slots=True)
class BetdaqWireTimestamp:
    value: datetime
    text: str
    timezone_present: bool


@dataclass(frozen=True, slots=True)
class BetdaqSettlementInformation:
    settled_time: BetdaqWireTimestamp
    void_percentage: Decimal
    left_side_factor: Decimal
    right_side_factor: Decimal
    settlement_result_string: str


@dataclass(frozen=True, slots=True)
class BetdaqChangedSelection:
    selection_id: int
    name: str
    display_order: int
    is_hidden: bool
    status_code: int
    reset_count: int
    withdrawal_factor: Decimal
    market_id: int
    selection_sequence_number: int
    cancel_orders_time: BetdaqWireTimestamp
    settlement_information: tuple[BetdaqSettlementInformation, ...]


@dataclass(frozen=True, slots=True)
class BetdaqCurrentSelectionSequenceWireResponse:
    selection_sequence_number: int
    return_status_present: bool
    return_code: int | None
    return_description: str | None
    call_id: str | None
    provider_created_at: datetime | None
    provider_created_at_text: str | None


@dataclass(frozen=True, slots=True)
class BetdaqSelectionsChangedWireResponse:
    return_status_present: bool
    return_code: int | None
    return_description: str | None
    call_id: str | None
    provider_created_at: datetime | None
    provider_created_at_text: str | None
    selections: tuple[BetdaqChangedSelection, ...]


def _wire_timestamp(value: str, field: str) -> BetdaqWireTimestamp:
    raw = value.strip()
    if (
        not raw
        or raw != value
        or _XSD_DATETIME_LEXICAL.fullmatch(raw) is None
    ):
        raise BetdaqSoapProtocolError(
            f"{field} must be a trimmed ISO-8601/XSD dateTime"
        )
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise BetdaqSoapProtocolError(f"{field} must be an ISO-8601 dateTime") from exc
    return BetdaqWireTimestamp(
        value=parsed,
        text=raw,
        timezone_present=parsed.tzinfo is not None and parsed.utcoffset() is not None,
    )


def _reject_unknown_attributes(
    element: ET.Element,
    allowed: frozenset[str],
    field: str,
) -> None:
    unexpected = sorted(set(element.attrib) - allowed)
    if unexpected:
        joined = ", ".join(unexpected)
        raise BetdaqSoapProtocolError(
            f"{field} contains unexpected attribute(s): {joined}"
        )


def _parse_return_status(
    result: ET.Element,
) -> tuple[bool, int | None, str | None, str | None]:
    statuses = _children_exact(result, _tag(EXTERNAL_API_NS, "ReturnStatus"))
    if len(statuses) > 1:
        raise BetdaqSoapProtocolError("ReturnStatus must occur at most once")
    if not statuses:
        return False, None, None, None

    status = statuses[0]
    _reject_unknown_attributes(status, _RETURN_STATUS_ATTRIBUTES, "ReturnStatus")
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


def _parse_envelope(
    xml_payload: bytes | str,
    *,
    operation: str,
) -> tuple[ET.Element, datetime | None, str | None]:
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
        _tag(EXTERNAL_API_NS, f"{operation}Response"),
        f"{operation}Response",
    )
    if len(list(body)) != 1:
        raise BetdaqSoapProtocolError(
            f"SOAP Body must contain exactly one {operation}Response"
        )
    result = _one_child(
        response,
        _tag(EXTERNAL_API_NS, f"{operation}Result"),
        f"{operation}Result",
    )
    if len(list(response)) != 1:
        raise BetdaqSoapProtocolError(
            f"{operation}Response must contain exactly one result"
        )
    return result, provider_created_at, provider_created_at_text


def _parse_settlement_information(
    element: ET.Element,
) -> BetdaqSettlementInformation:
    _reject_unknown_attributes(
        element,
        _SETTLEMENT_INFORMATION_ATTRIBUTES,
        "SettlementInformation",
    )
    if list(element) or (element.text and element.text.strip()):
        raise BetdaqSoapProtocolError(
            "SettlementInformation must not contain child content"
        )
    return BetdaqSettlementInformation(
        settled_time=_wire_timestamp(
            _required_attr(element, "SettledTime"),
            "SettlementInformation SettledTime",
        ),
        void_percentage=_decimal(
            _required_attr(element, "VoidPercentage"),
            "SettlementInformation VoidPercentage",
            nonnegative=True,
        ),
        left_side_factor=_decimal(
            _required_attr(element, "LeftSideFactor"),
            "SettlementInformation LeftSideFactor",
            nonnegative=True,
        ),
        right_side_factor=_decimal(
            _required_attr(element, "RightSideFactor"),
            "SettlementInformation RightSideFactor",
            nonnegative=True,
        ),
        settlement_result_string=_safe_text(
            _required_attr(element, "SettlementResultString"),
            "SettlementInformation SettlementResultString",
        ),
    )


def _parse_changed_selection(element: ET.Element) -> BetdaqChangedSelection:
    _reject_unknown_attributes(
        element,
        _CHANGED_SELECTION_ATTRIBUTES,
        "Selections",
    )
    settlement_rows: list[BetdaqSettlementInformation] = []
    for child in list(element):
        if child.tag != _tag(EXTERNAL_API_NS, "SettlementInformation"):
            raise BetdaqSoapProtocolError(
                "Selections contains unexpected child or namespace"
            )
        if len(settlement_rows) >= _MAX_SETTLEMENT_ROWS:
            raise BetdaqSoapProtocolError(
                "selection settlement information exceeds row bound"
            )
        settlement_rows.append(_parse_settlement_information(child))

    return BetdaqChangedSelection(
        selection_id=_integer(
            _required_attr(element, "Id"),
            "selection Id",
            minimum=0,
        ),
        name=_safe_text(_required_attr(element, "Name"), "selection Name"),
        display_order=_integer(
            _required_attr(element, "DisplayOrder"),
            "selection DisplayOrder",
        ),
        is_hidden=_boolean(
            _required_attr(element, "IsHidden"),
            "selection IsHidden",
        ),
        status_code=_integer(
            _required_attr(element, "Status"),
            "selection Status",
            minimum=0,
        ),
        reset_count=_integer(
            _required_attr(element, "ResetCount"),
            "selection ResetCount",
            minimum=0,
        ),
        withdrawal_factor=_decimal(
            _required_attr(element, "WithdrawalFactor"),
            "selection WithdrawalFactor",
            nonnegative=True,
        ),
        market_id=_integer(
            _required_attr(element, "MarketId"),
            "selection MarketId",
            minimum=0,
        ),
        selection_sequence_number=_integer(
            _required_attr(element, "SelectionSequenceNumber"),
            "selection SelectionSequenceNumber",
            minimum=0,
        ),
        cancel_orders_time=_wire_timestamp(
            _required_attr(element, "CancelOrdersTime"),
            "selection CancelOrdersTime",
        ),
        settlement_information=tuple(settlement_rows),
    )


def parse_get_current_selection_sequence_number_response(
    xml_payload: bytes | str,
) -> BetdaqCurrentSelectionSequenceWireResponse:
    """Parse the provider's current maximum selection sequence number only."""

    result, provider_created_at, provider_created_at_text = _parse_envelope(
        xml_payload,
        operation="GetCurrentSelectionSequenceNumber",
    )
    (
        return_status_present,
        return_code,
        return_description,
        call_id,
    ) = _parse_return_status(result)

    _reject_unknown_attributes(
        result,
        _CURRENT_SEQUENCE_RESULT_ATTRIBUTES,
        "GetCurrentSelectionSequenceNumberResult",
    )
    allowed_child = _tag(EXTERNAL_API_NS, "ReturnStatus")
    if any(child.tag != allowed_child for child in list(result)):
        raise BetdaqSoapProtocolError(
            "GetCurrentSelectionSequenceNumberResult contains unexpected child"
        )

    return BetdaqCurrentSelectionSequenceWireResponse(
        selection_sequence_number=_integer(
            _required_attr(result, "SelectionSequenceNumber"),
            "SelectionSequenceNumber",
            minimum=0,
        ),
        return_status_present=return_status_present,
        return_code=return_code,
        return_description=return_description,
        call_id=call_id,
        provider_created_at=provider_created_at,
        provider_created_at_text=provider_created_at_text,
    )


def parse_list_selections_changed_since_response(
    xml_payload: bytes | str,
) -> BetdaqSelectionsChangedWireResponse:
    """Parse provider-native changed selections without claiming cursor completeness."""

    result, provider_created_at, provider_created_at_text = _parse_envelope(
        xml_payload,
        operation="ListSelectionsChangedSince",
    )
    _reject_unknown_attributes(
        result,
        _CHANGED_RESULT_ATTRIBUTES,
        "ListSelectionsChangedSinceResult",
    )
    (
        return_status_present,
        return_code,
        return_description,
        call_id,
    ) = _parse_return_status(result)

    selections: list[BetdaqChangedSelection] = []
    seen_rows: set[tuple[int, int]] = set()
    for child in list(result):
        if child.tag == _tag(EXTERNAL_API_NS, "ReturnStatus"):
            continue
        if child.tag != _tag(EXTERNAL_API_NS, "Selections"):
            raise BetdaqSoapProtocolError(
                "ListSelectionsChangedSinceResult contains unexpected child or namespace"
            )
        if len(selections) >= _MAX_SELECTION_ROWS:
            raise BetdaqSoapProtocolError(
                "ListSelectionsChangedSince exceeds selection row bound"
            )
        parsed = _parse_changed_selection(child)
        key = (parsed.selection_id, parsed.selection_sequence_number)
        if key in seen_rows:
            raise BetdaqSoapProtocolError(
                "duplicate selection/sequence row in one changed-selection response"
            )
        seen_rows.add(key)
        selections.append(parsed)

    return BetdaqSelectionsChangedWireResponse(
        return_status_present=return_status_present,
        return_code=return_code,
        return_description=return_description,
        call_id=call_id,
        provider_created_at=provider_created_at,
        provider_created_at_text=provider_created_at_text,
        selections=tuple(selections),
    )

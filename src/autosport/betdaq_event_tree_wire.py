from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import xml.etree.ElementTree as ET

from .betdaq_readonly_market_wire import (
    EXTERNAL_API_NS,
    SOAP11_NS,
    SOAP12_NS,
    XSI_NS,
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
    _timestamp,
)


_MAX_TREE_DEPTH = 64
_MAX_TREE_ITEMS = 100_000


@dataclass(frozen=True, slots=True)
class BetdaqDiscoveryMarket:
    market_id: int
    name: str
    market_type_code: int
    status_code: int
    number_of_winning_selections: int
    start_time: datetime
    start_time_text: str
    withdrawal_sequence_number: int
    display_order: int
    is_play_market: bool
    is_enabled_for_multiples: bool
    is_in_running_allowed: bool
    is_managed_when_in_running: bool
    is_currently_in_running: bool
    in_running_delay_seconds: int
    event_classifier_id: int
    place_payout: Decimal


@dataclass(frozen=True, slots=True)
class BetdaqEventClassifier:
    event_classifier_id: int
    name: str
    display_order: int
    is_enabled_for_multiples: bool
    parent_id: int
    children: tuple["BetdaqEventClassifier", ...]
    markets: tuple[BetdaqDiscoveryMarket, ...]


@dataclass(frozen=True, slots=True)
class BetdaqEventTreeWireResponse:
    operation: str
    return_status_present: bool
    return_code: int | None
    return_description: str | None
    call_id: str | None
    provider_created_at: datetime | None
    provider_created_at_text: str | None
    event_classifiers: tuple[BetdaqEventClassifier, ...]


@dataclass(slots=True)
class _ParseState:
    event_ids: set[int]
    market_ids: set[int]
    item_count: int = 0

    def register_event(self, event_id: int) -> None:
        self.item_count += 1
        if self.item_count > _MAX_TREE_ITEMS:
            raise BetdaqSoapProtocolError("BETDAQ event tree exceeds bounded item count")
        if event_id in self.event_ids:
            raise BetdaqSoapProtocolError(
                "duplicate EventClassifier Id in one BETDAQ event tree"
            )
        self.event_ids.add(event_id)

    def register_market(self, market_id: int) -> None:
        self.item_count += 1
        if self.item_count > _MAX_TREE_ITEMS:
            raise BetdaqSoapProtocolError("BETDAQ event tree exceeds bounded item count")
        if market_id in self.market_ids:
            raise BetdaqSoapProtocolError(
                "duplicate Market Id in one BETDAQ event tree"
            )
        self.market_ids.add(market_id)


def _nil_placeholder(element: ET.Element, *, label: str) -> bool:
    nil = element.attrib.get(_tag(XSI_NS, "nil"))
    if nil is None:
        return False
    if nil not in {"true", "1"}:
        raise BetdaqSoapProtocolError(f"{label} xsi:nil must be true when present")
    if set(element.attrib) != {_tag(XSI_NS, "nil")}:
        raise BetdaqSoapProtocolError(
            f"{label} nil placeholder must not carry provider attributes"
        )
    if list(element) or (element.text and element.text.strip()):
        raise BetdaqSoapProtocolError(f"{label} nil placeholder must be empty")
    return True


def _parse_return_status(
    result: ET.Element,
) -> tuple[bool, int | None, str | None, str | None]:
    statuses = _children_exact(result, _tag(EXTERNAL_API_NS, "ReturnStatus"))
    if len(statuses) > 1:
        raise BetdaqSoapProtocolError("ReturnStatus must occur at most once")
    if not statuses:
        # BETDAQ generated ASMX examples for event-tree methods omit the inherited
        # BaseResponse node. Absence is retained as absence; it is never invented as
        # provider success by this parser-only boundary.
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


def _parse_market(
    element: ET.Element,
    *,
    containing_event_id: int,
    state: _ParseState,
) -> BetdaqDiscoveryMarket | None:
    if _nil_placeholder(element, label="Markets"):
        return None

    market_id = _integer(_required_attr(element, "Id"), "market Id", minimum=0)
    state.register_market(market_id)
    event_classifier_id = _integer(
        _required_attr(element, "EventClassifierId"),
        "market EventClassifierId",
        minimum=0,
    )
    if event_classifier_id != containing_event_id:
        raise BetdaqSoapProtocolError(
            "market EventClassifierId does not match containing EventClassifier"
        )

    start_time, start_time_text = _timestamp(
        _required_attr(element, "StartTime"),
        "market StartTime",
    )
    market = BetdaqDiscoveryMarket(
        market_id=market_id,
        name=_safe_text(_required_attr(element, "Name"), "market Name"),
        market_type_code=_integer(
            _required_attr(element, "Type"),
            "market Type",
            minimum=0,
        ),
        status_code=_integer(
            _required_attr(element, "Status"),
            "market Status",
            minimum=0,
        ),
        number_of_winning_selections=_integer(
            _required_attr(element, "NumberOfWinningSelections"),
            "market NumberOfWinningSelections",
            minimum=0,
        ),
        start_time=start_time,
        start_time_text=start_time_text,
        withdrawal_sequence_number=_integer(
            _required_attr(element, "WithdrawalSequenceNumber"),
            "market WithdrawalSequenceNumber",
            minimum=0,
        ),
        display_order=_integer(
            _required_attr(element, "DisplayOrder"),
            "market DisplayOrder",
            minimum=0,
        ),
        is_play_market=_boolean(
            _required_attr(element, "IsPlayMarket"),
            "market IsPlayMarket",
        ),
        is_enabled_for_multiples=_boolean(
            _required_attr(element, "IsEnabledForMultiples"),
            "market IsEnabledForMultiples",
        ),
        is_in_running_allowed=_boolean(
            _required_attr(element, "IsInRunningAllowed"),
            "market IsInRunningAllowed",
        ),
        is_managed_when_in_running=_boolean(
            _required_attr(element, "IsManagedWhenInRunning"),
            "market IsManagedWhenInRunning",
        ),
        is_currently_in_running=_boolean(
            _required_attr(element, "IsCurrentlyInRunning"),
            "market IsCurrentlyInRunning",
        ),
        in_running_delay_seconds=_integer(
            _required_attr(element, "InRunningDelaySeconds"),
            "market InRunningDelaySeconds",
            minimum=0,
        ),
        event_classifier_id=event_classifier_id,
        place_payout=_decimal(
            _required_attr(element, "PlacePayout"),
            "market PlacePayout",
            nonnegative=True,
        ),
    )

    for child in list(element):
        if child.tag != _tag(EXTERNAL_API_NS, "Selections"):
            raise BetdaqSoapProtocolError(
                "Markets contains an unexpected child element or namespace"
            )
        if not _nil_placeholder(child, label="Selections"):
            raise BetdaqSoapProtocolError(
                "GetEventSubTreeNoSelections must not carry selection data"
            )
    return market


def _parse_event(
    element: ET.Element,
    *,
    expected_parent_id: int | None,
    state: _ParseState,
    depth: int,
) -> BetdaqEventClassifier | None:
    if _nil_placeholder(element, label="EventClassifiers"):
        return None
    if depth > _MAX_TREE_DEPTH:
        raise BetdaqSoapProtocolError("BETDAQ event tree exceeds maximum depth")

    event_id = _integer(
        _required_attr(element, "Id"),
        "EventClassifier Id",
        minimum=0,
    )
    state.register_event(event_id)
    parent_id = _integer(
        _required_attr(element, "ParentId"),
        "EventClassifier ParentId",
        minimum=0,
    )
    if expected_parent_id is not None and parent_id != expected_parent_id:
        raise BetdaqSoapProtocolError(
            "EventClassifier ParentId does not match containing EventClassifier"
        )

    children: list[BetdaqEventClassifier] = []
    markets: list[BetdaqDiscoveryMarket] = []
    for child in list(element):
        if child.tag == _tag(EXTERNAL_API_NS, "EventClassifiers"):
            parsed_event = _parse_event(
                child,
                expected_parent_id=event_id,
                state=state,
                depth=depth + 1,
            )
            if parsed_event is not None:
                children.append(parsed_event)
        elif child.tag == _tag(EXTERNAL_API_NS, "Markets"):
            parsed_market = _parse_market(
                child,
                containing_event_id=event_id,
                state=state,
            )
            if parsed_market is not None:
                markets.append(parsed_market)
        else:
            raise BetdaqSoapProtocolError(
                "EventClassifiers contains an unexpected child element or namespace"
            )

    return BetdaqEventClassifier(
        event_classifier_id=event_id,
        name=_safe_text(_required_attr(element, "Name"), "EventClassifier Name"),
        display_order=_integer(
            _required_attr(element, "DisplayOrder"),
            "EventClassifier DisplayOrder",
            minimum=0,
        ),
        is_enabled_for_multiples=_boolean(
            _required_attr(element, "IsEnabledForMultiples"),
            "EventClassifier IsEnabledForMultiples",
        ),
        parent_id=parent_id,
        children=tuple(children),
        markets=tuple(markets),
    )


def _parse_event_tree_response(
    xml_payload: bytes | str,
    *,
    operation: str,
) -> BetdaqEventTreeWireResponse:
    if operation not in {"ListTopLevelEvents", "GetEventSubTreeNoSelections"}:
        raise ValueError("unsupported BETDAQ event-tree operation")
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

    response_tag = _tag(EXTERNAL_API_NS, f"{operation}Response")
    result_tag = _tag(EXTERNAL_API_NS, f"{operation}Result")
    response = _one_child(body, response_tag, f"{operation}Response")
    if len(list(body)) != 1:
        raise BetdaqSoapProtocolError(
            "SOAP Body must contain exactly one event-tree response"
        )
    result = _one_child(response, result_tag, f"{operation}Result")
    if len(list(response)) != 1:
        raise BetdaqSoapProtocolError(
            f"{operation}Response must contain exactly one result"
        )

    (
        return_status_present,
        return_code,
        return_description,
        call_id,
    ) = _parse_return_status(result)

    state = _ParseState(event_ids=set(), market_ids=set())
    events: list[BetdaqEventClassifier] = []
    for child in list(result):
        if child.tag == _tag(EXTERNAL_API_NS, "ReturnStatus"):
            continue
        if child.tag != _tag(EXTERNAL_API_NS, "EventClassifiers"):
            raise BetdaqSoapProtocolError(
                f"{operation}Result contains an unexpected child element or namespace"
            )
        parsed = _parse_event(
            child,
            expected_parent_id=None,
            state=state,
            depth=0,
        )
        if parsed is not None:
            events.append(parsed)

    return BetdaqEventTreeWireResponse(
        operation=operation,
        return_status_present=return_status_present,
        return_code=return_code,
        return_description=return_description,
        call_id=call_id,
        provider_created_at=provider_created_at,
        provider_created_at_text=provider_created_at_text,
        event_classifiers=tuple(events),
    )


def parse_list_top_level_events_response(
    xml_payload: bytes | str,
) -> BetdaqEventTreeWireResponse:
    """Parse ListTopLevelEvents without inferring canonical sport or market truth."""

    return _parse_event_tree_response(
        xml_payload,
        operation="ListTopLevelEvents",
    )


def parse_get_event_subtree_no_selections_response(
    xml_payload: bytes | str,
) -> BetdaqEventTreeWireResponse:
    """Parse GetEventSubTreeNoSelections as provider-native discovery evidence only."""

    return _parse_event_tree_response(
        xml_payload,
        operation="GetEventSubTreeNoSelections",
    )

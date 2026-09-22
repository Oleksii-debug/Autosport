from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Final
import xml.etree.ElementTree as ET


EXTERNAL_API_NS: Final = "http://www.GlobalBettingExchange.com/ExternalAPI/"
SOAP11_NS: Final = "http://schemas.xmlsoap.org/soap/envelope/"
SOAP12_NS: Final = "http://www.w3.org/2003/05/soap-envelope"
XSI_NS: Final = "http://www.w3.org/2001/XMLSchema-instance"
WSSE_NS: Final = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU_NS: Final = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
_MAX_XML_BYTES: Final = 4 * 1024 * 1024
_MAX_TEXT: Final = 512
_RC016_MARKET_NEITHER_SUSPENDED_NOR_ACTIVE: Final = 16


class BetdaqWireError(ValueError):
    """Base class for fail-closed BETDAQ read-only wire errors."""


class BetdaqSoapProtocolError(BetdaqWireError):
    """The response is not the exact supported SOAP/GetPrices contract."""


class BetdaqSoapFaultError(BetdaqWireError):
    """BETDAQ returned a SOAP fault instead of a GetPrices response."""

    def __init__(self, *, code: str, reason: str) -> None:
        self.code = _safe_text(code, "fault code")
        self.reason = _safe_text(reason, "fault reason")
        super().__init__(f"BETDAQ SOAP fault: {self.code}: {self.reason}")


class BetdaqProviderStatusError(BetdaqWireError):
    """BETDAQ Base ReturnStatus or per-market ReturnCode was non-success."""

    def __init__(
        self,
        *,
        code: int,
        description: str,
        call_id: str | None = None,
        scope: str = "response",
    ) -> None:
        self.code = code
        self.description = _safe_text(description, "provider description")
        self.call_id = (
            None if call_id is None else _safe_text(call_id, "provider call id")
        )
        self.scope = _safe_text(scope, "provider error scope")
        suffix = "" if self.call_id is None else f" call_id={self.call_id}"
        super().__init__(
            f"BETDAQ {self.scope} status {self.code}: {self.description}{suffix}"
        )


@dataclass(frozen=True, slots=True)
class BetdaqPriceLevel:
    """One provider-native GetPrices level; side is intentionally not canonicalized."""

    provider_side: str
    price: Decimal
    stake: Decimal


@dataclass(frozen=True, slots=True)
class BetdaqSelectionPrices:
    selection_id: int
    name: str
    status_code: int
    reset_count: int
    deduction_factor: Decimal | None
    for_side_prices: tuple[BetdaqPriceLevel, ...]
    against_side_prices: tuple[BetdaqPriceLevel, ...]


@dataclass(frozen=True, slots=True)
class BetdaqMarketPrices:
    market_id: int
    name: str
    market_type_code: int
    status_code: int
    start_time: datetime
    start_time_text: str
    withdrawal_sequence_number: int
    is_play_market: bool
    is_in_running_allowed: bool
    is_managed_when_in_running: bool
    is_currently_in_running: bool
    in_running_delay_seconds: int
    selections: tuple[BetdaqSelectionPrices, ...]


@dataclass(frozen=True, slots=True)
class BetdaqUnavailableMarket:
    """One requested market that BETDAQ explicitly marked unavailable via RC016."""

    market_id: int
    return_code: int


@dataclass(frozen=True, slots=True)
class BetdaqGetPricesWireResponse:
    return_code: int
    return_description: str
    call_id: str | None
    provider_created_at: datetime | None
    provider_created_at_text: str | None
    markets: tuple[BetdaqMarketPrices, ...]
    unavailable_markets: tuple[BetdaqUnavailableMarket, ...]


def _tag(namespace: str, local: str) -> str:
    return f"{{{namespace}}}{local}"


def _safe_text(value: str, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be str")
    value = value.strip()
    if not value:
        raise BetdaqSoapProtocolError(f"{field} must be a non-empty string")
    if len(value) > _MAX_TEXT or any(not char.isprintable() for char in value):
        raise BetdaqSoapProtocolError(f"{field} contains unsafe text")
    return value


def _required_attr(element: ET.Element, name: str) -> str:
    try:
        value = element.attrib[name]
    except KeyError as exc:
        raise BetdaqSoapProtocolError(
            f"{element.tag.rsplit('}', 1)[-1]} missing required attribute {name}"
        ) from exc
    if not value:
        raise BetdaqSoapProtocolError(
            f"{element.tag.rsplit('}', 1)[-1]} has empty required attribute {name}"
        )
    return value


def _optional_attr(element: ET.Element, name: str) -> str | None:
    value = element.attrib.get(name)
    return None if value is None else value


def _integer(value: str, field: str, *, minimum: int | None = None) -> int:
    try:
        parsed = int(value, 10)
    except (TypeError, ValueError) as exc:
        raise BetdaqSoapProtocolError(f"{field} must be an integer") from exc
    if minimum is not None and parsed < minimum:
        raise BetdaqSoapProtocolError(f"{field} must be >= {minimum}")
    return parsed


def _decimal(
    value: str,
    field: str,
    *,
    strictly_positive: bool = False,
    nonnegative: bool = False,
) -> Decimal:
    try:
        parsed = Decimal(value)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BetdaqSoapProtocolError(f"{field} must be a decimal") from exc
    if not parsed.is_finite():
        raise BetdaqSoapProtocolError(f"{field} must be finite")
    if strictly_positive and parsed <= 0:
        raise BetdaqSoapProtocolError(f"{field} must be positive")
    if nonnegative and parsed < 0:
        raise BetdaqSoapProtocolError(f"{field} must be non-negative")
    return parsed


def _boolean(value: str, field: str) -> bool:
    if value in {"true", "1"}:
        return True
    if value in {"false", "0"}:
        return False
    raise BetdaqSoapProtocolError(f"{field} must be an XML Schema boolean")


def _timestamp(value: str, field: str) -> tuple[datetime, str]:
    raw = value.strip()
    candidate = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise BetdaqSoapProtocolError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetdaqSoapProtocolError(f"{field} must include a timezone")
    return parsed, raw


def _children_exact(element: ET.Element, tag: str) -> list[ET.Element]:
    return [child for child in list(element) if child.tag == tag]


def _one_child(element: ET.Element, tag: str, field: str) -> ET.Element:
    matches = _children_exact(element, tag)
    if len(matches) != 1:
        raise BetdaqSoapProtocolError(f"{field} must occur exactly once")
    return matches[0]


def _provider_created_at(
    root: ET.Element,
    soap_ns: str,
) -> tuple[datetime | None, str | None]:
    headers = _children_exact(root, _tag(soap_ns, "Header"))
    if len(headers) > 1:
        raise BetdaqSoapProtocolError("SOAP Header must occur at most once")
    if not headers:
        return None, None

    securities = _children_exact(headers[0], _tag(WSSE_NS, "Security"))
    if not securities:
        return None, None
    if len(securities) != 1:
        raise BetdaqSoapProtocolError("WS-Security node must occur at most once")

    timestamps = _children_exact(securities[0], _tag(WSU_NS, "Timestamp"))
    if not timestamps:
        return None, None
    if len(timestamps) != 1:
        raise BetdaqSoapProtocolError("WS-Security Timestamp must occur at most once")
    created = _children_exact(timestamps[0], _tag(WSU_NS, "Created"))
    if len(created) != 1 or created[0].text is None:
        raise BetdaqSoapProtocolError(
            "WS-Security Timestamp requires exactly one Created value"
        )
    return _timestamp(created[0].text, "WS-Security Created")


def _soap_fault(body: ET.Element, soap_ns: str) -> BetdaqSoapFaultError | None:
    faults = _children_exact(body, _tag(soap_ns, "Fault"))
    if not faults:
        return None
    if len(faults) != 1:
        return BetdaqSoapFaultError(code="MALFORMED_FAULT", reason="multiple Fault nodes")
    fault = faults[0]
    if soap_ns == SOAP11_NS:
        code = fault.findtext("faultcode") or "UNKNOWN"
        reason = fault.findtext("faultstring") or "unspecified SOAP fault"
    else:
        code_node = fault.find(
            f"{_tag(soap_ns, 'Code')}/{_tag(soap_ns, 'Value')}"
        )
        reason_node = fault.find(
            f"{_tag(soap_ns, 'Reason')}/{_tag(soap_ns, 'Text')}"
        )
        code = code_node.text if code_node is not None and code_node.text else "UNKNOWN"
        reason = (
            reason_node.text
            if reason_node is not None and reason_node.text
            else "unspecified SOAP fault"
        )
    return BetdaqSoapFaultError(code=code, reason=reason)


def _parse_price_level(
    element: ET.Element,
    *,
    provider_side: str,
) -> BetdaqPriceLevel | None:
    nil = element.attrib.get(_tag(XSI_NS, "nil"))
    if nil is not None:
        if nil not in {"true", "1"}:
            raise BetdaqSoapProtocolError("xsi:nil must be true when present")
        # The ASMX schema sample uses nil price nodes. A nil node carries no quote.
        if any(key not in {_tag(XSI_NS, "nil")} for key in element.attrib):
            raise BetdaqSoapProtocolError("nil price level must not carry quote attributes")
        if list(element) or (element.text and element.text.strip()):
            raise BetdaqSoapProtocolError("nil price level must be empty")
        return None

    unexpected_attributes = sorted(set(element.attrib) - {"Price", "Stake"})
    if unexpected_attributes:
        raise BetdaqSoapProtocolError(
            f"{provider_side} price level contains unexpected attribute(s): "
            + ", ".join(unexpected_attributes)
        )

    price_text = _optional_attr(element, "Price")
    stake_text = _optional_attr(element, "Stake")
    if price_text is None or stake_text is None:
        raise BetdaqSoapProtocolError(
            f"{provider_side} price level requires one exact price/stake pair"
        )

    return BetdaqPriceLevel(
        provider_side=provider_side,
        price=_decimal(
            price_text,
            f"{provider_side} price",
            strictly_positive=True,
        ),
        stake=_decimal(
            stake_text,
            f"{provider_side} stake",
            nonnegative=True,
        ),
    )


def _parse_selection(element: ET.Element) -> BetdaqSelectionPrices:
    selection_id = _integer(
        _required_attr(element, "Id"), "selection Id", minimum=0
    )
    name = _safe_text(_required_attr(element, "Name"), "selection Name")
    status_code = _integer(
        _required_attr(element, "Status"), "selection Status", minimum=0
    )
    reset_count = _integer(
        _required_attr(element, "ResetCount"), "selection ResetCount", minimum=0
    )
    deduction_raw = _optional_attr(element, "DeductionFactor")
    deduction_factor = (
        None
        if deduction_raw is None
        else _decimal(
            deduction_raw,
            "selection DeductionFactor",
            nonnegative=True,
        )
    )

    for_prices: list[BetdaqPriceLevel] = []
    against_prices: list[BetdaqPriceLevel] = []
    for child in list(element):
        if child.tag == _tag(EXTERNAL_API_NS, "ForSidePrices"):
            parsed = _parse_price_level(child, provider_side="FOR")
            if parsed is not None:
                for_prices.append(parsed)
        elif child.tag == _tag(EXTERNAL_API_NS, "AgainstSidePrices"):
            parsed = _parse_price_level(child, provider_side="AGAINST")
            if parsed is not None:
                against_prices.append(parsed)
        else:
            raise BetdaqSoapProtocolError(
                "Selections contains an unexpected child element or namespace"
            )

    def validate_provider_order(
        levels: list[BetdaqPriceLevel],
        side: str,
        *,
        descending: bool,
    ) -> None:
        seen: set[Decimal] = set()
        for level in levels:
            if level.price in seen:
                raise BetdaqSoapProtocolError(
                    f"duplicate {side} price level would double-count liquidity"
                )
            seen.add(level.price)
        prices = [level.price for level in levels]
        if prices != sorted(prices, reverse=descending):
            raise BetdaqSoapProtocolError(
                f"{side} price levels violate provider competitiveness order"
            )

    validate_provider_order(for_prices, "FOR", descending=True)
    validate_provider_order(against_prices, "AGAINST", descending=False)
    return BetdaqSelectionPrices(
        selection_id=selection_id,
        name=name,
        status_code=status_code,
        reset_count=reset_count,
        deduction_factor=deduction_factor,
        for_side_prices=tuple(for_prices),
        against_side_prices=tuple(against_prices),
    )


def _parse_market(element: ET.Element) -> BetdaqMarketPrices | BetdaqUnavailableMarket:
    return_code_raw = _optional_attr(element, "ReturnCode")
    if return_code_raw is not None:
        return_code = _integer(return_code_raw, "MarketPrices ReturnCode")
        if return_code == _RC016_MARKET_NEITHER_SUSPENDED_NOR_ACTIVE:
            market_id = _integer(
                _required_attr(element, "Id"),
                "RC016 market Id",
                minimum=0,
            )
            if list(element):
                raise BetdaqSoapProtocolError(
                    "RC016 unavailable market must not carry price children"
                )
            return BetdaqUnavailableMarket(
                market_id=market_id,
                return_code=return_code,
            )
        if return_code != 0:
            market_hint = _optional_attr(element, "Id")
            scope = (
                "market"
                if market_hint is None
                else f"market { _safe_text(market_hint, 'market Id') }"
            )
            raise BetdaqProviderStatusError(
                code=return_code,
                description="GetPrices market-level failure",
                scope=scope,
            )

    market_id = _integer(_required_attr(element, "Id"), "market Id", minimum=0)
    name = _safe_text(_required_attr(element, "Name"), "market Name")
    market_type_code = _integer(
        _required_attr(element, "Type"), "market Type", minimum=0
    )
    status_code = _integer(
        _required_attr(element, "Status"), "market Status", minimum=0
    )
    start_time, start_time_text = _timestamp(
        _required_attr(element, "StartTime"), "market StartTime"
    )
    withdrawal_sequence_number = _integer(
        _required_attr(element, "WithdrawalSequenceNumber"),
        "market WithdrawalSequenceNumber",
        minimum=0,
    )
    is_play_market = _boolean(
        _required_attr(element, "IsPlayMarket"), "market IsPlayMarket"
    )
    is_in_running_allowed = _boolean(
        _required_attr(element, "IsInRunningAllowed"),
        "market IsInRunningAllowed",
    )
    is_managed_when_in_running = _boolean(
        _required_attr(element, "IsManagedWhenInRunning"),
        "market IsManagedWhenInRunning",
    )
    is_currently_in_running = _boolean(
        _required_attr(element, "IsCurrentlyInRunning"),
        "market IsCurrentlyInRunning",
    )
    in_running_delay_seconds = _integer(
        _required_attr(element, "InRunningDelaySeconds"),
        "market InRunningDelaySeconds",
        minimum=0,
    )

    selections: list[BetdaqSelectionPrices] = []
    seen_selection_ids: set[int] = set()
    for child in list(element):
        if child.tag != _tag(EXTERNAL_API_NS, "Selections"):
            raise BetdaqSoapProtocolError(
                "MarketPrices contains an unexpected child element or namespace"
            )
        parsed = _parse_selection(child)
        if parsed.selection_id in seen_selection_ids:
            raise BetdaqSoapProtocolError(
                "duplicate selection Id in one MarketPrices response"
            )
        seen_selection_ids.add(parsed.selection_id)
        selections.append(parsed)

    return BetdaqMarketPrices(
        market_id=market_id,
        name=name,
        market_type_code=market_type_code,
        status_code=status_code,
        start_time=start_time,
        start_time_text=start_time_text,
        withdrawal_sequence_number=withdrawal_sequence_number,
        is_play_market=is_play_market,
        is_in_running_allowed=is_in_running_allowed,
        is_managed_when_in_running=is_managed_when_in_running,
        is_currently_in_running=is_currently_in_running,
        in_running_delay_seconds=in_running_delay_seconds,
        selections=tuple(selections),
    )


def parse_get_prices_response(
    xml_payload: bytes | str,
) -> BetdaqGetPricesWireResponse:
    """Parse one BETDAQ GetPrices SOAP response without minting canonical quote truth.

    This function is deliberately network-free and read-only. It preserves the
    provider-native FOR/AGAINST distinction and returns no Autosport
    ProviderQuote/MarketEvent objects. Canonical exchange-side mapping belongs to
    the separate provider adapter after the product exchange-side contract lands.
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

    provider_created_at, provider_created_at_text = _provider_created_at(
        root,
        soap_ns,
    )
    body = _one_child(root, _tag(soap_ns, "Body"), "SOAP Body")
    fault = _soap_fault(body, soap_ns)
    if fault is not None:
        raise fault

    response = _one_child(
        body,
        _tag(EXTERNAL_API_NS, "GetPricesResponse"),
        "GetPricesResponse",
    )
    result = _one_child(
        response,
        _tag(EXTERNAL_API_NS, "GetPricesResult"),
        "GetPricesResult",
    )
    return_status = _one_child(
        result,
        _tag(EXTERNAL_API_NS, "ReturnStatus"),
        "ReturnStatus",
    )
    return_code = _integer(
        _required_attr(return_status, "Code"),
        "ReturnStatus Code",
    )
    description = _safe_text(
        _required_attr(return_status, "Description"),
        "ReturnStatus Description",
    )
    call_id_raw = _optional_attr(return_status, "CallId")
    call_id = None if call_id_raw is None else _safe_text(call_id_raw, "ReturnStatus CallId")
    if return_code != 0:
        raise BetdaqProviderStatusError(
            code=return_code,
            description=description,
            call_id=call_id,
        )

    markets: list[BetdaqMarketPrices] = []
    unavailable_markets: list[BetdaqUnavailableMarket] = []
    seen_market_ids: set[int] = set()
    for child in list(result):
        if child is return_status:
            continue
        if child.tag != _tag(EXTERNAL_API_NS, "MarketPrices"):
            raise BetdaqSoapProtocolError(
                "GetPricesResult contains an unexpected child element or namespace"
            )
        market = _parse_market(child)
        if market.market_id in seen_market_ids:
            raise BetdaqSoapProtocolError(
                "duplicate market Id in one GetPrices response"
            )
        seen_market_ids.add(market.market_id)
        if isinstance(market, BetdaqUnavailableMarket):
            unavailable_markets.append(market)
        else:
            markets.append(market)

    return BetdaqGetPricesWireResponse(
        return_code=return_code,
        return_description=description,
        call_id=call_id,
        provider_created_at=provider_created_at,
        provider_created_at_text=provider_created_at_text,
        markets=tuple(markets),
        unavailable_markets=tuple(unavailable_markets),
    )

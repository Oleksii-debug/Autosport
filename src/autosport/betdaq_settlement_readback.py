"""Strict read-only BETDAQ settlement and account-posting economic evidence.

This companion intentionally composes the canonical BETDAQ authenticated read-only
client instead of creating a second credential/session or transport stack.  It
adds only three provider READ methods: GetOrderDetails, ListAccountPostings and
ListAccountPostingsById.  Provider settlement, commissions, postings and balance
facts remain observations; this module does not settle bets, infer P&L, attribute
campaign economics, or expose provider writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
import xml.etree.ElementTree as ET

from . import betdaq_account_readonly as _account
from .betdaq_account_readonly import (
    ADAPTER_ID,
    BetdaqAccountReadOnlyClient,
)

_ECONOMIC_SCHEMA = "autosport.betdaq-economic-readback-v1"
_READ_METHODS = frozenset(
    {"GetOrderDetails", "ListAccountPostings", "ListAccountPostingsById"}
)
_REQUEST_ELEMENT = {
    "GetOrderDetails": "getOrderDetailsRequest",
    "ListAccountPostings": "listAccountPostingsRequest",
    "ListAccountPostingsById": "listAccountPostingsByIdRequest",
}
_INTEGER_RE = re.compile(r"[0-9]+\Z")


class BetdaqEconomicReadbackError(RuntimeError):
    """BETDAQ settlement/posting readback contract or evidence error."""


@dataclass(frozen=True, slots=True)
class BetdaqEconomicEvidence:
    method: str
    request_identity_sha256: str
    source_payload_sha256: str
    observed_at: str
    account_context_id: str
    authenticated_principal_continuity_proven: bool = False
    physical_account_identity_proven: bool = False

    def __post_init__(self) -> None:
        if self.method not in _READ_METHODS:
            raise BetdaqEconomicReadbackError("economic evidence method is not read-only")
        _sha256_hex(self.request_identity_sha256, "request_identity_sha256")
        _sha256_hex(self.source_payload_sha256, "source_payload_sha256")
        _timestamp(self.observed_at, "observed_at")
        if (
            type(self.account_context_id) is not str
            or not self.account_context_id.startswith("betdaq-auth-context:")
        ):
            raise BetdaqEconomicReadbackError(
                "account_context_id is not canonical BETDAQ account context"
            )
        if self.authenticated_principal_continuity_proven is not False:
            raise BetdaqEconomicReadbackError(
                "cross-process authenticated-principal continuity is owned elsewhere"
            )
        if self.physical_account_identity_proven is not False:
            raise BetdaqEconomicReadbackError(
                "BETDAQ economic reads do not prove physical/legal account identity"
            )

    @property
    def evidence_id(self) -> str:
        # Product receive time is intentionally excluded, while the current canonical
        # authenticated account context remains part of identity. Cross-process continuity
        # is not claimed here; the separate durable-principal authority owns that upgrade.
        return "betdaq-economic:" + _canonical_sha256(
            {
                "schema": _ECONOMIC_SCHEMA,
                "method": self.method,
                "account_context_id": self.account_context_id,
                "request_identity_sha256": self.request_identity_sha256,
                "source_payload_sha256": self.source_payload_sha256,
            }
        )


@dataclass(frozen=True, slots=True)
class BetdaqOrderSettlementObservation:
    order_id: str
    market_id: str
    selection_id: str
    order_status_code: int
    sequence_number: int
    issued_at: str
    last_changed_at: str
    requested_stake: Decimal
    requested_price: Decimal
    total_stake: Decimal
    unmatched_stake: Decimal
    average_price: Decimal
    matching_timestamp: str
    polarity_code: int
    punter_reference_number: str
    gross_settlement_amount: Decimal | None
    order_commission: Decimal | None
    market_commission: Decimal | None
    market_settled_at: str | None
    final_settlement_proven: bool
    evidence: BetdaqEconomicEvidence

    def __post_init__(self) -> None:
        for field in ("order_id", "market_id", "selection_id", "punter_reference_number"):
            _provider_id(getattr(self, field), field)
        for field in ("order_status_code", "sequence_number", "polarity_code"):
            _nonnegative_int(getattr(self, field), field)
        for field in ("issued_at", "last_changed_at", "matching_timestamp"):
            _timestamp(getattr(self, field), field)
        for field in (
            "requested_stake",
            "requested_price",
            "total_stake",
            "unmatched_stake",
            "average_price",
        ):
            _finite_decimal(getattr(self, field), field)
        for field in (
            "gross_settlement_amount",
            "order_commission",
            "market_commission",
        ):
            value = getattr(self, field)
            if value is not None:
                _finite_decimal(value, field)
        if self.market_settled_at is not None:
            _timestamp(self.market_settled_at, "market_settled_at")
        expected_final = (
            self.order_status_code in {4, 5}
            and self.gross_settlement_amount is not None
            and self.order_commission is not None
            and self.market_commission is not None
            and self.market_settled_at is not None
        )
        if self.final_settlement_proven is not expected_final:
            raise BetdaqEconomicReadbackError(
                "final_settlement_proven does not match exact provider settlement evidence"
            )

    def canonical_dict(self) -> dict[str, object]:
        return {
            "order_id": self.order_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "order_status_code": self.order_status_code,
            "sequence_number": self.sequence_number,
            "issued_at": self.issued_at,
            "last_changed_at": self.last_changed_at,
            "requested_stake": _decimal_text(self.requested_stake),
            "requested_price": _decimal_text(self.requested_price),
            "total_stake": _decimal_text(self.total_stake),
            "unmatched_stake": _decimal_text(self.unmatched_stake),
            "average_price": _decimal_text(self.average_price),
            "matching_timestamp": self.matching_timestamp,
            "polarity_code": self.polarity_code,
            "punter_reference_number": self.punter_reference_number,
            "gross_settlement_amount": _optional_decimal_text(
                self.gross_settlement_amount
            ),
            "order_commission": _optional_decimal_text(self.order_commission),
            "market_commission": _optional_decimal_text(self.market_commission),
            "market_settled_at": self.market_settled_at,
            "final_settlement_proven": self.final_settlement_proven,
            "evidence_id": self.evidence.evidence_id,
        }

    @property
    def observation_id(self) -> str:
        return "betdaq-order-settlement:" + _canonical_sha256(self.canonical_dict())


@dataclass(frozen=True, slots=True)
class BetdaqPostingObservation:
    posted_at: str
    description: str
    amount: Decimal
    resulting_balance: Decimal
    posting_category: int
    order_id: str | None
    market_id: str | None
    transaction_id: str
    evidence: BetdaqEconomicEvidence

    def __post_init__(self) -> None:
        _timestamp(self.posted_at, "posted_at")
        if type(self.description) is not str:
            raise BetdaqEconomicReadbackError("description must be text")
        _finite_decimal(self.amount, "amount")
        _finite_decimal(self.resulting_balance, "resulting_balance")
        _nonnegative_int(self.posting_category, "posting_category")
        if self.order_id is not None:
            _provider_id(self.order_id, "order_id")
        if self.market_id is not None:
            _provider_id(self.market_id, "market_id")
        _provider_id(self.transaction_id, "transaction_id")

    def canonical_dict(self) -> dict[str, object]:
        return {
            "posted_at": self.posted_at,
            "description": self.description,
            "amount": _decimal_text(self.amount),
            "resulting_balance": _decimal_text(self.resulting_balance),
            "posting_category": self.posting_category,
            "order_id": self.order_id,
            "market_id": self.market_id,
            "transaction_id": self.transaction_id,
        }

    @property
    def observation_id(self) -> str:
        return "betdaq-posting:" + _canonical_sha256(self.canonical_dict())


@dataclass(frozen=True, slots=True)
class BetdaqPostingsReadback:
    method: str
    query_start_at: str | None
    query_end_at: str | None
    query_transaction_id: str | None
    currency: str
    available_funds: Decimal
    balance: Decimal
    credit: Decimal
    exposure: Decimal
    window_complete: bool | None
    postings: tuple[BetdaqPostingObservation, ...]
    evidence: BetdaqEconomicEvidence

    def __post_init__(self) -> None:
        if self.method not in {"ListAccountPostings", "ListAccountPostingsById"}:
            raise BetdaqEconomicReadbackError("invalid postings readback method")
        if (
            type(self.currency) is not str
            or not self.currency
            or self.currency != self.currency.strip()
        ):
            raise BetdaqEconomicReadbackError("currency must be non-empty trimmed text")
        for field in ("available_funds", "balance", "credit", "exposure"):
            _finite_decimal(getattr(self, field), field)
        if self.method == "ListAccountPostings":
            if self.query_start_at is None or self.query_end_at is None:
                raise BetdaqEconomicReadbackError("window read requires exact bounds")
            _timestamp(self.query_start_at, "query_start_at")
            _timestamp(self.query_end_at, "query_end_at")
            if type(self.window_complete) is not bool:
                raise BetdaqEconomicReadbackError(
                    "window completeness must come from provider boolean"
                )
            if self.query_transaction_id is not None:
                raise BetdaqEconomicReadbackError(
                    "window read cannot claim transaction-id query"
                )
        else:
            if self.query_transaction_id is None:
                raise BetdaqEconomicReadbackError("ById read requires transaction id")
            _provider_id(self.query_transaction_id, "query_transaction_id")
            if self.window_complete is not None:
                raise BetdaqEconomicReadbackError(
                    "ById response cannot claim time-window completeness"
                )
            if self.query_start_at is not None or self.query_end_at is not None:
                raise BetdaqEconomicReadbackError(
                    "ById read cannot claim bounded-window authority"
                )
        seen: dict[str, dict[str, object]] = {}
        for posting in self.postings:
            if not isinstance(posting, BetdaqPostingObservation):
                raise BetdaqEconomicReadbackError("postings contain invalid observation")
            canonical = posting.canonical_dict()
            previous = seen.get(posting.transaction_id)
            if previous is not None and previous != canonical:
                raise BetdaqEconomicReadbackError(
                    "same BETDAQ transaction id has conflicting economic content"
                )
            seen[posting.transaction_id] = canonical

    @property
    def readback_id(self) -> str:
        return "betdaq-postings:" + _canonical_sha256(
            {
                "method": self.method,
                "query_start_at": self.query_start_at,
                "query_end_at": self.query_end_at,
                "query_transaction_id": self.query_transaction_id,
                "currency": self.currency,
                "available_funds": _decimal_text(self.available_funds),
                "balance": _decimal_text(self.balance),
                "credit": _decimal_text(self.credit),
                "exposure": _decimal_text(self.exposure),
                "window_complete": self.window_complete,
                "postings": [item.canonical_dict() for item in self.postings],
                "evidence_id": self.evidence.evidence_id,
            }
        )


class BetdaqEconomicReadbackClient:
    """Economic READ companion bound to one canonical authenticated BETDAQ client."""

    def __init__(self, account_client: BetdaqAccountReadOnlyClient) -> None:
        if type(account_client) is not BetdaqAccountReadOnlyClient:
            raise TypeError("account_client must be canonical BetdaqAccountReadOnlyClient")
        self._account_client = account_client

    def __repr__(self) -> str:
        return f"{type(self).__name__}(adapter_id={ADAPTER_ID!r})"

    def read_order_details(self, order_id: int | str) -> BetdaqOrderSettlementObservation:
        order = _provider_id(order_id, "order_id")
        result, evidence = self._call("GetOrderDetails", {"OrderId": order})
        settlement_nodes = [
            child
            for child in result
            if child.tag == f"{{{_account._EXTERNAL_NS}}}OrderSettlementInformation"
        ]
        if len(settlement_nodes) > 1:
            raise BetdaqEconomicReadbackError(
                "GetOrderDetails has duplicate OrderSettlementInformation"
            )
        settlement = settlement_nodes[0] if settlement_nodes else None
        gross = (
            None
            if settlement is None
            else _optional_decimal_attr(settlement, "GrossSettlementAmount")
        )
        order_commission = (
            None
            if settlement is None
            else _optional_decimal_attr(settlement, "OrderCommission")
        )
        market_commission = (
            None
            if settlement is None
            else _optional_decimal_attr(settlement, "MarketCommission")
        )
        market_settled_at = (
            None
            if settlement is None
            else _optional_timestamp_attr(settlement, "MarketSettledDate")
        )
        status = _integer_attr(result, "OrderStatus")
        final = (
            status in {4, 5}
            and gross is not None
            and order_commission is not None
            and market_commission is not None
            and market_settled_at is not None
        )
        return BetdaqOrderSettlementObservation(
            order_id=order,
            market_id=_provider_id(_required_attr(result, "MarketId"), "MarketId"),
            selection_id=_provider_id(
                _required_attr(result, "SelectionId"), "SelectionId"
            ),
            order_status_code=status,
            sequence_number=_integer_attr(result, "SequenceNumber"),
            issued_at=_timestamp(_required_attr(result, "IssuedAt"), "IssuedAt"),
            last_changed_at=_timestamp(
                _required_attr(result, "LastChangedAt"), "LastChangedAt"
            ),
            requested_stake=_decimal_attr(result, "RequestedStake"),
            requested_price=_decimal_attr(result, "RequestedPrice"),
            total_stake=_decimal_attr(result, "TotalStake"),
            unmatched_stake=_decimal_attr(result, "UnmatchedStake"),
            average_price=_decimal_attr(result, "AveragePrice"),
            matching_timestamp=_timestamp(
                _required_attr(result, "MatchingTimeStamp"), "MatchingTimeStamp"
            ),
            polarity_code=_integer_attr(result, "Polarity"),
            punter_reference_number=_provider_id(
                _required_attr(result, "PunterReferenceNumber"),
                "PunterReferenceNumber",
            ),
            gross_settlement_amount=gross,
            order_commission=order_commission,
            market_commission=market_commission,
            market_settled_at=market_settled_at,
            final_settlement_proven=final,
            evidence=evidence,
        )

    def read_account_postings(
        self, start_at: datetime, end_at: datetime
    ) -> BetdaqPostingsReadback:
        start = _request_time(start_at, "start_at")
        end = _request_time(end_at, "end_at")
        if start_at.astimezone(timezone.utc) >= end_at.astimezone(timezone.utc):
            raise BetdaqEconomicReadbackError("postings window start must precede end")
        result, evidence = self._call(
            "ListAccountPostings", {"StartTime": start, "EndTime": end}
        )
        return _parse_postings_result(
            result,
            evidence=evidence,
            method="ListAccountPostings",
            query_start_at=start,
            query_end_at=end,
            query_transaction_id=None,
            window_complete=_boolean_attr(result, "HaveAllPostingsBeenReturned"),
        )

    def read_account_postings_by_id(
        self, transaction_id: int | str
    ) -> BetdaqPostingsReadback:
        transaction = _provider_id(transaction_id, "transaction_id")
        result, evidence = self._call(
            "ListAccountPostingsById", {"TransactionId": transaction}
        )
        # Provider contract has no HaveAllPostingsBeenReturned here. Existence of a
        # row can be authoritative for that row; sibling/window absence stays unknown.
        if "HaveAllPostingsBeenReturned" in result.attrib:
            raise BetdaqEconomicReadbackError(
                "ById response unexpectedly tries to mint window completeness"
            )
        return _parse_postings_result(
            result,
            evidence=evidence,
            method="ListAccountPostingsById",
            query_start_at=None,
            query_end_at=None,
            query_transaction_id=transaction,
            window_complete=None,
        )

    def _call(
        self, method: str, request_attributes: dict[str, str]
    ) -> tuple[ET.Element, BetdaqEconomicEvidence]:
        if method not in _READ_METHODS:
            raise BetdaqEconomicReadbackError("method is outside economic READ allowlist")
        client = self._account_client
        try:
            _account._require_canonical_account_transport(client._transport)
        except Exception:
            raise BetdaqEconomicReadbackError(
                "canonical BETDAQ economic evidence requires product-owned HTTPS transport"
            ) from None
        request_identity = _canonical_sha256(
            {
                "schema": _ECONOMIC_SCHEMA,
                "method": method,
                "attributes": dict(sorted(request_attributes.items())),
            }
        )
        body = _request_xml(client, method, request_attributes)
        headers = {
            "Accept": "text/xml",
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"{_account._EXTERNAL_NS}{method}"',
        }
        with client._call_lock:
            try:
                payload = client._transport.post(
                    _account._SECURE_ENDPOINT,
                    headers=headers,
                    body=body,
                    timeout_seconds=client._timeout_seconds,
                )
            except Exception:
                raise BetdaqEconomicReadbackError(
                    "BETDAQ economic read transport failed"
                ) from None
        if type(payload) is not bytes:
            raise BetdaqEconomicReadbackError(
                "BETDAQ economic read transport must return bytes"
            )
        try:
            result = _parse_economic_soap_result(payload, method)
            context = _account._authenticated_account_context(
                client._credentials, client._venue_id
            )
            observed_at = client._observed_at()
        except Exception:
            # Provider/parser diagnostics are normalized. Never expose payload,
            # credentials or transport exception text from a secure request.
            raise BetdaqEconomicReadbackError(
                "BETDAQ economic response failed canonical validation"
            ) from None
        evidence = BetdaqEconomicEvidence(
            method=method,
            request_identity_sha256=request_identity,
            source_payload_sha256=sha256(payload).hexdigest(),
            observed_at=observed_at,
            account_context_id=context.session_context_id,
        )
        return result, evidence


def _request_xml(
    client: BetdaqAccountReadOnlyClient,
    method: str,
    attributes: dict[str, str],
) -> bytes:
    ET.register_namespace("soap", _account._SOAP11_NS)
    envelope = ET.Element(f"{{{_account._SOAP11_NS}}}Envelope")
    header = ET.SubElement(envelope, f"{{{_account._SOAP11_NS}}}Header")
    credentials = client._credentials
    ET.SubElement(
        header,
        f"{{{_account._EXTERNAL_NS}}}ExternalApiHeader",
        {
            "version": credentials.version,
            "languageCode": credentials.language_code,
            "username": credentials.username,
            "password": credentials.password,
            "applicationIdentifier": credentials.application_identifier,
        },
    )
    body = ET.SubElement(envelope, f"{{{_account._SOAP11_NS}}}Body")
    method_element = ET.SubElement(body, f"{{{_account._EXTERNAL_NS}}}{method}")
    request_name = _REQUEST_ELEMENT[method]
    ET.SubElement(
        method_element,
        f"{{{_account._EXTERNAL_NS}}}{request_name}",
        attributes,
    )
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


def _parse_economic_soap_result(payload: bytes, method: str) -> ET.Element:
    """Parse generated BETDAQ SOAP results without inventing ReturnStatus.

    Current generated SecureService examples for these methods contain the method
    Result directly and no ReturnStatus child. If a provider deployment does include
    ReturnStatus, it is only an additional failure gate: exactly one integer Code=0
    is accepted; nonzero, duplicate, or malformed status fails closed.
    """

    upper = payload.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise BetdaqEconomicReadbackError(
            "BETDAQ economic SOAP payload contains forbidden DTD/entity"
        )
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, UnicodeError):
        raise BetdaqEconomicReadbackError(
            "BETDAQ economic response is not valid SOAP XML"
        ) from None
    namespace, local = _split_tag(root.tag)
    if local != "Envelope" or namespace not in {
        _account._SOAP11_NS,
        _account._SOAP12_NS,
    }:
        raise BetdaqEconomicReadbackError(
            "BETDAQ economic response has invalid SOAP Envelope"
        )
    bodies = [child for child in root if child.tag == f"{{{namespace}}}Body"]
    if len(bodies) != 1:
        raise BetdaqEconomicReadbackError(
            "BETDAQ economic response must contain one SOAP Body"
        )
    body = bodies[0]
    for child in body:
        child_namespace, child_local = _split_tag(child.tag)
        if child_namespace == namespace and child_local == "Fault":
            raise BetdaqEconomicReadbackError("BETDAQ economic SOAP Fault")
    responses = [
        child
        for child in body
        if child.tag == f"{{{_account._EXTERNAL_NS}}}{method}Response"
    ]
    if len(responses) != 1:
        raise BetdaqEconomicReadbackError(
            f"BETDAQ economic response is missing exact {method}Response"
        )
    results = [
        child
        for child in responses[0]
        if child.tag == f"{{{_account._EXTERNAL_NS}}}{method}Result"
    ]
    if len(results) != 1:
        raise BetdaqEconomicReadbackError(
            f"BETDAQ economic response is missing exact {method}Result"
        )
    result = results[0]
    statuses = [
        child
        for child in result
        if child.tag == f"{{{_account._EXTERNAL_NS}}}ReturnStatus"
    ]
    if len(statuses) > 1:
        raise BetdaqEconomicReadbackError(
            "BETDAQ economic result has duplicate ReturnStatus"
        )
    if statuses:
        raw_code = statuses[0].attrib.get("Code")
        if (
            type(raw_code) is not str
            or raw_code != raw_code.strip()
            or not raw_code
            or not raw_code.lstrip("-").isdigit()
        ):
            raise BetdaqEconomicReadbackError(
                "BETDAQ economic ReturnStatus Code must be provider integer text"
            )
        if int(raw_code) != 0:
            raise BetdaqEconomicReadbackError(
                f"BETDAQ economic ReturnStatus reported failure code {int(raw_code)}"
            )
    return result


def _split_tag(tag: str) -> tuple[str, str]:
    if type(tag) is not str:
        raise BetdaqEconomicReadbackError("BETDAQ XML tag must be text")
    if tag.startswith("{"):
        if "}" not in tag:
            raise BetdaqEconomicReadbackError("BETDAQ XML namespace is malformed")
        namespace, local = tag[1:].split("}", 1)
        return namespace, local
    return "", tag


def _parse_postings_result(
    result: ET.Element,
    *,
    evidence: BetdaqEconomicEvidence,
    method: str,
    query_start_at: str | None,
    query_end_at: str | None,
    query_transaction_id: str | None,
    window_complete: bool | None,
) -> BetdaqPostingsReadback:
    containers = [
        child
        for child in result
        if child.tag == f"{{{_account._EXTERNAL_NS}}}Orders"
    ]
    if len(containers) != 1:
        raise BetdaqEconomicReadbackError(
            "BETDAQ postings result must contain exactly one Orders element"
        )
    deduped: dict[str, BetdaqPostingObservation] = {}
    ordered_ids: list[str] = []
    for child in containers[0]:
        if child.tag != f"{{{_account._EXTERNAL_NS}}}Order":
            raise BetdaqEconomicReadbackError(
                "BETDAQ postings Orders contains unexpected element"
            )
        transaction_id = _provider_id(
            _required_attr(child, "TransactionId"), "TransactionId"
        )
        posting = BetdaqPostingObservation(
            posted_at=_timestamp(_required_attr(child, "PostedAt"), "PostedAt"),
            description=_required_attr(child, "Description", allow_empty=True),
            amount=_decimal_attr(child, "Amount"),
            resulting_balance=_decimal_attr(child, "ResultingBalance"),
            posting_category=_integer_attr(child, "PostingCategory"),
            order_id=_optional_provider_attr(child, "OrderId"),
            market_id=_optional_provider_attr(child, "MarketId"),
            transaction_id=transaction_id,
            evidence=evidence,
        )
        previous = deduped.get(transaction_id)
        if previous is not None:
            if previous.canonical_dict() != posting.canonical_dict():
                raise BetdaqEconomicReadbackError(
                    "same BETDAQ transaction id has conflicting economic content"
                )
            continue
        deduped[transaction_id] = posting
        ordered_ids.append(transaction_id)
    return BetdaqPostingsReadback(
        method=method,
        query_start_at=query_start_at,
        query_end_at=query_end_at,
        query_transaction_id=query_transaction_id,
        currency=_required_attr(result, "Currency"),
        available_funds=_decimal_attr(result, "AvailableFunds"),
        balance=_decimal_attr(result, "Balance"),
        credit=_decimal_attr(result, "Credit"),
        exposure=_decimal_attr(result, "Exposure"),
        window_complete=window_complete,
        postings=tuple(deduped[item] for item in ordered_ids),
        evidence=evidence,
    )


def _required_attr(
    element: ET.Element, name: str, *, allow_empty: bool = False
) -> str:
    value = element.attrib.get(name)
    if type(value) is not str or value != value.strip():
        raise BetdaqEconomicReadbackError(f"{name} must be trimmed provider text")
    if not allow_empty and not value:
        raise BetdaqEconomicReadbackError(f"{name} is required")
    return value


def _optional_provider_attr(element: ET.Element, name: str) -> str | None:
    raw = element.attrib.get(name)
    if raw is None:
        return None
    return _provider_id(raw, name)


def _provider_id(value: int | str, field: str) -> str:
    if type(value) is int:
        if value < 0:
            raise BetdaqEconomicReadbackError(f"{field} must be non-negative")
        return str(value)
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or _INTEGER_RE.fullmatch(value) is None
    ):
        raise BetdaqEconomicReadbackError(
            f"{field} must be canonical non-negative provider integer text"
        )
    if len(value) > 1 and value.startswith("0"):
        raise BetdaqEconomicReadbackError(f"{field} must not contain leading zeroes")
    return value


def _integer_attr(element: ET.Element, name: str) -> int:
    return int(_provider_id(_required_attr(element, name), name))


def _decimal_attr(element: ET.Element, name: str) -> Decimal:
    raw = _required_attr(element, name)
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise BetdaqEconomicReadbackError(f"{name} is not provider Decimal text") from None
    return _finite_decimal(value, name)


def _optional_decimal_attr(element: ET.Element, name: str) -> Decimal | None:
    raw = element.attrib.get(name)
    if raw is None:
        return None
    if type(raw) is not str or not raw or raw != raw.strip():
        raise BetdaqEconomicReadbackError(f"{name} must be provider Decimal text")
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise BetdaqEconomicReadbackError(f"{name} is not provider Decimal text") from None
    return _finite_decimal(value, name)


def _finite_decimal(value: Decimal, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise BetdaqEconomicReadbackError(f"{field} must be a finite Decimal")
    return value


def _boolean_attr(element: ET.Element, name: str) -> bool:
    raw = _required_attr(element, name)
    if raw == "true":
        return True
    if raw == "false":
        return False
    raise BetdaqEconomicReadbackError(f"{name} must be exact provider boolean text")


def _timestamp(value: str, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BetdaqEconomicReadbackError(f"{field} must be trimmed timestamp text")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        raise BetdaqEconomicReadbackError(f"{field} must be ISO-8601") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetdaqEconomicReadbackError(f"{field} must include timezone offset")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _optional_timestamp_attr(element: ET.Element, name: str) -> str | None:
    raw = element.attrib.get(name)
    if raw is None:
        return None
    return _timestamp(raw, name)


def _request_time(value: datetime, field: str) -> str:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise BetdaqEconomicReadbackError(
            f"{field} must be a timezone-aware datetime"
        )
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _nonnegative_int(value: int, field: str) -> int:
    if type(value) is not int or value < 0:
        raise BetdaqEconomicReadbackError(f"{field} must be a non-negative integer")
    return value


def _sha256_hex(value: str, field: str) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise BetdaqEconomicReadbackError(f"{field} must be lowercase SHA-256 hex")
    return value


def _decimal_text(value: Decimal) -> str:
    value = _finite_decimal(value, "decimal")
    sign, digits, exponent = value.as_tuple()
    raw_digits = list(digits)
    if not any(raw_digits):
        return "0"
    while raw_digits[-1] == 0:
        raw_digits.pop()
        exponent += 1
    coefficient = "".join(str(digit) for digit in raw_digits)
    if exponent >= 0:
        text = coefficient + ("0" * exponent)
    else:
        point = len(coefficient) + exponent
        if point > 0:
            text = f"{coefficient[:point]}.{coefficient[point:]}"
        else:
            text = f"0.{('0' * -point)}{coefficient}"
    return f"-{text}" if sign else text


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else _decimal_text(value)


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    return sha256(encoded).hexdigest()

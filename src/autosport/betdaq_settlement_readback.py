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
    BetdaqAccountReadOnlyError,
)

_ECONOMIC_SCHEMA = "autosport.betdaq-economic-readback-v1"
_INTEGER_RE = re.compile(r"[0-9]+\Z")
_DECIMAL_RE = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)\Z")
_CANONICAL_ACCOUNT_HTTPS_POST = _account._CANONICAL_HTTPS_POST
_REQUIRE_CANONICAL_ACCOUNT_TRANSPORT = _account._require_canonical_account_transport
_CANONICAL_SECURE_ENDPOINT = _account._SECURE_ENDPOINT
_CANONICAL_EXTERNAL_NS = _account._EXTERNAL_NS
_CANONICAL_SOAP11_NS = _account._SOAP11_NS
_CANONICAL_SOAP12_NS = _account._SOAP12_NS
_CANONICAL_ACCOUNT_PROTOCOL_AUTHORITY = _account._canonical_betdaq_protocol_authority
_CANONICAL_ACCOUNT_PROTOCOL_AUTHORITY_CODE = getattr(
    _CANONICAL_ACCOUNT_PROTOCOL_AUTHORITY,
    "__code__",
    None,
)
_CANONICAL_ACCOUNT_CURRENCY = _account._currency
_CANONICAL_ACCOUNT_CURRENCY_CODE = getattr(_CANONICAL_ACCOUNT_CURRENCY, "__code__", None)
_CANONICAL_ACCOUNT_CONTEXT_DISPATCH = _account._canonical_account_context_dispatch
_CANONICAL_ACCOUNT_CONTEXT_DISPATCH_CODE = getattr(
    _CANONICAL_ACCOUNT_CONTEXT_DISPATCH,
    "__code__",
    None,
)
_CANONICAL_AUTHENTICATED_ACCOUNT_CONTEXT = _account._authenticated_account_context
_CANONICAL_AUTHENTICATED_ACCOUNT_CONTEXT_CODE = getattr(
    _CANONICAL_AUTHENTICATED_ACCOUNT_CONTEXT,
    "__code__",
    None,
)
_CANONICAL_AUTHENTICATED_ACCOUNT_CONTEXT_TYPE = _account.BetdaqAuthenticatedAccountContext
_MAX_XSD_LONG = 9_223_372_036_854_775_807
_CANONICAL_ACCOUNT_HTTPS_POST_CODE = getattr(_CANONICAL_ACCOUNT_HTTPS_POST, "__code__", None)
_CANONICAL_REQUIRE_ACCOUNT_TRANSPORT_CODE = getattr(
    _REQUIRE_CANONICAL_ACCOUNT_TRANSPORT,
    "__code__",
    None,
)
_TERMINAL_ORDER_STATUS_CODES = _account._TERMINAL_STATUS_CODES


def _product_receive_time(
    _datetime=datetime,
    _utc=timezone.utc,
) -> datetime:
    """Return the product-owned wall-clock instant for one provider acquisition."""

    return _datetime.now(_utc)


_PRODUCT_RECEIVE_TIME = _product_receive_time
_PRODUCT_RECEIVE_TIME_CODE = getattr(_PRODUCT_RECEIVE_TIME, "__code__", None)
_PRODUCT_RECEIVE_TIME_DEFAULTS = _PRODUCT_RECEIVE_TIME.__defaults__


class BetdaqEconomicReadbackError(RuntimeError):
    """BETDAQ settlement/posting readback contract or evidence error."""


def _canonical_product_receive_clock(
    *,
    _clock=_PRODUCT_RECEIVE_TIME,
    _clock_code=_PRODUCT_RECEIVE_TIME_CODE,
    _clock_defaults=_PRODUCT_RECEIVE_TIME_DEFAULTS,
):
    """Resolve the exact product-owned receive clock without caller clock trust."""

    live_clock = globals().get("_product_receive_time")
    aliases = (
        globals().get("_PRODUCT_RECEIVE_TIME"),
        globals().get("_PRODUCT_RECEIVE_TIME_CODE"),
        globals().get("_PRODUCT_RECEIVE_TIME_DEFAULTS"),
    )
    expected_aliases = (_clock, _clock_code, _clock_defaults)
    if (
        live_clock is not _clock
        or getattr(live_clock, "__code__", None) is not _clock_code
        or getattr(_clock, "__code__", None) is not _clock_code
        or _clock.__defaults__ is not _clock_defaults
        or aliases != expected_aliases
    ):
        raise BetdaqEconomicReadbackError(
            "canonical BETDAQ economic product clock authority was replaced"
        )
    return _clock


def _canonical_economic_protocol_authority(
    *,
    _account_protocol=_CANONICAL_ACCOUNT_PROTOCOL_AUTHORITY,
    _account_protocol_code=_CANONICAL_ACCOUNT_PROTOCOL_AUTHORITY_CODE,
    _expected=(
        _CANONICAL_SECURE_ENDPOINT,
        _CANONICAL_EXTERNAL_NS,
        _CANONICAL_SOAP11_NS,
        _CANONICAL_SOAP12_NS,
    ),
) -> tuple[str, str, str, str]:
    """Reuse the shared account wire authority with captured local anchors."""

    live_account_protocol = getattr(
        _account,
        "_canonical_betdaq_protocol_authority",
        None,
    )
    aliases = (
        globals().get("_CANONICAL_SECURE_ENDPOINT"),
        globals().get("_CANONICAL_EXTERNAL_NS"),
        globals().get("_CANONICAL_SOAP11_NS"),
        globals().get("_CANONICAL_SOAP12_NS"),
    )
    if (
        live_account_protocol is not _account_protocol
        or getattr(live_account_protocol, "__code__", None)
        is not _account_protocol_code
        or globals().get("_CANONICAL_ACCOUNT_PROTOCOL_AUTHORITY")
        is not _account_protocol
        or globals().get("_CANONICAL_ACCOUNT_PROTOCOL_AUTHORITY_CODE")
        is not _account_protocol_code
        or aliases != _expected
    ):
        raise BetdaqEconomicReadbackError(
            "canonical BETDAQ economic protocol authority was replaced"
        )
    try:
        live = _account_protocol()
    except BetdaqAccountReadOnlyError:
        raise BetdaqEconomicReadbackError(
            "canonical BETDAQ economic protocol authority was replaced"
        ) from None
    if live != _expected:
        raise BetdaqEconomicReadbackError(
            "canonical BETDAQ economic protocol authority was replaced"
        )
    return _expected


def _canonical_economic_transport_dispatch(
    *,
    _post=_CANONICAL_ACCOUNT_HTTPS_POST,
    _post_code=_CANONICAL_ACCOUNT_HTTPS_POST_CODE,
    _require=_REQUIRE_CANONICAL_ACCOUNT_TRANSPORT,
    _require_code=_CANONICAL_REQUIRE_ACCOUNT_TRANSPORT_CODE,
    _transport_type=_account.UrllibBetdaqSoapTransport,
):
    """Return captured shared transport callables after exact live validation."""

    live_post = globals().get("_CANONICAL_ACCOUNT_HTTPS_POST")
    live_require = globals().get("_REQUIRE_CANONICAL_ACCOUNT_TRANSPORT")
    account_post = getattr(_account, "_CANONICAL_HTTPS_POST", None)
    account_require = getattr(_account, "_require_canonical_account_transport", None)
    class_post = vars(_transport_type).get("post")
    if (
        live_post is not _post
        or live_require is not _require
        or account_post is not _post
        or account_require is not _require
        or class_post is not _post
        or getattr(_post, "__code__", None) is not _post_code
        or getattr(_require, "__code__", None) is not _require_code
        or globals().get("_CANONICAL_ACCOUNT_HTTPS_POST_CODE") is not _post_code
        or globals().get("_CANONICAL_REQUIRE_ACCOUNT_TRANSPORT_CODE")
        is not _require_code
    ):
        raise BetdaqEconomicReadbackError(
            "canonical BETDAQ economic transport dispatch was replaced"
        )
    return _require, _post


def _canonical_authenticated_account_context_dispatch(
    *,
    _dispatch=_CANONICAL_ACCOUNT_CONTEXT_DISPATCH,
    _dispatch_code=_CANONICAL_ACCOUNT_CONTEXT_DISPATCH_CODE,
    _resolver=_CANONICAL_AUTHENTICATED_ACCOUNT_CONTEXT,
    _resolver_code=_CANONICAL_AUTHENTICATED_ACCOUNT_CONTEXT_CODE,
    _context_type=_CANONICAL_AUTHENTICATED_ACCOUNT_CONTEXT_TYPE,
):
    """Reuse captured shared account-context authority without alias trust."""

    live_dispatch = getattr(_account, "_canonical_account_context_dispatch", None)
    local_aliases = (
        globals().get("_CANONICAL_ACCOUNT_CONTEXT_DISPATCH"),
        globals().get("_CANONICAL_ACCOUNT_CONTEXT_DISPATCH_CODE"),
        globals().get("_CANONICAL_AUTHENTICATED_ACCOUNT_CONTEXT"),
        globals().get("_CANONICAL_AUTHENTICATED_ACCOUNT_CONTEXT_CODE"),
        globals().get("_CANONICAL_AUTHENTICATED_ACCOUNT_CONTEXT_TYPE"),
    )
    expected_aliases = (
        _dispatch,
        _dispatch_code,
        _resolver,
        _resolver_code,
        _context_type,
    )
    if (
        live_dispatch is not _dispatch
        or getattr(live_dispatch, "__code__", None) is not _dispatch_code
        or local_aliases != expected_aliases
    ):
        raise BetdaqEconomicReadbackError(
            "canonical BETDAQ authenticated account context authority was replaced"
        )
    try:
        live_resolver = _dispatch()
    except BetdaqAccountReadOnlyError:
        raise BetdaqEconomicReadbackError(
            "canonical BETDAQ authenticated account context authority was replaced"
        ) from None
    live_type = getattr(_account, "BetdaqAuthenticatedAccountContext", None)
    if (
        live_resolver is not _resolver
        or getattr(live_resolver, "__code__", None) is not _resolver_code
        or live_type is not _context_type
    ):
        raise BetdaqEconomicReadbackError(
            "canonical BETDAQ authenticated account context authority was replaced"
        )
    return _resolver


def _canonical_terminal_order_status_codes(
    *,
    _expected=_TERMINAL_ORDER_STATUS_CODES,
) -> frozenset[int]:
    """Resolve the captured existing account terminal-order authority."""

    live_codes = globals().get("_TERMINAL_ORDER_STATUS_CODES")
    account_codes = getattr(_account, "_TERMINAL_STATUS_CODES", None)
    if (
        live_codes is not _expected
        or account_codes is not _expected
        or type(_expected) is not frozenset
    ):
        raise BetdaqEconomicReadbackError(
            "canonical BETDAQ terminal order status authority was replaced"
        )
    return _expected


def _economic_request_identity(method: str, attributes: dict[str, str]) -> str:
    """Bind an economic claim to the exact provider READ request that produced it."""

    if method not in ("GetOrderDetails", "ListAccountPostings", "ListAccountPostingsById"):
        raise BetdaqEconomicReadbackError(
            "method is outside economic READ request-identity allowlist"
        )
    if type(attributes) is not dict:
        raise BetdaqEconomicReadbackError(
            "economic request identity attributes must be an exact dict"
        )
    canonical_attributes: dict[str, str] = {}
    for key, value in attributes.items():
        if (
            type(key) is not str
            or not key
            or type(value) is not str
            or value != value.strip()
        ):
            raise BetdaqEconomicReadbackError(
                "economic request identity attributes are not canonical text"
            )
        canonical_attributes[key] = value
    return _canonical_sha256(
        {
            "schema": _ECONOMIC_SCHEMA,
            "method": method,
            "attributes": dict(sorted(canonical_attributes.items())),
        }
    )


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
        if self.method not in ("GetOrderDetails", "ListAccountPostings", "ListAccountPostingsById"):
            raise BetdaqEconomicReadbackError("economic evidence method is not read-only")
        _sha256_hex(self.request_identity_sha256, "request_identity_sha256")
        _sha256_hex(self.source_payload_sha256, "source_payload_sha256")
        if _timestamp(self.observed_at, "observed_at") != self.observed_at:
            raise BetdaqEconomicReadbackError(
                "observed_at must use canonical UTC timestamp spelling"
            )
        context_prefix = "betdaq-auth-context:"
        if (
            type(self.account_context_id) is not str
            or not self.account_context_id.startswith(context_prefix)
        ):
            raise BetdaqEconomicReadbackError(
                "account_context_id is not canonical BETDAQ account context"
            )
        try:
            _sha256_hex(
                self.account_context_id.removeprefix(context_prefix),
                "account_context_id",
            )
        except BetdaqEconomicReadbackError as exc:
            raise BetdaqEconomicReadbackError(
                "account_context_id must bind an exact canonical context digest"
            ) from exc
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

    @property
    def acquisition_id(self) -> str:
        """Identify one product acquisition without changing replay-stable economics.

        evidence_id identifies the exact authenticated request/provider payload
        independent of when Autosport re-read it. Causal consumers also need an
        identity for the product receive/availability observation itself; bind that
        separately so a later exact replay cannot masquerade as the earlier acquisition.
        """
        return "betdaq-economic-acquisition:" + _canonical_sha256(
            {
                "schema": _ECONOMIC_SCHEMA,
                "evidence_id": self.evidence_id,
                "observed_at": self.observed_at,
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
    currency: str | None
    denomination_proven: bool
    scalar_economic_use_proven: bool
    final_settlement_proven: bool
    evidence: BetdaqEconomicEvidence

    def __post_init__(self) -> None:
        for field in ("order_id", "market_id", "selection_id", "punter_reference_number"):
            _provider_id(getattr(self, field), field)
        _unsigned_byte(self.order_status_code, "order_status_code")
        _provider_id(self.sequence_number, "sequence_number")
        _unsigned_byte(self.polarity_code, "polarity_code")
        for field in ("issued_at", "last_changed_at", "matching_timestamp"):
            value = getattr(self, field)
            if _timestamp(value, field) != value:
                raise BetdaqEconomicReadbackError(
                    f"{field} must use canonical UTC timestamp spelling"
                )
        for field in (
            "requested_stake",
            "requested_price",
            "total_stake",
            "unmatched_stake",
            "average_price",
        ):
            _finite_decimal(getattr(self, field), field)
        if self.requested_stake <= 0:
            raise BetdaqEconomicReadbackError(
                "requested_stake must be a positive provider stake"
            )
        if self.requested_price <= 0:
            raise BetdaqEconomicReadbackError(
                "requested_price must be a positive provider price"
            )
        for field in ("total_stake", "unmatched_stake", "average_price"):
            if getattr(self, field) < 0:
                raise BetdaqEconomicReadbackError(
                    f"{field} must be a non-negative provider economic value"
                )
        for field in (
            "gross_settlement_amount",
            "order_commission",
            "market_commission",
        ):
            value = getattr(self, field)
            if value is not None:
                _finite_decimal(value, field)
        if self.market_settled_at is not None:
            if (
                _timestamp(self.market_settled_at, "market_settled_at")
                != self.market_settled_at
            ):
                raise BetdaqEconomicReadbackError(
                    "market_settled_at must use canonical UTC timestamp spelling"
                )
        if self.currency is not None:
            _provider_currency(self.currency, "settlement currency")
        if type(self.denomination_proven) is not bool:
            raise BetdaqEconomicReadbackError("denomination_proven must be bool")
        if type(self.scalar_economic_use_proven) is not bool:
            raise BetdaqEconomicReadbackError("scalar_economic_use_proven must be bool")
        if type(self.evidence) is not BetdaqEconomicEvidence:
            raise BetdaqEconomicReadbackError(
                "order settlement evidence must be canonical BETDAQ economic evidence"
            )
        if self.evidence.method != "GetOrderDetails":
            raise BetdaqEconomicReadbackError(
                "order settlement evidence method must be GetOrderDetails"
            )
        expected_request_identity = _economic_request_identity(
            "GetOrderDetails",
            {"OrderId": self.order_id},
        )
        if self.evidence.request_identity_sha256 != expected_request_identity:
            raise BetdaqEconomicReadbackError(
                "order settlement evidence request identity does not match order_id"
            )
        if self.denomination_proven is not (self.currency is not None):
            raise BetdaqEconomicReadbackError(
                "denomination_proven must match independently bound settlement currency"
            )
        if self.scalar_economic_use_proven and not self.denomination_proven:
            raise BetdaqEconomicReadbackError(
                "scalar economic use requires independently proven denomination"
            )
        terminal_status_codes = _canonical_terminal_order_status_codes()
        expected_final = (
            self.order_status_code in terminal_status_codes
            and self.gross_settlement_amount is not None
            and (
                self.order_commission is not None
                or self.market_commission is not None
            )
            and self.market_settled_at is not None
        )
        if self.final_settlement_proven is not expected_final:
            raise BetdaqEconomicReadbackError(
                "final_settlement_proven does not match exact provider settlement evidence"
            )
        if self.scalar_economic_use_proven and not self.final_settlement_proven:
            raise BetdaqEconomicReadbackError(
                "scalar economic use requires final provider settlement evidence"
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
            "currency": self.currency,
            "denomination_proven": self.denomination_proven,
            "scalar_economic_use_proven": self.scalar_economic_use_proven,
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
    currency: str
    evidence: BetdaqEconomicEvidence

    def __post_init__(self) -> None:
        if _timestamp(self.posted_at, "posted_at") != self.posted_at:
            raise BetdaqEconomicReadbackError(
                "posted_at must use canonical UTC timestamp spelling"
            )
        if type(self.description) is not str:
            raise BetdaqEconomicReadbackError("description must be text")
        _finite_decimal(self.amount, "amount")
        _finite_decimal(self.resulting_balance, "resulting_balance")
        _unsigned_byte(self.posting_category, "posting_category")
        if self.order_id is not None:
            _provider_id(self.order_id, "order_id")
        if self.market_id is not None:
            _provider_id(self.market_id, "market_id")
        if self.posting_category == 1:
            if self.order_id is None or self.market_id is not None:
                raise BetdaqEconomicReadbackError(
                    "Settlement posting requires exact OrderId and no MarketId"
                )
        elif self.posting_category == 2:
            if self.market_id is None or self.order_id is not None:
                raise BetdaqEconomicReadbackError(
                    "Commission posting requires exact MarketId and no OrderId"
                )
        elif self.posting_category == 3:
            if self.order_id is not None or self.market_id is not None:
                raise BetdaqEconomicReadbackError(
                    "Other posting cannot claim Settlement/Commission handles"
                )
        # Future provider enum values remain raw evidence per BETDAQ's schema-
        # evolution contract; they do not inherit known category semantics.
        _provider_id(self.transaction_id, "transaction_id")
        _provider_currency(self.currency, "posting currency")
        if type(self.evidence) is not BetdaqEconomicEvidence:
            raise BetdaqEconomicReadbackError(
                "posting evidence must be canonical BETDAQ economic evidence"
            )
        if self.evidence.method not in {
            "ListAccountPostings",
            "ListAccountPostingsById",
        }:
            raise BetdaqEconomicReadbackError(
                "posting evidence method must be a postings read method"
            )

    def provider_content_dict(self) -> dict[str, object]:
        """Return immutable provider-row economics without per-call envelope provenance."""

        return {
            "posted_at": self.posted_at,
            "description": self.description,
            "amount": _decimal_text(self.amount),
            "resulting_balance": _decimal_text(self.resulting_balance),
            "posting_category": self.posting_category,
            "order_id": self.order_id,
            "market_id": self.market_id,
            "transaction_id": self.transaction_id,
            "currency": self.currency,
        }

    def canonical_dict(self) -> dict[str, object]:
        return {
            **self.provider_content_dict(),
            "evidence_id": self.evidence.evidence_id,
        }

    @property
    def transaction_identity(self) -> str:
        """Stable only inside one authenticated process-local BETDAQ account context.

        A restart may issue a new context. Cross-session account equivalence is a
        separate authority and is deliberately not inferred from credentials here.
        """

        return "betdaq-posting-transaction:" + _canonical_sha256(
            {
                "account_context_id": self.evidence.account_context_id,
                "transaction_id": self.transaction_id,
            }
        )

    @property
    def observation_id(self) -> str:
        # Request/window and sibling-row differences are per-call provenance, not
        # transaction economics. This identity therefore remains stable when BETDAQ
        # deliberately overlaps a page boundary or the row is re-resolved ById.
        return "betdaq-posting:" + _canonical_sha256(
            {
                "transaction_identity": self.transaction_identity,
                "provider_content": self.provider_content_dict(),
            }
        )


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
        _provider_currency(self.currency, "currency")
        for field in ("available_funds", "balance", "credit", "exposure"):
            _finite_decimal(getattr(self, field), field)
        if self.method == "ListAccountPostings":
            if self.query_start_at is None or self.query_end_at is None:
                raise BetdaqEconomicReadbackError("window read requires exact bounds")
            if (
                _timestamp(self.query_start_at, "query_start_at")
                != self.query_start_at
                or _timestamp(self.query_end_at, "query_end_at")
                != self.query_end_at
            ):
                raise BetdaqEconomicReadbackError(
                    "postings window bounds must use canonical UTC timestamp spelling"
                )
            start_utc = datetime.fromisoformat(
                self.query_start_at[:-1] + "+00:00"
            )
            end_utc = datetime.fromisoformat(
                self.query_end_at[:-1] + "+00:00"
            )
            if start_utc >= end_utc:
                raise BetdaqEconomicReadbackError(
                    "postings window start must precede end"
                )
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
        if type(self.evidence) is not BetdaqEconomicEvidence:
            raise BetdaqEconomicReadbackError(
                "postings evidence must be canonical BETDAQ economic evidence"
            )
        if self.evidence.method != self.method:
            raise BetdaqEconomicReadbackError(
                "postings evidence method does not match readback method"
            )
        request_attributes: dict[str, str]
        if self.method == "ListAccountPostings":
            start_value = self.query_start_at
            end_value = self.query_end_at
            if type(start_value) is not str or type(end_value) is not str:
                raise BetdaqEconomicReadbackError(
                    "postings readback window query is not canonical request text"
                )
            request_attributes = {
                "StartTime": start_value,
                "EndTime": end_value,
            }
        else:
            transaction_value = self.query_transaction_id
            if type(transaction_value) is not str:
                raise BetdaqEconomicReadbackError(
                    "postings readback transaction query is not canonical request text"
                )
            request_attributes = {"TransactionId": transaction_value}
        expected_request_identity = _economic_request_identity(
            self.method,
            request_attributes,
        )
        if self.evidence.request_identity_sha256 != expected_request_identity:
            raise BetdaqEconomicReadbackError(
                "postings evidence request identity does not match readback query"
            )
        if type(self.postings) is not tuple:
            raise BetdaqEconomicReadbackError("postings must be an exact immutable tuple")
        seen: dict[str, dict[str, object]] = {}
        for posting in self.postings:
            if type(posting) is not BetdaqPostingObservation:
                raise BetdaqEconomicReadbackError("postings contain invalid observation")
            if posting.evidence != self.evidence:
                raise BetdaqEconomicReadbackError(
                    "posting evidence does not match exact readback acquisition"
                )
            if posting.currency != self.currency:
                raise BetdaqEconomicReadbackError(
                    "posting currency does not match readback currency"
                )
            provider_content = posting.provider_content_dict()
            previous = seen.get(posting.transaction_id)
            if previous is not None:
                if previous != provider_content:
                    raise BetdaqEconomicReadbackError(
                        "same BETDAQ transaction id has conflicting economic content"
                    )
                raise BetdaqEconomicReadbackError(
                    "canonical postings readback must not retain duplicate transaction ids"
                )
            seen[posting.transaction_id] = provider_content

        # Reconstructed/copy-mutated canonical DTOs must retain the same provider
        # continuation/order laws already enforced at the XML parser boundary. Without
        # this second boundary check, dataclasses.replace() could reorder valid rows or
        # move a ById row to/behind its cursor while retaining otherwise valid evidence.
        if self.method == "ListAccountPostings":
            previous_posted_at: datetime | None = None
            for posting in self.postings:
                posted_at = datetime.fromisoformat(
                    posting.posted_at[:-1] + "+00:00"
                )
                if posted_at < start_utc or posted_at > end_utc:
                    raise BetdaqEconomicReadbackError(
                        "canonical ListAccountPostings readback contains posting "
                        "outside requested window"
                    )
                if (
                    previous_posted_at is not None
                    and posted_at < previous_posted_at
                ):
                    raise BetdaqEconomicReadbackError(
                        "canonical ListAccountPostings readback is not ordered by "
                        "increasing PostedAt"
                    )
                previous_posted_at = posted_at
        else:
            transaction_cursor = int(self.query_transaction_id)
            previous_transaction_id: int | None = None
            for posting in self.postings:
                transaction_id = int(posting.transaction_id)
                if transaction_id <= transaction_cursor:
                    raise BetdaqEconomicReadbackError(
                        "canonical ListAccountPostingsById readback contains "
                        "transaction at/before cursor"
                    )
                if (
                    previous_transaction_id is not None
                    and transaction_id <= previous_transaction_id
                ):
                    raise BetdaqEconomicReadbackError(
                        "canonical ListAccountPostingsById readback is not strictly "
                        "ordered by TransactionId"
                    )
                previous_transaction_id = transaction_id

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


def coalesce_posting_replays(
    *readbacks: BetdaqPostingsReadback,
) -> tuple[BetdaqPostingObservation, ...]:
    """Deduplicate repeated provider transactions across exact read responses.

    BETDAQ time-window paging may overlap rows at an equal PostedAt boundary, and
    ListAccountPostingsById can re-resolve an already observed transaction. Per-call
    evidence remains attached to the retained observation, but request/response
    envelope differences cannot create a second economic effect. Conflicting content
    for the same account-context + TransactionId fails closed. This helper never
    upgrades window completeness, proves absence of sibling postings, or authorizes
    scalar aggregation across independent responses.
    """

    if not readbacks:
        return ()
    account_context_id: str | None = None
    currency: str | None = None
    seen: dict[str, BetdaqPostingObservation] = {}
    order: list[str] = []
    for readback in readbacks:
        if type(readback) is not BetdaqPostingsReadback:
            raise BetdaqEconomicReadbackError(
                "posting replay coalescence requires canonical readbacks"
            )
        current_context = readback.evidence.account_context_id
        if account_context_id is None:
            account_context_id = current_context
            currency = readback.currency
        elif current_context != account_context_id:
            raise BetdaqEconomicReadbackError(
                "posting replays belong to different authenticated account contexts"
            )
        elif readback.currency != currency:
            raise BetdaqEconomicReadbackError(
                "posting replays use different provider currencies"
            )
        for posting in readback.postings:
            identity = posting.transaction_identity
            previous = seen.get(identity)
            if previous is not None:
                if previous.provider_content_dict() != posting.provider_content_dict():
                    raise BetdaqEconomicReadbackError(
                        "same BETDAQ transaction id has conflicting economic content"
                    )
                continue
            seen[identity] = posting
            order.append(identity)
    return tuple(seen[identity] for identity in order)


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
        _, external_ns, _, _ = _canonical_economic_protocol_authority()
        settlement_nodes = [
            child
            for child in result
            if child.tag == f"{{{external_ns}}}OrderSettlementInformation"
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
        status = _unsigned_byte_attr(result, "OrderStatus")
        terminal_status_codes = _canonical_terminal_order_status_codes()
        final = (
            status in terminal_status_codes
            and gross is not None
            and (order_commission is not None or market_commission is not None)
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
            polarity_code=_unsigned_byte_attr(result, "Polarity"),
            punter_reference_number=_provider_id(
                _required_attr(result, "PunterReferenceNumber"),
                "PunterReferenceNumber",
            ),
            gross_settlement_amount=gross,
            order_commission=order_commission,
            market_commission=market_commission,
            market_settled_at=market_settled_at,
            currency=None,
            denomination_proven=False,
            scalar_economic_use_proven=False,
            final_settlement_proven=final,
            evidence=evidence,
        )

    def read_account_postings(
        self, start_at: datetime, end_at: datetime
    ) -> BetdaqPostingsReadback:
        start = _request_time(start_at, "start_at")
        end = _request_time(end_at, "end_at")
        start_utc = datetime.fromisoformat(start[:-1] + "+00:00")
        end_utc = datetime.fromisoformat(end[:-1] + "+00:00")
        if start_utc >= end_utc:
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
        self,
        method: str,
        request_attributes: dict[str, str],
        *,
        _product_clock_dispatch=_canonical_product_receive_clock,
        _product_clock_dispatch_code=_canonical_product_receive_clock.__code__,
        _request_builder=_request_xml,
        _request_builder_code=_request_xml.__code__,
        _request_builder_defaults=_request_xml.__defaults__,
        _request_builder_kwdefaults=_request_xml.__kwdefaults__,
    ) -> tuple[ET.Element, BetdaqEconomicEvidence]:
        if method not in ("GetOrderDetails", "ListAccountPostings", "ListAccountPostingsById"):
            raise BetdaqEconomicReadbackError("method is outside economic READ allowlist")
        client = self._account_client
        if type(request_attributes) is not dict:
            raise BetdaqEconomicReadbackError(
                "economic request attributes must be an exact dict"
            )
        # Freeze the authority material before any callback or lock acquisition.
        # The actual SOAP builder receives a private copy of this snapshot, never
        # the caller-owned mapping used to mint request identity.
        request_attributes_snapshot = dict(request_attributes)
        request_identity = _economic_request_identity(
            method,
            request_attributes_snapshot,
        )

        def request_builder_current() -> bool:
            live_builder = globals().get("_request_xml")
            return (
                live_builder is _request_builder
                and getattr(live_builder, "__code__", None) is _request_builder_code
                and getattr(_request_builder, "__code__", None) is _request_builder_code
                and getattr(_request_builder, "__defaults__", None)
                is _request_builder_defaults
                and getattr(_request_builder, "__kwdefaults__", None)
                is _request_builder_kwdefaults
            )

        protocol_authority = _canonical_economic_protocol_authority()
        secure_endpoint, external_ns, _, _ = protocol_authority
        headers = {
            "Accept": "text/xml",
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"{external_ns}{method}"',
        }
        with client._call_lock:
            # Snapshot the exact authenticated identity used to construct the secure
            # request. Evidence must bind to these same values even if another owner
            # rotates/rebinds the mutable account client while network I/O is in flight.
            credentials = client._credentials
            venue_id = client._venue_id
            live_clock_dispatch = globals().get("_canonical_product_receive_clock")
            if (
                live_clock_dispatch is not _product_clock_dispatch
                or getattr(live_clock_dispatch, "__code__", None)
                is not _product_clock_dispatch_code
                or getattr(_product_clock_dispatch, "__code__", None)
                is not _product_clock_dispatch_code
            ):
                raise BetdaqEconomicReadbackError(
                    "canonical BETDAQ economic product clock authority was replaced"
                )
            clock = _product_clock_dispatch()
            if type(credentials) is not _account.BetdaqCredentials:
                raise BetdaqEconomicReadbackError(
                    "BETDAQ economic read requires canonical credentials"
                )
            try:
                context_resolver = _canonical_authenticated_account_context_dispatch()
                context_before = context_resolver(credentials, venue_id)
            except BetdaqEconomicReadbackError:
                raise
            except Exception:
                raise BetdaqEconomicReadbackError(
                    "BETDAQ authenticated account context failed canonical validation"
                ) from None
            if type(context_before) is not _CANONICAL_AUTHENTICATED_ACCOUNT_CONTEXT_TYPE:
                raise BetdaqEconomicReadbackError(
                    "BETDAQ authenticated account context is not canonical"
                )
            if not request_builder_current():
                raise BetdaqEconomicReadbackError(
                    "request body does not match exact economic request authority"
                )
            body = _request_builder(
                credentials,
                method,
                dict(request_attributes_snapshot),
            )
            if not request_builder_current():
                raise BetdaqEconomicReadbackError(
                    "request body does not match exact economic request authority"
                )
            transport = client._transport
            try:
                require_transport, https_post = _canonical_economic_transport_dispatch()
            except BetdaqEconomicReadbackError:
                # Preserve exact canonical-authority tamper evidence from the
                # economic dispatcher; do not blur it into an ordinary bad transport.
                raise
            except Exception:
                raise BetdaqEconomicReadbackError(
                    "canonical BETDAQ economic evidence requires product-owned HTTPS transport"
                ) from None
            try:
                require_transport(transport)
            except Exception:
                raise BetdaqEconomicReadbackError(
                    "canonical BETDAQ economic evidence requires product-owned HTTPS transport"
                ) from None
            try:
                payload = https_post(
                    transport,
                    secure_endpoint,
                    headers=headers,
                    body=body,
                    timeout_seconds=client._timeout_seconds,
                )
            except Exception:
                raise BetdaqEconomicReadbackError(
                    "BETDAQ economic read transport failed"
                ) from None
            if (
                client._credentials is not credentials
                or client._venue_id != venue_id
            ):
                raise BetdaqEconomicReadbackError(
                    "BETDAQ authenticated account context changed during economic acquisition"
                )
            if _canonical_economic_protocol_authority() != protocol_authority:
                raise BetdaqEconomicReadbackError(
                    "canonical BETDAQ economic protocol authority was replaced"
                )
            try:
                if (
                    _canonical_authenticated_account_context_dispatch()
                    is not context_resolver
                ):
                    raise BetdaqEconomicReadbackError(
                        "canonical BETDAQ authenticated account context authority was replaced"
                    )
                context_after = context_resolver(credentials, venue_id)
            except BetdaqEconomicReadbackError:
                raise
            except Exception:
                raise BetdaqEconomicReadbackError(
                    "BETDAQ authenticated account context failed canonical validation"
                ) from None
            if (
                type(context_after) is not _CANONICAL_AUTHENTICATED_ACCOUNT_CONTEXT_TYPE
                or context_after is not context_before
            ):
                raise BetdaqEconomicReadbackError(
                    "BETDAQ authenticated account context changed during economic acquisition"
                )
            live_clock_dispatch = globals().get("_canonical_product_receive_clock")
            if (
                live_clock_dispatch is not _product_clock_dispatch
                or getattr(live_clock_dispatch, "__code__", None)
                is not _product_clock_dispatch_code
                or _product_clock_dispatch() is not clock
            ):
                raise BetdaqEconomicReadbackError(
                    "canonical BETDAQ economic product clock authority was replaced"
                )
            try:
                observed_value = clock()
                if (
                    type(observed_value) is not datetime
                    or observed_value.tzinfo is None
                    or observed_value.utcoffset() is None
                ):
                    raise ValueError("product clock must return exact timezone-aware datetime")
                observed_at = (
                    observed_value.astimezone(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                )
            except BetdaqEconomicReadbackError:
                raise
            except Exception:
                raise BetdaqEconomicReadbackError(
                    "BETDAQ economic response failed canonical validation"
                ) from None
            live_clock_dispatch = globals().get("_canonical_product_receive_clock")
            if (
                live_clock_dispatch is not _product_clock_dispatch
                or getattr(live_clock_dispatch, "__code__", None)
                is not _product_clock_dispatch_code
                or _product_clock_dispatch() is not clock
            ):
                raise BetdaqEconomicReadbackError(
                    "canonical BETDAQ economic product clock authority was replaced"
                )
        if type(payload) is not bytes:
            raise BetdaqEconomicReadbackError(
                "BETDAQ economic read transport must return bytes"
            )
        try:
            result = _parse_economic_soap_result(payload, method)
        except Exception:
            raise BetdaqEconomicReadbackError(
                "BETDAQ economic response failed canonical validation"
            ) from None
        evidence = BetdaqEconomicEvidence(
            method=method,
            request_identity_sha256=request_identity,
            source_payload_sha256=sha256(payload).hexdigest(),
            observed_at=observed_at,
            account_context_id=context_before.session_context_id,
        )
        return result, evidence


def _request_xml(
    credentials: _account.BetdaqCredentials,
    method: str,
    attributes: dict[str, str],
) -> bytes:
    _, external_ns, soap11_ns, _ = _canonical_economic_protocol_authority()
    ET.register_namespace("soap", soap11_ns)
    envelope = ET.Element(f"{{{soap11_ns}}}Envelope")
    header = ET.SubElement(envelope, f"{{{soap11_ns}}}Header")
    ET.SubElement(
        header,
        f"{{{external_ns}}}ExternalApiHeader",
        {
            "version": credentials.version,
            "languageCode": credentials.language_code,
            "username": credentials.username,
            "password": credentials.password,
            "applicationIdentifier": credentials.application_identifier,
        },
    )
    body = ET.SubElement(envelope, f"{{{soap11_ns}}}Body")
    method_element = ET.SubElement(body, f"{{{external_ns}}}{method}")
    if method == "GetOrderDetails":
        request_name = "getOrderDetailsRequest"
    elif method == "ListAccountPostings":
        request_name = "listAccountPostingsRequest"
    elif method == "ListAccountPostingsById":
        request_name = "listAccountPostingsByIdRequest"
    else:
        raise BetdaqEconomicReadbackError(
            "method is outside economic READ request-element allowlist"
        )
    ET.SubElement(
        method_element,
        f"{{{external_ns}}}{request_name}",
        attributes,
    )
    return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)


def _parse_economic_soap_result(payload: bytes, method: str) -> ET.Element:
    """Parse generated BETDAQ SOAP results without inventing ReturnStatus."""
    _, external_ns, soap11_ns, soap12_ns = _canonical_economic_protocol_authority()
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
        soap11_ns,
        soap12_ns,
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
    expected_response_tag = f"{{{external_ns}}}{method}Response"
    body_children = list(body)
    if len(body_children) != 1 or body_children[0].tag != expected_response_tag:
        raise BetdaqEconomicReadbackError(
            f"BETDAQ economic response is not the exact {method}Response body"
        )
    response = body_children[0]
    expected_result_tag = f"{{{external_ns}}}{method}Result"
    response_children = list(response)
    if len(response_children) != 1 or response_children[0].tag != expected_result_tag:
        raise BetdaqEconomicReadbackError(
            f"BETDAQ economic response is not the exact {method}Result wrapper"
        )
    result = response_children[0]

    def require_known_attributes(
        element: ET.Element,
        allowed: tuple[str, ...],
        context: str,
    ) -> None:
        unexpected = tuple(
            sorted(name for name in element.attrib if name not in allowed)
        )
        if unexpected:
            raise BetdaqEconomicReadbackError(
                f"{context} contains unexpected provider attribute: "
                + ", ".join(unexpected)
            )

    statuses = [
        child
        for child in result
        if child.tag == f"{{{external_ns}}}ReturnStatus"
    ]
    if len(statuses) != 1:
        raise BetdaqEconomicReadbackError(
            "BETDAQ economic result must contain exactly one ReturnStatus"
        )
    require_known_attributes(
        statuses[0],
        ("Code", "Description", "CallId"),
        "BETDAQ ReturnStatus",
    )
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

    allowed_children = {
        f"{{{external_ns}}}ReturnStatus",
        (
            f"{{{external_ns}}}OrderSettlementInformation"
            if method == "GetOrderDetails"
            else f"{{{external_ns}}}Orders"
        ),
    }
    if method == "GetOrderDetails":
        # The generated BETDAQ contract documents AuditLog as a sibling of
        # OrderSettlementInformation. It remains raw/content-bound evidence here;
        # this economic projection does not infer settlement state from audit entries.
        audit_log_tag = f"{{{external_ns}}}AuditLog"
        allowed_children.add(audit_log_tag)
        if sum(child.tag == audit_log_tag for child in result) > 1:
            raise BetdaqEconomicReadbackError(
                "GetOrderDetails has duplicate AuditLog containers"
            )
    if any(child.tag not in allowed_children for child in result):
        raise BetdaqEconomicReadbackError(
            f"BETDAQ economic {method} result contains unexpected element"
        )

    if method == "GetOrderDetails":
        require_known_attributes(
            result,
            (
                "SelectionId",
                "OrderStatus",
                "IssuedAt",
                "LastChangedAt",
                "ExpiresAt",
                "ValidFrom",
                "RestrictOrderToBroker",
                "OrderFillType",
                "FillOrKillThreshold",
                "MarketId",
                "MarketStatus",
                "RequestedStake",
                "RequestedPrice",
                "ExpectedSelectionResetCount",
                "TotalStake",
                "UnmatchedStake",
                "AveragePrice",
                "MatchingTimeStamp",
                "Polarity",
                "WithdrawlRepriceOption",
                "WithdrawalRepriceOption",
                "CancelOnInRunning",
                "CancelIfSelectionReset",
                "SequenceNumber",
                "MarketType",
                "ExpectedWithdrawlSequenceNumber",
                "ExpectedWithdrawalSequenceNumber",
                "PunterReferenceNumber",
            ),
            "BETDAQ GetOrderDetailsResult",
        )
        settlement_tag = (
            f"{{{external_ns}}}OrderSettlementInformation"
        )
        for settlement in result:
            if settlement.tag == settlement_tag:
                require_known_attributes(
                    settlement,
                    (
                        "GrossSettlementAmount",
                        "OrderCommission",
                        "MarketCommission",
                        "MarketSettledDate",
                    ),
                    "BETDAQ OrderSettlementInformation",
                )
    else:
        result_attributes = (
            (
                "Currency",
                "AvailableFunds",
                "Balance",
                "Credit",
                "Exposure",
                "HaveAllPostingsBeenReturned",
            )
            if method == "ListAccountPostings"
            else (
                "Currency",
                "AvailableFunds",
                "Balance",
                "Credit",
                "Exposure",
            )
        )
        require_known_attributes(
            result,
            result_attributes,
            f"BETDAQ {method}Result",
        )
        orders_tag = f"{{{external_ns}}}Orders"
        order_tag = f"{{{external_ns}}}Order"
        for container in result:
            if container.tag != orders_tag:
                continue
            require_known_attributes(
                container,
                (),
                "BETDAQ postings Orders",
            )
            for posting in container:
                if posting.tag == order_tag:
                    require_known_attributes(
                        posting,
                        (
                            "PostedAt",
                            "Description",
                            "Amount",
                            "ResultingBalance",
                            "PostingCategory",
                            "OrderId",
                            "MarketId",
                            "TransactionId",
                        ),
                        "BETDAQ posting row",
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
    _, external_ns, _, _ = _canonical_economic_protocol_authority()
    containers = [
        child
        for child in result
        if child.tag == f"{{{external_ns}}}Orders"
    ]
    if len(containers) != 1:
        raise BetdaqEconomicReadbackError(
            "BETDAQ postings result must contain exactly one Orders element"
        )
    currency = _required_attr(result, "Currency")
    # The provider API specification gives two paging-order laws that are
    # authority-bearing for continuation:
    # - ListAccountPostings rows are ordered by increasing PostedAt;
    # - ListAccountPostingsById returns TransactionId values strictly greater than
    #   the supplied cursor, ordered ascending by TransactionId.
    # Exact duplicate transaction rows remain idempotent per the canonical #1734
    # contract; do not invent ResultingBalance adjacency beyond provider evidence.
    deduped: dict[str, BetdaqPostingObservation] = {}
    ordered_ids: list[str] = []
    previous_posted_at: datetime | None = None
    previous_transaction_id: int | None = None
    cursor_transaction_id = (
        int(query_transaction_id)
        if method == "ListAccountPostingsById"
        and query_transaction_id is not None
        else None
    )
    for child in containers[0]:
        if child.tag != f"{{{external_ns}}}Order":
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
            posting_category=_unsigned_byte_attr(child, "PostingCategory"),
            order_id=_optional_provider_attr(child, "OrderId"),
            market_id=_optional_provider_attr(child, "MarketId"),
            transaction_id=transaction_id,
            currency=currency,
            evidence=evidence,
        )
        if method == "ListAccountPostings":
            posted_at = datetime.fromisoformat(
                posting.posted_at[:-1] + "+00:00"
            )
            if previous_posted_at is not None and posted_at < previous_posted_at:
                raise BetdaqEconomicReadbackError(
                    "ListAccountPostings rows are not ordered by increasing PostedAt"
                )
            previous_posted_at = posted_at
        else:
            numeric_transaction_id = int(transaction_id)
            if (
                cursor_transaction_id is None
                or numeric_transaction_id <= cursor_transaction_id
            ):
                raise BetdaqEconomicReadbackError(
                    "ListAccountPostingsById returned transaction at/before cursor"
                )
            if (
                previous_transaction_id is not None
                and numeric_transaction_id < previous_transaction_id
            ):
                raise BetdaqEconomicReadbackError(
                    "ListAccountPostingsById rows are not ordered ascending by TransactionId"
                )
            previous_transaction_id = numeric_transaction_id
        previous = deduped.get(transaction_id)
        if previous is not None:
            if previous.provider_content_dict() != posting.provider_content_dict():
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
        currency=currency,
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


def _provider_currency(
    value: str,
    field: str,
    *,
    _currency=_CANONICAL_ACCOUNT_CURRENCY,
    _currency_code=_CANONICAL_ACCOUNT_CURRENCY_CODE,
) -> str:
    live_currency = getattr(_account, "_currency", None)
    if (
        live_currency is not _currency
        or getattr(live_currency, "__code__", None) is not _currency_code
        or globals().get("_CANONICAL_ACCOUNT_CURRENCY") is not _currency
        or globals().get("_CANONICAL_ACCOUNT_CURRENCY_CODE") is not _currency_code
    ):
        raise BetdaqEconomicReadbackError(
            "canonical BETDAQ currency validator was replaced"
        )
    try:
        return _currency(value)
    except BetdaqAccountReadOnlyError:
        raise BetdaqEconomicReadbackError(
            f"{field} must be canonical 3-letter uppercase provider currency"
        ) from None


def _provider_id(value: int | str, field: str) -> str:
    if type(value) is int:
        if value < 0 or value > _MAX_XSD_LONG:
            raise BetdaqEconomicReadbackError(
                f"{field} must fit non-negative provider xsd:long"
            )
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
    if int(value) > _MAX_XSD_LONG:
        raise BetdaqEconomicReadbackError(
            f"{field} must fit non-negative provider xsd:long"
        )
    return value


def _integer_attr(element: ET.Element, name: str) -> int:
    return int(_provider_id(_required_attr(element, name), name))


def _unsigned_byte_attr(element: ET.Element, name: str) -> int:
    return _unsigned_byte(_integer_attr(element, name), name)


def _decimal_attr(element: ET.Element, name: str) -> Decimal:
    raw = _required_attr(element, name)
    if _DECIMAL_RE.fullmatch(raw) is None:
        raise BetdaqEconomicReadbackError(
            f"{name} is not canonical provider xsd:decimal text"
        )
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
    if _DECIMAL_RE.fullmatch(raw) is None:
        raise BetdaqEconomicReadbackError(
            f"{name} is not canonical provider xsd:decimal text"
        )
    try:
        value = Decimal(raw)
    except (InvalidOperation, ValueError):
        raise BetdaqEconomicReadbackError(f"{name} is not provider Decimal text") from None
    return _finite_decimal(value, name)


def _finite_decimal(value: Decimal, field: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise BetdaqEconomicReadbackError(f"{field} must be an exact finite Decimal")
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
    if type(value) is not datetime or value.tzinfo is None:
        raise BetdaqEconomicReadbackError(
            f"{field} must be an exact timezone-aware datetime"
        )
    try:
        detached = datetime.fromisoformat(value.isoformat())
    except (TypeError, ValueError):
        raise BetdaqEconomicReadbackError(
            f"{field} must be an exact timezone-aware datetime"
        ) from None
    if detached.tzinfo is None or detached.utcoffset() is None:
        raise BetdaqEconomicReadbackError(
            f"{field} must be an exact timezone-aware datetime"
        )
    return detached.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _nonnegative_int(value: int, field: str) -> int:
    if type(value) is not int or value < 0:
        raise BetdaqEconomicReadbackError(f"{field} must be a non-negative integer")
    return value


def _unsigned_byte(value: int, field: str) -> int:
    if type(value) is not int or value < 0 or value > 255:
        raise BetdaqEconomicReadbackError(
            f"{field} must be an unsigned-byte provider value"
        )
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
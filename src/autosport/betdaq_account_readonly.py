"""Strict read-only BETDAQ secure account/order acquisition.

This adapter consumes the provider's documented secure READ methods only and projects
complete current-order evidence into Autosport's existing bookmaker account snapshot
contract.  It does not place, update, cancel, suspend, settle, or otherwise move money.

BETDAQ bootstrap semantics are deliberately implemented as a causal protocol: freeze the
first MaximumSequenceNumber, finish the bootstrap prefix to that cutoff, then close the
bootstrap race with ListOrdersChangedSince until an empty delta is observed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import hmac
from secrets import token_bytes, token_hex
from threading import Lock, RLock
from typing import Callable, Protocol, runtime_checkable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import json
import re
import xml.etree.ElementTree as ET

from .bookmaker_capability import (
    BookmakerAccountSnapshot,
    BookmakerBalanceObservation,
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
    BookmakerPositionObservation,
    BookmakerPositionState,
)


ADAPTER_ID = "betdaq-secure-readonly"
ADAPTER_VERSION = "1"
_SECURE_ENDPOINT = "https://api.betdaq.com/v2.0/Secure/SecureService.asmx"
_EXTERNAL_NS = "http://www.GlobalBettingExchange.com/ExternalAPI/"
_SOAP11_NS = "http://schemas.xmlsoap.org/soap/envelope/"
_SOAP12_NS = "http://www.w3.org/2003/05/soap-envelope"
_ALLOWED_METHODS = frozenset(
    {"GetAccountBalances", "ListBootstrapOrders", "ListOrdersChangedSince"}
)
_STATUS_NAMES = {
    1: "UNMATCHED",
    2: "MATCHED",
    3: "CANCELLED",
    4: "SETTLED",
    5: "VOID",
    6: "SUSPENDED",
}
_POLARITY_NAMES = {1: "BACK", 2: "LAY"}
_TERMINAL_STATUS_CODES = frozenset({4, 5})
_INTEGER_RE = re.compile(r"-?[0-9]+\Z")
_ACCOUNT_CONTEXT_PREFIX = "betdaq-auth-context:"
_ACCOUNT_CONTEXT_SCOPE = "AUTHENTICATED_CREDENTIAL_APPLICATION_CONTEXT"
_PROCESS_HMAC_KEY = token_bytes(32)
_ACCOUNT_CONTEXT_LOCK = RLock()


class BetdaqAccountReadOnlyError(RuntimeError):
    """BETDAQ read-only transport, protocol, or evidence error."""


@dataclass(frozen=True, slots=True, repr=False)
class BetdaqCredentials:
    username: str
    password: str
    application_identifier: str
    version: str = "2.0"
    language_code: str = "en"

    def __post_init__(self) -> None:
        for field in (
            "username",
            "password",
            "application_identifier",
            "version",
            "language_code",
        ):
            _required_text(getattr(self, field), field)

    def __repr__(self) -> str:
        return "BetdaqCredentials(<redacted>)"


@dataclass(frozen=True, slots=True)
class BetdaqAuthenticatedAccountContext:
    """Opaque process-local scope for one exact BETDAQ credential/app context.

    BETDAQ account/order reads used here do not expose a stable provider account id.
    This object therefore prevents caller labels from becoming account authority while
    explicitly refusing to claim cross-session physical-account equivalence.
    """

    venue_id: str
    session_context_id: str
    identity_scope: str = _ACCOUNT_CONTEXT_SCOPE
    stable_account_identity_proven: bool = False
    cross_session_equivalence_proven: bool = False

    def __post_init__(self) -> None:
        _required_text(self.venue_id, "venue_id")
        if (
            type(self.session_context_id) is not str
            or not self.session_context_id.startswith(_ACCOUNT_CONTEXT_PREFIX)
        ):
            raise BetdaqAccountReadOnlyError(
                "session_context_id is not a canonical BETDAQ context id"
            )
        _sha256_hex(
            self.session_context_id.removeprefix(_ACCOUNT_CONTEXT_PREFIX),
            "session_context_id",
        )
        if self.identity_scope != _ACCOUNT_CONTEXT_SCOPE:
            raise BetdaqAccountReadOnlyError(
                "BETDAQ account context identity scope is product-owned"
            )
        if self.stable_account_identity_proven is not False:
            raise BetdaqAccountReadOnlyError(
                "BETDAQ secure reads do not prove stable account identity"
            )
        if self.cross_session_equivalence_proven is not False:
            raise BetdaqAccountReadOnlyError(
                "BETDAQ secure reads do not prove cross-session account equivalence"
            )


_ACCOUNT_CONTEXTS: dict[
    tuple[str, bytes], BetdaqAuthenticatedAccountContext
] = {}


def _credential_context_binding(credentials: BetdaqCredentials) -> bytes:
    if type(credentials) is not BetdaqCredentials:
        raise BetdaqAccountReadOnlyError(
            "authenticated account context requires canonical BetdaqCredentials"
        )
    material = json.dumps(
        {
            "adapter_id": ADAPTER_ID,
            "adapter_version": ADAPTER_VERSION,
            "application_identifier": credentials.application_identifier,
            "endpoint": _SECURE_ENDPOINT,
            "language_code": credentials.language_code,
            "password": credentials.password,
            "username": credentials.username,
            "version": credentials.version,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    # The keyed digest never leaves process memory and is never serialized or exposed.
    # It only lets equal live credential/application contexts share one opaque random id.
    return hmac.digest(_PROCESS_HMAC_KEY, material, "sha256")


def _authenticated_account_context(
    credentials: BetdaqCredentials,
    venue_id: str,
) -> BetdaqAuthenticatedAccountContext:
    venue = _required_text(venue_id, "venue_id")
    binding = _credential_context_binding(credentials)
    key = (venue, binding)
    with _ACCOUNT_CONTEXT_LOCK:
        existing = _ACCOUNT_CONTEXTS.get(key)
        if existing is not None:
            return existing
        value = BetdaqAuthenticatedAccountContext(
            venue_id=venue,
            session_context_id=_ACCOUNT_CONTEXT_PREFIX + token_hex(32),
        )
        _ACCOUNT_CONTEXTS[key] = value
        return value


@runtime_checkable
class BetdaqSoapTransport(Protocol):
    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes: ...


class UrllibBetdaqSoapTransport:
    """Small HTTPS transport. Provider/network diagnostics intentionally omit secrets."""

    def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> bytes:
        request = Request(url, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                payload = response.read()
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise BetdaqAccountReadOnlyError("BETDAQ secure read transport failed") from None
        if type(payload) is not bytes:
            raise BetdaqAccountReadOnlyError("BETDAQ transport returned non-bytes payload")
        return payload


@dataclass(frozen=True, slots=True)
class BetdaqSoapEvidence:
    method: str
    observed_at: str
    source_payload_sha256: str

    def __post_init__(self) -> None:
        if self.method not in _ALLOWED_METHODS:
            raise BetdaqAccountReadOnlyError("evidence method is outside the read-only allowlist")
        _timestamp(self.observed_at, "observed_at")
        _sha256_hex(self.source_payload_sha256, "source_payload_sha256")


@dataclass(frozen=True, slots=True)
class BetdaqBalanceObservation:
    currency: str
    balance: Decimal
    exposure: Decimal
    available_funds: Decimal
    credit: Decimal
    evidence: BetdaqSoapEvidence

    def __post_init__(self) -> None:
        _currency(self.currency)
        for field in ("balance", "exposure", "available_funds", "credit"):
            _finite_decimal(getattr(self, field), field)


@dataclass(frozen=True, slots=True)
class BetdaqOrderObservation:
    order_id: str
    market_id: str
    selection_id: str
    sequence_number: int
    issued_at: str
    polarity: str
    unmatched_stake: Decimal
    requested_price: Decimal
    matched_price: Decimal | None
    matched_stake: Decimal
    total_for_side_make_stake: Decimal
    total_for_side_take_stake: Decimal
    matched_against_stake: Decimal
    status_code: int
    status_name: str
    punter_reference_number: str
    expected_selection_reset_count: int
    expected_withdrawal_sequence_number: int
    evidence: BetdaqSoapEvidence

    def __post_init__(self) -> None:
        for field in ("order_id", "market_id", "selection_id", "punter_reference_number"):
            _provider_identifier(getattr(self, field), field)
        _nonnegative_int(self.sequence_number, "sequence_number")
        _timestamp(self.issued_at, "issued_at")
        if self.polarity not in frozenset(_POLARITY_NAMES.values()):
            raise BetdaqAccountReadOnlyError("polarity must be BACK or LAY")
        for field in (
            "unmatched_stake",
            "matched_stake",
            "total_for_side_make_stake",
            "total_for_side_take_stake",
            "matched_against_stake",
        ):
            value = _finite_decimal(getattr(self, field), field)
            if value < 0:
                raise BetdaqAccountReadOnlyError(f"{field} must be non-negative")
        if _finite_decimal(self.requested_price, "requested_price") <= 0:
            raise BetdaqAccountReadOnlyError("requested_price must be positive")
        if self.matched_price is not None and _finite_decimal(
            self.matched_price, "matched_price"
        ) <= 0:
            raise BetdaqAccountReadOnlyError("matched_price must be positive")
        expected_name = _STATUS_NAMES.get(self.status_code)
        if expected_name is None or self.status_name != expected_name:
            raise BetdaqAccountReadOnlyError("unknown or inconsistent BETDAQ order status")
        _nonnegative_int(
            self.expected_selection_reset_count, "expected_selection_reset_count"
        )
        _nonnegative_int(
            self.expected_withdrawal_sequence_number,
            "expected_withdrawal_sequence_number",
        )
        if not isinstance(self.evidence, BetdaqSoapEvidence):
            raise BetdaqAccountReadOnlyError("order evidence must be BetdaqSoapEvidence")

    @property
    def observation_id(self) -> str:
        return (
            f"betdaq-order:{self.order_id}:{self.sequence_number}:"
            f"{self.evidence.source_payload_sha256}"
        )

    def canonical_payload(self) -> dict[str, object]:
        return {
            "order_id": self.order_id,
            "market_id": self.market_id,
            "selection_id": self.selection_id,
            "sequence_number": self.sequence_number,
            "issued_at": self.issued_at,
            "polarity": self.polarity,
            "unmatched_stake": _decimal_text(self.unmatched_stake),
            "requested_price": _decimal_text(self.requested_price),
            "matched_price": (
                None if self.matched_price is None else _decimal_text(self.matched_price)
            ),
            "matched_stake": _decimal_text(self.matched_stake),
            "total_for_side_make_stake": _decimal_text(
                self.total_for_side_make_stake
            ),
            "total_for_side_take_stake": _decimal_text(
                self.total_for_side_take_stake
            ),
            "matched_against_stake": _decimal_text(self.matched_against_stake),
            "status_code": self.status_code,
            "status_name": self.status_name,
            "punter_reference_number": self.punter_reference_number,
            "expected_selection_reset_count": self.expected_selection_reset_count,
            "expected_withdrawal_sequence_number": self.expected_withdrawal_sequence_number,
            "source_payload_sha256": self.evidence.source_payload_sha256,
        }


@dataclass(frozen=True, slots=True)
class BetdaqBootstrapPage:
    maximum_sequence_number: int
    orders: tuple[BetdaqOrderObservation, ...]
    evidence: BetdaqSoapEvidence

    def __post_init__(self) -> None:
        if (
            type(self.maximum_sequence_number) is not int
            or self.maximum_sequence_number < -1
        ):
            raise BetdaqAccountReadOnlyError(
                "maximum_sequence_number must be an integer >= -1"
            )
        _validate_strict_sequence(self.orders, "bootstrap orders")


@dataclass(frozen=True, slots=True)
class BetdaqChangedOrdersPage:
    orders: tuple[BetdaqOrderObservation, ...]
    evidence: BetdaqSoapEvidence

    def __post_init__(self) -> None:
        _validate_strict_sequence(self.orders, "changed orders")


@dataclass(frozen=True, slots=True)
class BetdaqCurrentOrderBook:
    orders: tuple[BetdaqOrderObservation, ...]
    bootstrap_cutoff_sequence: int
    final_sequence: int
    observed_at: str
    response_evidence: tuple[BetdaqSoapEvidence, ...]
    evidence_sha256: str

    def __post_init__(self) -> None:
        if type(self.bootstrap_cutoff_sequence) is not int:
            raise BetdaqAccountReadOnlyError("bootstrap cutoff must be an integer")
        if type(self.final_sequence) is not int:
            raise BetdaqAccountReadOnlyError("final sequence must be an integer")
        if self.final_sequence < self.bootstrap_cutoff_sequence:
            raise BetdaqAccountReadOnlyError("final sequence cannot precede bootstrap cutoff")
        _timestamp(self.observed_at, "observed_at")
        _sha256_hex(self.evidence_sha256, "evidence_sha256")
        ids = [item.order_id for item in self.orders]
        if len(ids) != len(set(ids)):
            raise BetdaqAccountReadOnlyError("current order book contains duplicate order ids")


@dataclass(frozen=True, slots=True)
class BetdaqAccountEvidence:
    snapshot: BookmakerAccountSnapshot
    balance: BetdaqBalanceObservation
    current_orders: BetdaqCurrentOrderBook | None
    account_context: BetdaqAuthenticatedAccountContext

    def __post_init__(self) -> None:
        if not isinstance(self.snapshot, BookmakerAccountSnapshot):
            raise BetdaqAccountReadOnlyError("snapshot must be BookmakerAccountSnapshot")
        if not isinstance(self.balance, BetdaqBalanceObservation):
            raise BetdaqAccountReadOnlyError("balance must be BetdaqBalanceObservation")
        if self.current_orders is not None and not isinstance(
            self.current_orders, BetdaqCurrentOrderBook
        ):
            raise BetdaqAccountReadOnlyError(
                "current_orders must be BetdaqCurrentOrderBook or None"
            )
        if not isinstance(
            self.account_context, BetdaqAuthenticatedAccountContext
        ):
            raise BetdaqAccountReadOnlyError(
                "account_context must be BetdaqAuthenticatedAccountContext"
            )
        if (
            self.snapshot.profile.venue_id != self.account_context.venue_id
            or self.snapshot.profile.account_id
            != self.account_context.session_context_id
        ):
            raise BetdaqAccountReadOnlyError(
                "snapshot identity is not bound to authenticated account context"
            )


class BetdaqAccountReadOnlyClient:
    """Authenticated BETDAQ READ client with no provider mutation surface."""

    def __init__(
        self,
        credentials: BetdaqCredentials,
        *,
        transport: BetdaqSoapTransport | None = None,
        timeout_seconds: float = 10.0,
        clock: Callable[[], datetime] | None = None,
        venue_id: str = "betdaq",
        account_id: str = "default-account",
    ) -> None:
        if not isinstance(credentials, BetdaqCredentials):
            raise TypeError("credentials must be BetdaqCredentials")
        if (
            not isinstance(timeout_seconds, (int, float))
            or isinstance(timeout_seconds, bool)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be positive")
        self._credentials = credentials
        self._transport = transport or UrllibBetdaqSoapTransport()
        self._timeout_seconds = float(timeout_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._venue_id = _required_text(venue_id, "venue_id")
        # Retained only as a human/configuration label for API compatibility. It is
        # deliberately excluded from canonical account evidence and authority.
        self._account_label = _required_text(account_id, "account_id")
        self._call_lock = Lock()

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(adapter_id={ADAPTER_ID!r}, "
            f"adapter_version={ADAPTER_VERSION!r})"
        )

    def read_account_balance(self) -> BetdaqBalanceObservation:
        result, evidence = self._call("GetAccountBalances", {})
        return BetdaqBalanceObservation(
            currency=_required_attr(result, "Currency"),
            balance=_decimal_attr(result, "Balance"),
            exposure=_decimal_attr(result, "Exposure"),
            available_funds=_decimal_attr(result, "AvailableFunds"),
            credit=_decimal_attr(result, "Credit"),
            evidence=evidence,
        )

    def read_bootstrap_page(
        self,
        sequence_number: int,
        *,
        want_settled_orders_on_unsettled_markets: bool = True,
    ) -> BetdaqBootstrapPage:
        if type(sequence_number) is not int or sequence_number < -1:
            raise BetdaqAccountReadOnlyError("sequence_number must be an integer >= -1")
        if type(want_settled_orders_on_unsettled_markets) is not bool:
            raise BetdaqAccountReadOnlyError(
                "want_settled_orders_on_unsettled_markets must be bool"
            )
        result, evidence = self._call(
            "ListBootstrapOrders",
            {
                "SequenceNumber": str(sequence_number),
                "wantSettledOrdersOnUnsettledMarkets": (
                    "true" if want_settled_orders_on_unsettled_markets else "false"
                ),
            },
        )
        maximum = _integer_attr(result, "MaximumSequenceNumber")
        orders = _parse_orders(result, evidence)
        _require_after_cursor(orders, sequence_number)
        return BetdaqBootstrapPage(maximum, orders, evidence)

    def read_orders_changed_since(self, sequence_number: int) -> BetdaqChangedOrdersPage:
        if type(sequence_number) is not int or sequence_number < -1:
            raise BetdaqAccountReadOnlyError("sequence_number must be an integer >= -1")
        result, evidence = self._call(
            "ListOrdersChangedSince", {"SequenceNumber": str(sequence_number)}
        )
        orders = _parse_orders(result, evidence)
        _require_after_cursor(orders, sequence_number)
        return BetdaqChangedOrdersPage(orders, evidence)

    def read_complete_current_orders(
        self, *, max_bootstrap_pages: int = 100, max_changed_pages: int = 100
    ) -> BetdaqCurrentOrderBook:
        _positive_int(max_bootstrap_pages, "max_bootstrap_pages")
        _positive_int(max_changed_pages, "max_changed_pages")

        all_observations: list[BetdaqOrderObservation] = []
        evidence: list[BetdaqSoapEvidence] = []
        cursor = -1
        bootstrap_cutoff: int | None = None

        for _ in range(max_bootstrap_pages):
            page = self.read_bootstrap_page(cursor)
            evidence.append(page.evidence)
            if bootstrap_cutoff is None:
                bootstrap_cutoff = page.maximum_sequence_number
                if bootstrap_cutoff < cursor:
                    raise BetdaqAccountReadOnlyError(
                        "first bootstrap maximum sequence precedes request cursor"
                    )
            if any(
                item.sequence_number > bootstrap_cutoff for item in page.orders
            ):
                raise BetdaqAccountReadOnlyError(
                    "bootstrap order sequence exceeds the first-call causal cutoff"
                )
            all_observations.extend(page.orders)
            if not page.orders:
                break
            cursor = page.orders[-1].sequence_number
            if cursor >= bootstrap_cutoff:
                break
        else:
            raise BetdaqAccountReadOnlyError(
                "BETDAQ bootstrap exceeded max_bootstrap_pages before completion"
            )

        assert bootstrap_cutoff is not None
        changed_cursor = bootstrap_cutoff
        for _ in range(max_changed_pages):
            page = self.read_orders_changed_since(changed_cursor)
            evidence.append(page.evidence)
            if not page.orders:
                break
            all_observations.extend(page.orders)
            changed_cursor = page.orders[-1].sequence_number
        else:
            raise BetdaqAccountReadOnlyError(
                "BETDAQ changed-since race closure exceeded max_changed_pages"
            )

        current = _fold_latest_orders(all_observations)
        observed_at = _latest_observed_at(evidence)
        payload = {
            "schema": "autosport.betdaq-current-orders-v1",
            "bootstrap_cutoff_sequence": bootstrap_cutoff,
            "final_sequence": changed_cursor,
            "orders": [item.canonical_payload() for item in current],
            "responses": [
                {
                    "method": item.method,
                    "observed_at": item.observed_at,
                    "source_payload_sha256": item.source_payload_sha256,
                }
                for item in evidence
            ],
        }
        digest = _canonical_sha256(payload)
        return BetdaqCurrentOrderBook(
            orders=current,
            bootstrap_cutoff_sequence=bootstrap_cutoff,
            final_sequence=changed_cursor,
            observed_at=observed_at,
            response_evidence=tuple(evidence),
            evidence_sha256=digest,
        )

    def read_account_evidence(
        self,
        requested_capabilities: frozenset[BookmakerCapability],
    ) -> BetdaqAccountEvidence:
        if not isinstance(requested_capabilities, frozenset):
            raise TypeError("requested_capabilities must be a frozenset")
        supported = frozenset(
            {
                BookmakerCapability.BALANCE_READ,
                BookmakerCapability.OPEN_POSITIONS_READ,
                BookmakerCapability.BET_READBACK,
            }
        )
        if any(
            not isinstance(capability, BookmakerCapability)
            for capability in requested_capabilities
        ):
            raise BetdaqAccountReadOnlyError(
                "requested_capabilities must contain BookmakerCapability values"
            )
        unsupported = requested_capabilities - supported
        if unsupported:
            names = ", ".join(sorted(item.value for item in unsupported))
            raise BetdaqAccountReadOnlyError(
                f"BETDAQ read-only adapter cannot prove complete capability: {names}"
            )

        context_before = _authenticated_account_context(
            self._credentials,
            self._venue_id,
        )
        balance = self.read_account_balance()
        order_requested = bool(
            requested_capabilities
            & frozenset(
                {
                    BookmakerCapability.OPEN_POSITIONS_READ,
                    BookmakerCapability.BET_READBACK,
                }
            )
        )
        order_book = self.read_complete_current_orders() if order_requested else None
        context_after = _authenticated_account_context(
            self._credentials,
            self._venue_id,
        )
        if context_after.session_context_id != context_before.session_context_id:
            raise BetdaqAccountReadOnlyError(
                "BETDAQ authenticated account context changed during acquisition"
            )
        account_scope_id = context_before.session_context_id

        # Currency is required by the canonical position contract, so a balance read is
        # always surfaced rather than silently used as hidden account context.
        observed_capabilities = frozenset(
            set(requested_capabilities) | {BookmakerCapability.BALANCE_READ}
        )
        all_evidence = [balance.evidence]
        if order_book is not None:
            all_evidence.extend(order_book.response_evidence)
        observed_at = _latest_observed_at(all_evidence)
        evidence_sha256 = _canonical_sha256(
            {
                "account_context_id": account_scope_id,
                "balance": balance.evidence.source_payload_sha256,
                "orders": None if order_book is None else order_book.evidence_sha256,
            }
        )
        facts = tuple(
            BookmakerCapabilityFact(capability, BookmakerCapabilityState.SUPPORTED)
            for capability in sorted(observed_capabilities, key=lambda item: item.value)
        )
        profile = BookmakerCapabilityProfile(
            context_before.venue_id,
            account_scope_id,
            ADAPTER_ID,
            ADAPTER_VERSION,
            1,
            facts,
            observed_at,
            f"betdaq://account-snapshot/{evidence_sha256}",
            evidence_sha256,
        )
        canonical_balance = BookmakerBalanceObservation(
            context_before.venue_id,
            account_scope_id,
            ADAPTER_ID,
            f"betdaq-balance:{balance.evidence.source_payload_sha256}",
            balance.currency,
            balance.available_funds,
            balance.evidence.observed_at,
            balance.evidence.source_payload_sha256,
            total_balance=None,
            exposure=balance.exposure,
        )

        open_positions: tuple[BookmakerPositionObservation, ...] = ()
        if (
            order_book is not None
            and BookmakerCapability.OPEN_POSITIONS_READ in observed_capabilities
        ):
            open_positions = tuple(
                _to_open_position(
                    item,
                    venue_id=context_before.venue_id,
                    account_id=account_scope_id,
                    currency=balance.currency,
                )
                for item in order_book.orders
                if _projects_as_open_position(item)
            )

        snapshot = BookmakerAccountSnapshot(
            profile,
            observed_capabilities,
            observed_at,
            canonical_balance,
            open_positions,
            (),
        )
        return BetdaqAccountEvidence(
            snapshot,
            balance,
            order_book,
            context_before,
        )

    def read_account_snapshot(
        self,
        requested_capabilities: frozenset[BookmakerCapability],
    ) -> BookmakerAccountSnapshot:
        return self.read_account_evidence(requested_capabilities).snapshot

    def _call(
        self, method: str, request_fields: dict[str, str]
    ) -> tuple[ET.Element, BetdaqSoapEvidence]:
        if method not in _ALLOWED_METHODS:
            raise BetdaqAccountReadOnlyError(
                "BETDAQ SOAP method is outside the strict read-only allowlist"
            )
        with self._call_lock:
            body = self._request_xml(method, request_fields)
            headers = {
                "Accept": "text/xml",
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f'"{_EXTERNAL_NS}{method}"',
            }
            try:
                payload = self._transport.post(
                    _SECURE_ENDPOINT,
                    headers=headers,
                    body=body,
                    timeout_seconds=self._timeout_seconds,
                )
            except Exception:
                # Never propagate a provider/transport exception string: custom transports
                # can accidentally embed credentials or raw secure request XML, even when
                # they use this module's exception class.
                raise BetdaqAccountReadOnlyError(
                    "BETDAQ secure read transport failed"
                ) from None
        if type(payload) is not bytes:
            raise BetdaqAccountReadOnlyError("BETDAQ transport must return bytes")
        evidence = BetdaqSoapEvidence(
            method=method,
            observed_at=self._observed_at(),
            source_payload_sha256=sha256(payload).hexdigest(),
        )
        return _parse_soap_result(payload, method), evidence

    def _request_xml(self, method: str, fields: dict[str, str]) -> bytes:
        ET.register_namespace("soap", _SOAP11_NS)
        envelope = ET.Element(f"{{{_SOAP11_NS}}}Envelope")
        header = ET.SubElement(envelope, f"{{{_SOAP11_NS}}}Header")
        ET.SubElement(
            header,
            f"{{{_EXTERNAL_NS}}}ExternalApiHeader",
            {
                "version": self._credentials.version,
                "languageCode": self._credentials.language_code,
                "username": self._credentials.username,
                "password": self._credentials.password,
                "applicationIdentifier": self._credentials.application_identifier,
            },
        )
        soap_body = ET.SubElement(envelope, f"{{{_SOAP11_NS}}}Body")
        method_element = ET.SubElement(soap_body, f"{{{_EXTERNAL_NS}}}{method}")
        request_names = {
            "GetAccountBalances": "getAccountBalancesRequest",
            "ListBootstrapOrders": "listBootstrapOrdersRequest",
            "ListOrdersChangedSince": "listOrdersChangedSinceRequest",
        }
        request_element = ET.SubElement(
            method_element, f"{{{_EXTERNAL_NS}}}{request_names[method]}"
        )
        for key, value in fields.items():
            element = ET.SubElement(request_element, f"{{{_EXTERNAL_NS}}}{key}")
            element.text = value
        return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)

    def _observed_at(self) -> str:
        value = self._clock()
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise BetdaqAccountReadOnlyError(
                "clock must return a timezone-aware datetime"
            )
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_soap_result(payload: bytes, method: str) -> ET.Element:
    upper = payload.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise BetdaqAccountReadOnlyError("BETDAQ SOAP payload contains forbidden DTD/entity")
    try:
        root = ET.fromstring(payload)
    except (ET.ParseError, UnicodeError):
        raise BetdaqAccountReadOnlyError("BETDAQ response is not valid SOAP XML") from None
    namespace, local = _split_tag(root.tag)
    if local != "Envelope" or namespace not in {_SOAP11_NS, _SOAP12_NS}:
        raise BetdaqAccountReadOnlyError("BETDAQ response has invalid SOAP Envelope")
    bodies = [child for child in root if child.tag == f"{{{namespace}}}Body"]
    if len(bodies) != 1:
        raise BetdaqAccountReadOnlyError("BETDAQ response must contain one SOAP Body")
    body = bodies[0]
    for child in body:
        child_ns, child_local = _split_tag(child.tag)
        if child_ns == namespace and child_local == "Fault":
            raise BetdaqAccountReadOnlyError("BETDAQ SOAP Fault")
    responses = [
        child
        for child in body
        if child.tag == f"{{{_EXTERNAL_NS}}}{method}Response"
    ]
    if len(responses) != 1:
        raise BetdaqAccountReadOnlyError(
            f"BETDAQ response is missing exact {method}Response"
        )
    results = [
        child
        for child in responses[0]
        if child.tag == f"{{{_EXTERNAL_NS}}}{method}Result"
    ]
    if len(results) != 1:
        raise BetdaqAccountReadOnlyError(
            f"BETDAQ response is missing exact {method}Result"
        )
    _require_success_return_status(results[0])
    return results[0]


def _require_success_return_status(result: ET.Element) -> None:
    """Require provider-level success before any method payload can become evidence."""

    statuses = [
        child
        for child in result
        if child.tag == f"{{{_EXTERNAL_NS}}}ReturnStatus"
    ]
    if len(statuses) != 1:
        raise BetdaqAccountReadOnlyError(
            "BETDAQ result must contain exactly one ReturnStatus"
        )
    raw_code = statuses[0].attrib.get("Code")
    if raw_code is None or _INTEGER_RE.fullmatch(raw_code) is None:
        raise BetdaqAccountReadOnlyError(
            "BETDAQ ReturnStatus Code must be provider integer text"
        )
    code = int(raw_code)
    if code != 0:
        # Description can contain provider/account detail. Keep the public error
        # classification stable and retain only the documented numeric status code.
        raise BetdaqAccountReadOnlyError(
            f"BETDAQ provider ReturnStatus reported failure code {code}"
        )


def _parse_orders(
    result: ET.Element, evidence: BetdaqSoapEvidence
) -> tuple[BetdaqOrderObservation, ...]:
    containers = [
        child for child in result if child.tag == f"{{{_EXTERNAL_NS}}}Orders"
    ]
    if len(containers) != 1:
        raise BetdaqAccountReadOnlyError("BETDAQ order result must contain one Orders element")
    orders: list[BetdaqOrderObservation] = []
    for child in containers[0]:
        if child.tag != f"{{{_EXTERNAL_NS}}}Order":
            raise BetdaqAccountReadOnlyError("BETDAQ Orders contains an unexpected element")
        status_code = _integer_attr(child, "Status")
        status_name = _STATUS_NAMES.get(status_code)
        if status_name is None:
            raise BetdaqAccountReadOnlyError("unknown BETDAQ OrderStatus value")
        polarity_code = _integer_attr(child, "Polarity")
        polarity = _POLARITY_NAMES.get(polarity_code)
        if polarity is None:
            raise BetdaqAccountReadOnlyError("unknown BETDAQ Polarity value")
        order = BetdaqOrderObservation(
            order_id=_provider_identifier(_required_attr(child, "Id"), "Id"),
            market_id=_provider_identifier(_required_attr(child, "MarketId"), "MarketId"),
            selection_id=_provider_identifier(
                _required_attr(child, "SelectionId"), "SelectionId"
            ),
            sequence_number=_integer_attr(child, "SequenceNumber"),
            issued_at=_canonical_timestamp(_required_attr(child, "IssuedAt"), "IssuedAt"),
            polarity=polarity,
            unmatched_stake=_decimal_attr(child, "UnmatchedStake"),
            requested_price=_decimal_attr(child, "RequestedPrice"),
            matched_price=_optional_decimal_attr(child, "MatchedPrice"),
            matched_stake=_decimal_attr(child, "MatchedStake"),
            total_for_side_make_stake=_decimal_attr(
                child, "TotalForSideMakeStake"
            ),
            total_for_side_take_stake=_decimal_attr(
                child, "TotalForSideTakeStake"
            ),
            matched_against_stake=_decimal_attr(child, "MatchedAgainstStake"),
            status_code=status_code,
            status_name=status_name,
            punter_reference_number=_provider_identifier(
                _required_attr(child, "PunterReferenceNumber"),
                "PunterReferenceNumber",
            ),
            expected_selection_reset_count=_integer_attr(
                child, "ExpectedSelectionResetCount"
            ),
            expected_withdrawal_sequence_number=_integer_attr(
                child, "ExpectedWithdrawalSequenceNumber"
            ),
            evidence=evidence,
        )
        orders.append(order)
    return tuple(orders)


def _fold_latest_orders(
    observations: list[BetdaqOrderObservation],
) -> tuple[BetdaqOrderObservation, ...]:
    latest: dict[str, BetdaqOrderObservation] = {}
    for item in observations:
        previous = latest.get(item.order_id)
        if previous is None:
            latest[item.order_id] = item
            continue
        if item.sequence_number < previous.sequence_number:
            raise BetdaqAccountReadOnlyError(
                "BETDAQ order sequence regressed across acquisition"
            )
        if item.sequence_number == previous.sequence_number:
            if item.canonical_payload() != previous.canonical_payload():
                raise BetdaqAccountReadOnlyError(
                    "same BETDAQ order/sequence has conflicting content"
                )
            continue
        if previous.status_code == 3 and item.status_code in {1, 2, 6}:
            raise BetdaqAccountReadOnlyError(
                "newer BETDAQ evidence reopens a cancelled order"
            )
        if (
            previous.status_code in _TERMINAL_STATUS_CODES
            and item.status_code not in _TERMINAL_STATUS_CODES
        ):
            raise BetdaqAccountReadOnlyError(
                "newer BETDAQ evidence regresses a terminal order state"
            )
        latest[item.order_id] = item
    return tuple(
        sorted(
            latest.values(),
            key=lambda item: (item.sequence_number, item.order_id),
        )
    )


def _projects_as_open_position(order: BetdaqOrderObservation) -> bool:
    if order.status_code in {1, 2, 6}:
        return True
    if order.status_code == 3:
        # Cancelled unmatched-only orders have no remaining economic position. A
        # cancelled order with a matched portion remains unsettled until explicit
        # settlement evidence arrives.
        return order.matched_stake > 0
    return False


def _to_open_position(
    order: BetdaqOrderObservation,
    *,
    venue_id: str,
    account_id: str,
    currency: str,
) -> BookmakerPositionObservation:
    # Current UNMATCHED/MATCHED/SUSPENDED orders can still carry an unmatched
    # remainder.  That quantity remains provider-side order exposure/reservation
    # evidence and must not disappear merely because BookmakerPositionObservation
    # has one provider_amount field.  A cancelled order's unmatched remainder is no
    # longer live, so only its already-matched portion remains economically relevant.
    active_unmatched = (
        order.unmatched_stake if order.status_code in {1, 2, 6} else Decimal(0)
    )
    provider_amount = _exact_decimal_sum(order.matched_stake, active_unmatched)

    # One decimal_odds value cannot truthfully represent both the average matched
    # price and a different requested price for a still-unmatched remainder.
    if order.matched_stake > 0 and active_unmatched > 0:
        odds = None
    elif order.matched_stake > 0:
        odds = order.matched_price
    elif active_unmatched > 0:
        odds = order.requested_price
    else:
        odds = None

    return BookmakerPositionObservation(
        venue_id,
        account_id,
        ADAPTER_ID,
        order.observation_id,
        order.order_id,
        BookmakerPositionState.OPEN,
        currency,
        order.evidence.observed_at,
        order.evidence.source_payload_sha256,
        provider_amount=provider_amount,
        provider_amount_semantics="betdaq_matched_plus_active_unmatched_stake",
        provider_side=order.polarity,
        decimal_odds=odds,
    )


def _exact_decimal_sum(left: Decimal, right: Decimal) -> Decimal:
    """Add provider Decimal quantities without ambient-context rounding."""

    left = _finite_decimal(left, "left")
    right = _finite_decimal(right, "right")

    def signed_coefficient(value: Decimal) -> tuple[int, int]:
        sign, digits, exponent = value.as_tuple()
        coefficient = 0
        for digit in digits:
            coefficient = (coefficient * 10) + digit
        return (-coefficient if sign else coefficient), exponent

    left_coefficient, left_exponent = signed_coefficient(left)
    right_coefficient, right_exponent = signed_coefficient(right)
    common_exponent = min(left_exponent, right_exponent)
    left_coefficient *= 10 ** (left_exponent - common_exponent)
    right_coefficient *= 10 ** (right_exponent - common_exponent)
    coefficient = left_coefficient + right_coefficient

    if coefficient == 0:
        return Decimal(0)

    while coefficient % 10 == 0:
        coefficient //= 10
        common_exponent += 1

    sign = 1 if coefficient < 0 else 0
    digits = tuple(int(character) for character in str(abs(coefficient)))
    return Decimal((sign, digits, common_exponent))


def _validate_strict_sequence(
    orders: tuple[BetdaqOrderObservation, ...], field: str
) -> None:
    previous: int | None = None
    for item in orders:
        if not isinstance(item, BetdaqOrderObservation):
            raise BetdaqAccountReadOnlyError(f"{field} has invalid item")
        if previous is not None and item.sequence_number <= previous:
            raise BetdaqAccountReadOnlyError(
                f"{field} must be strictly ordered by sequence number"
            )
        previous = item.sequence_number


def _require_after_cursor(
    orders: tuple[BetdaqOrderObservation, ...], cursor: int
) -> None:
    if orders and orders[0].sequence_number <= cursor:
        raise BetdaqAccountReadOnlyError(
            "BETDAQ returned an order not strictly after the requested sequence"
        )


def _latest_observed_at(evidence: list[BetdaqSoapEvidence]) -> str:
    if not evidence:
        raise BetdaqAccountReadOnlyError("cannot derive time from empty evidence")
    latest = max(
        evidence, key=lambda item: _timestamp(item.observed_at, "observed_at")
    )
    return latest.observed_at


def _required_attr(element: ET.Element, name: str) -> str:
    if name not in element.attrib:
        raise BetdaqAccountReadOnlyError(f"BETDAQ response is missing {name}")
    return _required_text(element.attrib[name], name)


def _optional_decimal_attr(element: ET.Element, name: str) -> Decimal | None:
    raw = element.attrib.get(name)
    if raw is None or raw == "":
        return None
    return _decimal_text_value(raw, name)


def _decimal_attr(element: ET.Element, name: str) -> Decimal:
    return _decimal_text_value(_required_attr(element, name), name)


def _decimal_text_value(value: str, field: str) -> Decimal:
    try:
        result = Decimal(value)
    except (InvalidOperation, ValueError):
        raise BetdaqAccountReadOnlyError(f"{field} must be provider Decimal text") from None
    if not result.is_finite():
        raise BetdaqAccountReadOnlyError(f"{field} must be finite")
    return result


def _integer_attr(element: ET.Element, name: str) -> int:
    raw = _required_attr(element, name)
    if _INTEGER_RE.fullmatch(raw) is None:
        raise BetdaqAccountReadOnlyError(f"{name} must be provider integer text")
    return int(raw)


def _provider_identifier(value: str, field: str) -> str:
    text = _required_text(value, field)
    if _INTEGER_RE.fullmatch(text) is None:
        raise BetdaqAccountReadOnlyError(f"{field} must be provider integer identity text")
    return str(int(text))


def _required_text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise BetdaqAccountReadOnlyError(f"{field} must be non-empty trimmed text")
    value.encode("utf-8")
    return value


def _currency(value: str) -> str:
    text = _required_text(value, "currency")
    if (
        len(text) != 3
        or not text.isascii()
        or not text.isalpha()
        or text != text.upper()
    ):
        raise BetdaqAccountReadOnlyError("currency must be a 3-letter uppercase code")
    return text


def _finite_decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise BetdaqAccountReadOnlyError(f"{field} must be a finite Decimal")
    return value


def _positive_int(value: object, field: str) -> int:
    if type(value) is not int or value <= 0:
        raise BetdaqAccountReadOnlyError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if type(value) is not int or value < 0:
        raise BetdaqAccountReadOnlyError(f"{field} must be a non-negative integer")
    return value


def _timestamp(value: str, field: str) -> datetime:
    text = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise BetdaqAccountReadOnlyError(f"{field} must be ISO-8601") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetdaqAccountReadOnlyError(f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)


def _canonical_timestamp(value: str, field: str) -> str:
    return _timestamp(value, field).isoformat().replace("+00:00", "Z")


def _sha256_hex(value: str, field: str) -> str:
    text = _required_text(value, field)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise BetdaqAccountReadOnlyError(f"{field} must be lowercase SHA-256 hex")
    return text


def _canonical_sha256(value: object) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(raw).hexdigest()


def _decimal_text(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise BetdaqAccountReadOnlyError("decimal evidence must be finite")
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
        text = (
            f"{coefficient[:point]}.{coefficient[point:]}"
            if point > 0
            else f"0.{('0' * -point)}{coefficient}"
        )
    return f"-{text}" if sign else text


def _split_tag(tag: str) -> tuple[str, str]:
    if not tag.startswith("{") or "}" not in tag:
        return "", tag
    namespace, local = tag[1:].split("}", 1)
    return namespace, local

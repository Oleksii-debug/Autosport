"""Authenticated Betfair provider/fixed-billing observations.

This module extends the existing exact :class:`BetfairReadOnlyClient` with a
narrow read capability for developer-app entitlement and account-statement
evidence.  It does not turn public tariffs, missing rows, or shared/fixed charges
into per-opportunity money.  The observations are source evidence only; economic
allocation remains fail-closed until a product-owned policy binds it.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from typing import Callable, Mapping

from .betfair_account_readonly import (
    ACCOUNT_JSON_RPC_ENDPOINT,
    BetfairEvidence,
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)

_GET_ACCOUNT_DETAILS = "AccountAPING/v1.0/getAccountDetails"
_GET_DEVELOPER_APP_KEYS = "AccountAPING/v1.0/getDeveloperAppKeys"
_GET_ACCOUNT_STATEMENT = "AccountAPING/v1.0/getAccountStatement"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class BetfairDeveloperAppEntitlementObservation:
    """Provider-owned state for the exact application key in use.

    The raw application key is intentionally not retained.  Matching is performed
    against the exact authenticated credential before the SHA-256 fingerprint is
    emitted.
    """

    venue_id: str
    account_id: str
    application_key_sha256: str
    app_id: int
    app_name: str
    version_id: int
    version: str
    delay_data: bool
    subscription_required: bool
    owner_managed: bool
    active: bool
    vendor_id: int | None
    evidence: BetfairEvidence

    def __post_init__(self) -> None:
        _required_text(self.venue_id, "venue_id")
        _required_text(self.account_id, "account_id")
        _sha256_hex(self.application_key_sha256, "application_key_sha256")
        _positive_int(self.app_id, "app_id")
        _required_text(self.app_name, "app_name")
        _positive_int(self.version_id, "version_id")
        _required_text(self.version, "version")
        for value, field in (
            (self.delay_data, "delay_data"),
            (self.subscription_required, "subscription_required"),
            (self.owner_managed, "owner_managed"),
            (self.active, "active"),
        ):
            if type(value) is not bool:
                raise BetfairReadOnlyError(f"{field} must be bool")
        if self.vendor_id is not None:
            _positive_int(self.vendor_id, "vendor_id")
        if type(self.evidence) is not BetfairEvidence:
            raise BetfairReadOnlyError("entitlement evidence must be exact BetfairEvidence")


@dataclass(frozen=True, slots=True)
class BetfairAccountStatementItemObservation:
    """One provider-native account-statement row.

    ``amount`` and ``balance`` preserve provider units in the account currency.
    ``item_class_data_sha256`` binds the provider detail object without retaining
    arbitrary account text in the product evidence surface.
    """

    ref_id: str
    item_date: str
    amount: Decimal
    balance: Decimal
    item_class: str
    item_class_data_sha256: str

    def __post_init__(self) -> None:
        _required_text(self.ref_id, "ref_id")
        _instant(self.item_date, "item_date")
        _decimal(self.amount, "amount")
        _decimal(self.balance, "balance")
        _required_text(self.item_class, "item_class")
        _sha256_hex(self.item_class_data_sha256, "item_class_data_sha256")


@dataclass(frozen=True, slots=True)
class BetfairAccountStatementPageObservation:
    venue_id: str
    account_id: str
    currency_code: str
    from_record: int
    record_count: int
    items: tuple[BetfairAccountStatementItemObservation, ...]
    more_available: bool
    evidence: BetfairEvidence

    def __post_init__(self) -> None:
        _required_text(self.venue_id, "venue_id")
        _required_text(self.account_id, "account_id")
        currency = _required_text(self.currency_code, "currency_code")
        if not currency.isascii() or currency != currency.upper():
            raise BetfairReadOnlyError("currency_code must be uppercase ASCII")
        _nonnegative_int(self.from_record, "from_record")
        _positive_int(self.record_count, "record_count")
        if type(self.items) is not tuple or any(
            type(item) is not BetfairAccountStatementItemObservation
            for item in self.items
        ):
            raise BetfairReadOnlyError("statement items must be exact canonical tuple")
        if type(self.more_available) is not bool:
            raise BetfairReadOnlyError("more_available must be bool")
        if type(self.evidence) is not BetfairEvidence:
            raise BetfairReadOnlyError("statement evidence must be exact BetfairEvidence")


@dataclass(frozen=True, slots=True)
class BetfairProviderBillingInputsObservation:
    """Authenticated entitlement + statement evidence, not cost authority."""

    entitlement: BetfairDeveloperAppEntitlementObservation
    statement: BetfairAccountStatementPageObservation
    observed_at: str
    evidence_sha256: str

    def __post_init__(self) -> None:
        if type(self.entitlement) is not BetfairDeveloperAppEntitlementObservation:
            raise BetfairReadOnlyError("entitlement must be exact canonical observation")
        if type(self.statement) is not BetfairAccountStatementPageObservation:
            raise BetfairReadOnlyError("statement must be exact canonical observation")
        if (
            self.entitlement.venue_id != self.statement.venue_id
            or self.entitlement.account_id != self.statement.account_id
        ):
            raise BetfairReadOnlyError("provider billing observations disagree on account identity")
        _instant(self.observed_at, "observed_at")
        _sha256_hex(self.evidence_sha256, "evidence_sha256")
        expected = _combined_evidence_sha256(self.entitlement, self.statement)
        if self.evidence_sha256 != expected:
            raise BetfairReadOnlyError("provider billing combined evidence digest mismatch")


@dataclass(frozen=True, slots=True)
class _PinnedClient:
    venue_id: str
    account_id: str
    application_key: str
    session_token: str
    timeout_seconds: float
    post: Callable[..., bytes]
    next_request_id: Callable[[], int]
    observed_at: Callable[[], str]


@dataclass(frozen=True, slots=True)
class _RpcRead:
    result: object
    evidence: BetfairEvidence


def read_betfair_provider_billing_inputs(
    client: BetfairReadOnlyClient,
    *,
    from_record: int = 0,
    record_count: int = 100,
    statement_from: str | None = None,
    statement_to: str | None = None,
) -> BetfairProviderBillingInputsObservation:
    """Capture exact current key entitlement and account-statement evidence.

    The function uses only the exact canonical client's already-authorized
    credentials, transport, clock, account identity, and request-id authority.
    It exposes no provider write method.  Absence of a billing row is merely
    absence of evidence and must never be interpreted as zero cost.
    """

    _nonnegative_int(from_record, "from_record")
    _positive_int(record_count, "record_count")
    if record_count > 100:
        raise BetfairReadOnlyError("record_count exceeds provider statement page limit")
    if statement_from is not None:
        _instant(statement_from, "statement_from")
    if statement_to is not None:
        _instant(statement_to, "statement_to")
    if statement_from is not None and statement_to is not None:
        if _instant(statement_from, "statement_from") > _instant(statement_to, "statement_to"):
            raise BetfairReadOnlyError("statement_from must not be after statement_to")

    pinned = _snapshot_client(client)
    details = _read_rpc(pinned, _GET_ACCOUNT_DETAILS, {})
    details_result = _mapping(details.result, "getAccountDetails result")
    currency_code = _provider_text(details_result, "currencyCode", "currency_code")
    if not currency_code.isascii() or currency_code != currency_code.upper():
        raise BetfairReadOnlyError("currency_code must be uppercase ASCII")

    developer = _read_rpc(pinned, _GET_DEVELOPER_APP_KEYS, {})
    entitlement = _resolve_exact_entitlement(pinned, developer)

    statement_params: dict[str, object] = {
        "fromRecord": from_record,
        "recordCount": record_count,
    }
    if statement_from is not None or statement_to is not None:
        item_date_range: dict[str, str] = {}
        if statement_from is not None:
            item_date_range["from"] = statement_from
        if statement_to is not None:
            item_date_range["to"] = statement_to
        statement_params["itemDateRange"] = item_date_range

    statement_rpc = _read_rpc(pinned, _GET_ACCOUNT_STATEMENT, statement_params)
    statement_result = _mapping(statement_rpc.result, "getAccountStatement result")
    raw_items = statement_result.get("accountStatement")
    if type(raw_items) is not list:
        raise BetfairReadOnlyError("accountStatement must be a JSON array")
    items = tuple(_parse_statement_item(item, index) for index, item in enumerate(raw_items))
    more_available = statement_result.get("moreAvailable")
    if type(more_available) is not bool:
        raise BetfairReadOnlyError("statement moreAvailable must be bool")
    statement = BetfairAccountStatementPageObservation(
        venue_id=pinned.venue_id,
        account_id=pinned.account_id,
        currency_code=currency_code,
        from_record=from_record,
        record_count=record_count,
        items=items,
        more_available=more_available,
        evidence=statement_rpc.evidence,
    )
    observed_at = max(
        entitlement.evidence.observed_at,
        statement.evidence.observed_at,
        details.evidence.observed_at,
        key=lambda value: _instant(value, "observed_at"),
    )
    return BetfairProviderBillingInputsObservation(
        entitlement=entitlement,
        statement=statement,
        observed_at=observed_at,
        evidence_sha256=_combined_evidence_sha256(entitlement, statement),
    )


def _snapshot_client(client: object) -> _PinnedClient:
    if type(client) is not BetfairReadOnlyClient:
        raise TypeError("client must be exact BetfairReadOnlyClient")
    state = vars(client).copy()
    if any(name in state for name in ("_next_request_id", "_observed_at", "_rpc")):
        raise BetfairReadOnlyError("BetfairReadOnlyClient read authority is instance-shadowed")
    credentials = state.get("_credentials")
    if type(credentials) is not BetfairSessionCredentials:
        raise BetfairReadOnlyError("client credentials are not exact canonical credentials")
    venue_id = _required_text(state.get("_venue_id"), "venue_id")
    account_id = _required_text(state.get("_account_id"), "account_id")
    transport = state.get("_transport")
    post = getattr(transport, "post", None)
    if not callable(post):
        raise BetfairReadOnlyError("client transport post capability is unavailable")
    timeout = state.get("_timeout_seconds")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise BetfairReadOnlyError("client timeout is invalid")
    next_request_id = BetfairReadOnlyClient._next_request_id.__get__(client, BetfairReadOnlyClient)
    observed_at = BetfairReadOnlyClient._observed_at.__get__(client, BetfairReadOnlyClient)
    return _PinnedClient(
        venue_id=venue_id,
        account_id=account_id,
        application_key=credentials.application_key,
        session_token=credentials.session_token,
        timeout_seconds=float(timeout),
        post=post,
        next_request_id=next_request_id,
        observed_at=observed_at,
    )


def _read_rpc(pinned: _PinnedClient, method: str, params: Mapping[str, object]) -> _RpcRead:
    if method not in {_GET_ACCOUNT_DETAILS, _GET_DEVELOPER_APP_KEYS, _GET_ACCOUNT_STATEMENT}:
        raise BetfairReadOnlyError("provider billing RPC is outside the strict read-only allowlist")
    request_id = pinned.next_request_id()
    if isinstance(request_id, bool) or not isinstance(request_id, int):
        raise BetfairReadOnlyError("canonical request id is invalid")
    body = json.dumps(
        {"jsonrpc": "2.0", "method": method, "params": dict(params), "id": request_id},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload = pinned.post(
        ACCOUNT_JSON_RPC_ENDPOINT,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Application": pinned.application_key,
            "X-Authentication": pinned.session_token,
        },
        body=body,
        timeout_seconds=pinned.timeout_seconds,
    )
    if type(payload) is not bytes:
        raise BetfairReadOnlyError("Betfair transport must return bytes")
    observed_at = pinned.observed_at()
    _instant(observed_at, "observed_at")
    evidence = BetfairEvidence(observed_at, sha256(payload).hexdigest())
    try:
        decoded = json.loads(payload.decode("utf-8"), parse_float=Decimal, parse_int=int)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BetfairReadOnlyError("Betfair response is not valid UTF-8 JSON") from exc
    envelope = _mapping(decoded, "JSON-RPC response")
    if envelope.get("jsonrpc") != "2.0" or envelope.get("id") != request_id:
        raise BetfairReadOnlyError("Betfair response envelope does not match request")
    if "error" in envelope:
        raise BetfairReadOnlyError("Betfair provider returned an RPC error")
    if "result" not in envelope:
        raise BetfairReadOnlyError("Betfair response is missing result")
    return _RpcRead(envelope["result"], evidence)


def _resolve_exact_entitlement(
    pinned: _PinnedClient,
    rpc: _RpcRead,
) -> BetfairDeveloperAppEntitlementObservation:
    raw_apps = rpc.result
    if type(raw_apps) is not list:
        raise BetfairReadOnlyError("getDeveloperAppKeys result must be a JSON array")
    matches: list[tuple[Mapping[str, object], Mapping[str, object]]] = []
    for app_index, raw_app in enumerate(raw_apps):
        app = _mapping(raw_app, f"developerApps[{app_index}]")
        versions = app.get("appVersions")
        if type(versions) is not list:
            raise BetfairReadOnlyError("developer app versions must be a JSON array")
        for version_index, raw_version in enumerate(versions):
            version = _mapping(raw_version, f"developerApps[{app_index}].appVersions[{version_index}]")
            application_key = _provider_text(version, "applicationKey", "application_key")
            if application_key == pinned.application_key:
                matches.append((app, version))
    if len(matches) != 1:
        raise BetfairReadOnlyError("exact authenticated application key entitlement is ambiguous or missing")
    app, version = matches[0]
    vendor_id = version.get("vendorId")
    if vendor_id is not None:
        _positive_int(vendor_id, "vendor_id")
    return BetfairDeveloperAppEntitlementObservation(
        venue_id=pinned.venue_id,
        account_id=pinned.account_id,
        application_key_sha256=sha256(pinned.application_key.encode("utf-8")).hexdigest(),
        app_id=_provider_positive_int(app, "appId", "app_id"),
        app_name=_provider_text(app, "appName", "app_name"),
        version_id=_provider_positive_int(version, "versionId", "version_id"),
        version=_provider_text(version, "version", "version"),
        delay_data=_provider_bool(version, "delayData", "delay_data"),
        subscription_required=_provider_bool(
            version, "subscriptionRequired", "subscription_required"
        ),
        owner_managed=_provider_bool(version, "ownerManaged", "owner_managed"),
        active=_provider_bool(version, "active", "active"),
        vendor_id=vendor_id,
        evidence=rpc.evidence,
    )


def _parse_statement_item(value: object, index: int) -> BetfairAccountStatementItemObservation:
    row = _mapping(value, f"accountStatement[{index}]")
    item_class_data = row.get("itemClassData")
    if item_class_data is None:
        item_class_data = {}
    if not isinstance(item_class_data, Mapping) or any(
        type(key) is not str for key in item_class_data
    ):
        raise BetfairReadOnlyError("statement itemClassData must be a JSON object")
    detail_digest = sha256(
        json.dumps(
            item_class_data,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return BetfairAccountStatementItemObservation(
        ref_id=_provider_text(row, "refId", "ref_id"),
        item_date=_provider_text(row, "itemDate", "item_date"),
        amount=_provider_decimal(row, "amount", "amount"),
        balance=_provider_decimal(row, "balance", "balance"),
        item_class=_provider_text(row, "itemClass", "item_class"),
        item_class_data_sha256=detail_digest,
    )


def _combined_evidence_sha256(
    entitlement: BetfairDeveloperAppEntitlementObservation,
    statement: BetfairAccountStatementPageObservation,
) -> str:
    payload = {
        "schema": "autosport.betfair_provider_billing_inputs",
        "schema_version": 1,
        "venue_id": entitlement.venue_id,
        "account_id": entitlement.account_id,
        "application_key_sha256": entitlement.application_key_sha256,
        "entitlement_payload_sha256": entitlement.evidence.source_payload_sha256,
        "statement_payload_sha256": statement.evidence.source_payload_sha256,
        "currency_code": statement.currency_code,
        "statement_rows": [
            {
                "ref_id": item.ref_id,
                "item_date": item.item_date,
                "amount": str(item.amount),
                "balance": str(item.balance),
                "item_class": item.item_class,
                "item_class_data_sha256": item.item_class_data_sha256,
            }
            for item in statement.items
        ],
    }
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(type(key) is not str for key in value):
        raise BetfairReadOnlyError(f"{field} must be a JSON object")
    return value


def _provider_text(value: Mapping[str, object], key: str, field: str) -> str:
    if key not in value:
        raise BetfairReadOnlyError(f"{field} is missing from provider response")
    return _required_text(value[key], field)


def _provider_positive_int(value: Mapping[str, object], key: str, field: str) -> int:
    if key not in value:
        raise BetfairReadOnlyError(f"{field} is missing from provider response")
    return _positive_int(value[key], field)


def _provider_bool(value: Mapping[str, object], key: str, field: str) -> bool:
    if key not in value or type(value[key]) is not bool:
        raise BetfairReadOnlyError(f"{field} must be provider bool")
    return value[key]


def _provider_decimal(value: Mapping[str, object], key: str, field: str) -> Decimal:
    if key not in value:
        raise BetfairReadOnlyError(f"{field} is missing from provider response")
    raw = value[key]
    if isinstance(raw, bool):
        raise BetfairReadOnlyError(f"{field} must be provider number")
    try:
        result = raw if type(raw) is Decimal else Decimal(str(raw))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise BetfairReadOnlyError(f"{field} must be provider number") from exc
    return _decimal(result, field)


def _required_text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise BetfairReadOnlyError(f"{field} must be a non-empty canonical string")
    return value


def _sha256_hex(value: object, field: str) -> str:
    text = _required_text(value, field)
    if _SHA256_RE.fullmatch(text) is None:
        raise BetfairReadOnlyError(f"{field} must be lowercase SHA-256")
    return text


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BetfairReadOnlyError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BetfairReadOnlyError(f"{field} must be a non-negative integer")
    return value


def _decimal(value: object, field: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise BetfairReadOnlyError(f"{field} must be a finite Decimal")
    return value


def _instant(value: object, field: str) -> datetime:
    if type(value) is not str:
        raise BetfairReadOnlyError(f"{field} must be an ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BetfairReadOnlyError(f"{field} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BetfairReadOnlyError(f"{field} must be timezone-aware")
    return parsed.astimezone(timezone.utc)

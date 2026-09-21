"""Authenticated Betfair provider/fixed-billing source observations.

This module deliberately stops before economic attribution or allocation. It reads
provider-owned application-key entitlement and account-statement evidence through
an already-authorized :class:`BetfairReadOnlyClient`, but it never turns a public
tariff, a missing row, an amount/date coincidence, or a caller label into cost truth.

The Accounts API reads consumed here do not expose a provider-owned literal account
identifier. Consequently the caller-configured ``BetfairReadOnlyClient.account_id``
label is intentionally not emitted. A later billing issuer must still re-resolve an
account identity, causally attribute a statement row to a billed product, and apply
a product-owned allocation policy before any positive economic authority can exist.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from functools import partial
from hashlib import sha256
import json
import re
from types import MethodType, SimpleNamespace
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
_CLIENT_AUTHORITY_METHODS = frozenset(
    {"_rpc", "_next_request_id", "_observed_at", "_redact_provider_message"}
)


# Primitive validators are defined before DTOs so each dataclass can capture its
# exact validation helpers in default arguments. Provider callbacks can therefore
# rebind module mirrors without changing the executable set of an in-flight object.
def _required_text(
    value: object,
    field: str,
    *,
    error_cls=BetfairReadOnlyError,
) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.strip()
        or "\x00" in value
    ):
        raise error_cls(f"{field} must be a non-empty canonical string")
    return value


def _sha256_hex(
    value: object,
    field: str,
    *,
    required_text=_required_text,
    pattern=_SHA256_RE,
    error_cls=BetfairReadOnlyError,
) -> str:
    text = required_text(value, field)
    if pattern.fullmatch(text) is None:
        raise error_cls(f"{field} must be lowercase SHA-256")
    return text


def _positive_int(
    value: object,
    field: str,
    *,
    error_cls=BetfairReadOnlyError,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise error_cls(f"{field} must be a positive integer")
    return value


def _nonnegative_int(
    value: object,
    field: str,
    *,
    error_cls=BetfairReadOnlyError,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise error_cls(f"{field} must be a non-negative integer")
    return value


def _statement_record_count(
    value: object,
    *,
    positive_int=_positive_int,
    error_cls=BetfairReadOnlyError,
) -> int:
    count = positive_int(value, "record_count")
    if count > 100:
        raise error_cls("record_count exceeds provider statement page limit")
    return count


def _decimal(
    value: object,
    field: str,
    *,
    decimal_cls=Decimal,
    error_cls=BetfairReadOnlyError,
) -> Decimal:
    if type(value) is not decimal_cls or not value.is_finite():
        raise error_cls(f"{field} must be a finite Decimal")
    return value


def _instant(
    value: object,
    field: str,
    *,
    datetime_cls=datetime,
    utc=timezone.utc,
    error_cls=BetfairReadOnlyError,
) -> datetime:
    if type(value) is not str:
        raise error_cls(f"{field} must be an ISO-8601 string")
    try:
        parsed = datetime_cls.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise error_cls(f"{field} must be valid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise error_cls(f"{field} must be timezone-aware")
    return parsed.astimezone(utc)


def _optional_instant(
    value: object,
    field: str,
    *,
    instant=_instant,
) -> datetime | None:
    return None if value is None else instant(value, field)


def _mapping(
    value: object,
    field: str,
    *,
    mapping_cls=Mapping,
    error_cls=BetfairReadOnlyError,
) -> Mapping[str, object]:
    if not isinstance(value, mapping_cls) or any(
        type(key) is not str for key in value
    ):
        raise error_cls(f"{field} must be a JSON object")
    return value


def _statement_params(
    *,
    from_record: int,
    record_count: int,
    statement_from: str | None,
    statement_to: str | None,
) -> dict[str, object]:
    params: dict[str, object] = {
        "fromRecord": from_record,
        "recordCount": record_count,
    }
    if statement_from is not None or statement_to is not None:
        date_range: dict[str, str] = {}
        if statement_from is not None:
            date_range["from"] = statement_from
        if statement_to is not None:
            date_range["to"] = statement_to
        params["itemDateRange"] = date_range
    return params


def _statement_request_scope_sha256(
    *,
    from_record: int,
    record_count: int,
    statement_from: str | None,
    statement_to: str | None,
    statement_params=_statement_params,
    method=_GET_ACCOUNT_STATEMENT,
    dumps=json.dumps,
    hash_ctor=sha256,
) -> str:
    payload = {
        "method": method,
        "params": statement_params(
            from_record=from_record,
            record_count=record_count,
            statement_from=statement_from,
            statement_to=statement_to,
        ),
    }
    return hash_ctor(
        dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class BetfairDeveloperAppEntitlementObservation:
    """Provider-owned state for the exact application key in use."""

    venue_id: str
    application_key_sha256: str
    app_id: int
    app_name: str
    version_id: int
    version: str
    delay_data: bool
    subscription_required: bool
    owner_managed: bool
    active: bool
    vendor_id: str | None
    evidence: BetfairEvidence

    def __post_init__(
        self,
        required_text=_required_text,
        sha256_hex=_sha256_hex,
        positive_int=_positive_int,
        evidence_cls=BetfairEvidence,
        error_cls=BetfairReadOnlyError,
    ) -> None:
        required_text(self.venue_id, "venue_id")
        sha256_hex(self.application_key_sha256, "application_key_sha256")
        positive_int(self.app_id, "app_id")
        required_text(self.app_name, "app_name")
        positive_int(self.version_id, "version_id")
        required_text(self.version, "version")
        for value, field in (
            (self.delay_data, "delay_data"),
            (self.subscription_required, "subscription_required"),
            (self.owner_managed, "owner_managed"),
            (self.active, "active"),
        ):
            if type(value) is not bool:
                raise error_cls(f"{field} must be bool")
        if self.vendor_id is not None:
            required_text(self.vendor_id, "vendor_id")
        if type(self.evidence) is not evidence_cls:
            raise error_cls("entitlement evidence must be exact BetfairEvidence")


@dataclass(frozen=True, slots=True)
class BetfairAccountStatementItemObservation:
    """One provider-native statement row, without semantic cost attribution."""

    ref_id: str
    item_date: str
    amount: Decimal
    balance: Decimal
    item_class: str
    item_class_data_sha256: str

    def __post_init__(
        self,
        required_text=_required_text,
        instant=_instant,
        decimal_value=_decimal,
        sha256_hex=_sha256_hex,
    ) -> None:
        required_text(self.ref_id, "ref_id")
        instant(self.item_date, "item_date")
        decimal_value(self.amount, "amount")
        decimal_value(self.balance, "balance")
        required_text(self.item_class, "item_class")
        sha256_hex(self.item_class_data_sha256, "item_class_data_sha256")


@dataclass(frozen=True, slots=True)
class BetfairAccountStatementPageObservation:
    """One exact requested statement page and its causal request scope."""

    venue_id: str
    currency_code: str
    from_record: int
    record_count: int
    statement_from: str | None
    statement_to: str | None
    request_scope_sha256: str
    items: tuple[BetfairAccountStatementItemObservation, ...]
    more_available: bool
    account_details_evidence: BetfairEvidence
    evidence: BetfairEvidence

    def __post_init__(
        self,
        required_text=_required_text,
        nonnegative_int=_nonnegative_int,
        statement_record_count=_statement_record_count,
        optional_instant=_optional_instant,
        instant=_instant,
        sha256_hex=_sha256_hex,
        request_scope_sha256=_statement_request_scope_sha256,
        item_cls=BetfairAccountStatementItemObservation,
        evidence_cls=BetfairEvidence,
        error_cls=BetfairReadOnlyError,
    ) -> None:
        required_text(self.venue_id, "venue_id")
        currency = required_text(self.currency_code, "currency_code")
        if not currency.isascii() or currency != currency.upper():
            raise error_cls("currency_code must be uppercase ASCII")
        nonnegative_int(self.from_record, "from_record")
        statement_record_count(self.record_count)
        optional_instant(self.statement_from, "statement_from")
        optional_instant(self.statement_to, "statement_to")
        if self.statement_from is not None and self.statement_to is not None:
            if instant(self.statement_from, "statement_from") > instant(
                self.statement_to, "statement_to"
            ):
                raise error_cls("statement_from must not be after statement_to")
        sha256_hex(self.request_scope_sha256, "request_scope_sha256")
        if self.request_scope_sha256 != request_scope_sha256(
            from_record=self.from_record,
            record_count=self.record_count,
            statement_from=self.statement_from,
            statement_to=self.statement_to,
        ):
            raise error_cls("statement request scope digest mismatch")
        if type(self.items) is not tuple or any(
            type(item) is not item_cls for item in self.items
        ):
            raise error_cls("statement items must be exact canonical tuple")
        if len(self.items) > self.record_count:
            raise error_cls("statement response exceeds requested record_count")
        if type(self.more_available) is not bool:
            raise error_cls("more_available must be bool")
        if type(self.account_details_evidence) is not evidence_cls:
            raise error_cls(
                "account-details evidence must be exact BetfairEvidence"
            )
        if type(self.evidence) is not evidence_cls:
            raise error_cls("statement evidence must be exact BetfairEvidence")


@dataclass(frozen=True, slots=True)
class BetfairProviderBillingInputsObservation:
    """Authenticated source bundle; never prospective cost/allocation authority."""

    entitlement: BetfairDeveloperAppEntitlementObservation
    statement: BetfairAccountStatementPageObservation
    observed_at: str
    evidence_sha256: str

    def __post_init__(
        self,
        entitlement_cls=BetfairDeveloperAppEntitlementObservation,
        statement_cls=BetfairAccountStatementPageObservation,
        instant=_instant,
        sha256_hex=_sha256_hex,
        error_cls=BetfairReadOnlyError,
        combined_sha256=None,
    ) -> None:
        # ``combined_sha256`` cannot reference the later helper at class definition.
        # The public producer recomputes and validates the digest before returning;
        # caller construction remains a non-authoritative DTO regardless.
        if type(self.entitlement) is not entitlement_cls:
            raise error_cls("entitlement must be exact canonical observation")
        if type(self.statement) is not statement_cls:
            raise error_cls("statement must be exact canonical observation")
        if self.entitlement.venue_id != self.statement.venue_id:
            raise error_cls(
                "provider billing observations disagree on venue identity"
            )
        instant(self.observed_at, "observed_at")
        sha256_hex(self.evidence_sha256, "evidence_sha256")


@dataclass(frozen=True, slots=True, repr=False)
class _PinnedClient:
    venue_id: str
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


def _decode_json(
    payload: bytes,
    *,
    loads=json.loads,
    decimal_cls=Decimal,
    error_cls=BetfairReadOnlyError,
) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise error_cls("Betfair JSON contains duplicate object key")
            result[key] = value
        return result

    def constant(_value: str) -> object:
        raise error_cls("Betfair JSON contains non-standard numeric constant")

    try:
        return loads(
            payload.decode("utf-8"),
            parse_float=decimal_cls,
            object_pairs_hook=pairs,
            parse_constant=constant,
        )
    except error_cls:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise error_cls("Betfair response is not valid UTF-8 JSON") from None


def _provider_text(
    value: Mapping[str, object],
    key: str,
    field: str,
    *,
    required_text=_required_text,
    error_cls=BetfairReadOnlyError,
) -> str:
    if key not in value:
        raise error_cls(f"{field} is missing from provider response")
    return required_text(value[key], field)


def _provider_positive_int(
    value: Mapping[str, object],
    key: str,
    field: str,
    *,
    positive_int=_positive_int,
    error_cls=BetfairReadOnlyError,
) -> int:
    if key not in value:
        raise error_cls(f"{field} is missing from provider response")
    return positive_int(value[key], field)


def _provider_bool(
    value: Mapping[str, object],
    key: str,
    field: str,
    *,
    error_cls=BetfairReadOnlyError,
) -> bool:
    if key not in value or type(value[key]) is not bool:
        raise error_cls(f"{field} must be provider bool")
    return value[key]


def _provider_decimal(
    value: Mapping[str, object],
    key: str,
    field: str,
    *,
    decimal_cls=Decimal,
    decimal_value=_decimal,
    error_cls=BetfairReadOnlyError,
) -> Decimal:
    if key not in value:
        raise error_cls(f"{field} is missing from provider response")
    raw = value[key]
    if type(raw) is decimal_cls:
        result = raw
    elif isinstance(raw, int) and not isinstance(raw, bool):
        result = decimal_cls(raw)
    else:
        raise error_cls(
            f"{field} must be a JSON number decoded without binary float"
        )
    return decimal_value(result, field)


def _parse_statement_item(
    value: object,
    index: int,
    *,
    mapping=_mapping,
    provider_text=_provider_text,
    provider_decimal=_provider_decimal,
    item_cls=BetfairAccountStatementItemObservation,
    mapping_cls=Mapping,
    dumps=json.dumps,
    hash_ctor=sha256,
    error_cls=BetfairReadOnlyError,
) -> BetfairAccountStatementItemObservation:
    row = mapping(value, f"accountStatement[{index}]")
    item_class_data = row.get("itemClassData")
    if item_class_data is None:
        item_class_data = {}
    if not isinstance(item_class_data, mapping_cls) or any(
        type(key) is not str for key in item_class_data
    ):
        raise error_cls("statement itemClassData must be a JSON object")
    detail_digest = hash_ctor(
        dumps(
            item_class_data,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return item_cls(
        ref_id=provider_text(row, "refId", "ref_id"),
        item_date=provider_text(row, "itemDate", "item_date"),
        amount=provider_decimal(row, "amount", "amount"),
        balance=provider_decimal(row, "balance", "balance"),
        item_class=provider_text(row, "itemClass", "item_class"),
        item_class_data_sha256=detail_digest,
    )


def _resolve_exact_entitlement(
    pinned: _PinnedClient,
    rpc: _RpcRead,
    *,
    mapping=_mapping,
    provider_text=_provider_text,
    provider_positive_int=_provider_positive_int,
    provider_bool=_provider_bool,
    required_text=_required_text,
    observation_cls=BetfairDeveloperAppEntitlementObservation,
    hash_ctor=sha256,
    error_cls=BetfairReadOnlyError,
) -> BetfairDeveloperAppEntitlementObservation:
    raw_apps = rpc.result
    if type(raw_apps) is not list:
        raise error_cls("getDeveloperAppKeys result must be a JSON array")
    matches: list[tuple[Mapping[str, object], Mapping[str, object]]] = []
    for app_index, raw_app in enumerate(raw_apps):
        app = mapping(raw_app, f"developerApps[{app_index}]")
        versions = app.get("appVersions")
        if type(versions) is not list:
            raise error_cls("developer app versions must be a JSON array")
        for version_index, raw_version in enumerate(versions):
            version = mapping(
                raw_version,
                f"developerApps[{app_index}].appVersions[{version_index}]",
            )
            application_key = provider_text(
                version, "applicationKey", "application_key"
            )
            if application_key == pinned.application_key:
                matches.append((app, version))
    if len(matches) != 1:
        raise error_cls(
            "exact authenticated application key entitlement is ambiguous or missing"
        )
    app, version = matches[0]
    vendor_id = version.get("vendorId")
    if vendor_id is not None:
        vendor_id = required_text(vendor_id, "vendor_id")
    return observation_cls(
        venue_id=pinned.venue_id,
        application_key_sha256=hash_ctor(
            pinned.application_key.encode("utf-8")
        ).hexdigest(),
        app_id=provider_positive_int(app, "appId", "app_id"),
        app_name=provider_text(app, "appName", "app_name"),
        version_id=provider_positive_int(version, "versionId", "version_id"),
        version=provider_text(version, "version", "version"),
        delay_data=provider_bool(version, "delayData", "delay_data"),
        subscription_required=provider_bool(
            version, "subscriptionRequired", "subscription_required"
        ),
        owner_managed=provider_bool(version, "ownerManaged", "owner_managed"),
        active=provider_bool(version, "active", "active"),
        vendor_id=vendor_id,
        evidence=rpc.evidence,
    )


def _combined_evidence_sha256(
    entitlement: BetfairDeveloperAppEntitlementObservation,
    statement: BetfairAccountStatementPageObservation,
    *,
    dumps=json.dumps,
    hash_ctor=sha256,
) -> str:
    payload = {
        "schema": "autosport.betfair_provider_billing_inputs",
        "schema_version": 2,
        "venue_id": entitlement.venue_id,
        "application_key_sha256": entitlement.application_key_sha256,
        "entitlement_evidence": {
            "observed_at": entitlement.evidence.observed_at,
            "source_payload_sha256": entitlement.evidence.source_payload_sha256,
        },
        "account_details_evidence": {
            "observed_at": statement.account_details_evidence.observed_at,
            "source_payload_sha256": (
                statement.account_details_evidence.source_payload_sha256
            ),
        },
        "statement_evidence": {
            "observed_at": statement.evidence.observed_at,
            "source_payload_sha256": statement.evidence.source_payload_sha256,
        },
        "currency_code": statement.currency_code,
        "statement_scope": {
            "from_record": statement.from_record,
            "record_count": statement.record_count,
            "statement_from": statement.statement_from,
            "statement_to": statement.statement_to,
            "request_scope_sha256": statement.request_scope_sha256,
            "more_available": statement.more_available,
        },
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
    return hash_ctor(
        dumps(
            payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _snapshot_client(
    client: object,
    *,
    client_cls=BetfairReadOnlyClient,
    credentials_cls=BetfairSessionCredentials,
    required_text=_required_text,
    method_type=MethodType,
    namespace_cls=SimpleNamespace,
    next_request_id=BetfairReadOnlyClient.__dict__["_next_request_id"],
    observed_at=BetfairReadOnlyClient.__dict__["_observed_at"],
    authority_methods=_CLIENT_AUTHORITY_METHODS,
    error_cls=BetfairReadOnlyError,
) -> _PinnedClient:
    """Snapshot one exact read capability without exporting caller account labels."""

    if type(client) is not client_cls:
        raise TypeError("client must be exact BetfairReadOnlyClient")
    state = vars(client).copy()
    shadowed = sorted(name for name in authority_methods if name in state)
    if shadowed:
        raise error_cls(
            "BetfairReadOnlyClient read authority is instance-shadowed: "
            + ", ".join(shadowed)
        )
    credentials = state.get("_credentials")
    if type(credentials) is not credentials_cls:
        raise error_cls("client credentials are not exact canonical credentials")
    venue_id = required_text(state.get("_venue_id"), "venue_id")
    transport = state.get("_transport")
    post = getattr(transport, "post", None)
    if not callable(post):
        raise error_cls("client transport post capability is unavailable")
    timeout = state.get("_timeout_seconds")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout <= 0
    ):
        raise error_cls("client timeout is invalid")
    clock = state.get("_clock")
    if not callable(clock):
        raise error_cls("client clock is invalid")

    pinned_client = client_cls(
        credentials_cls(credentials.application_key, credentials.session_token),
        transport=namespace_cls(post=post),
        timeout_seconds=float(timeout),
        clock=clock,
        venue_id=venue_id,
        account_id="non-authoritative-local-binding-not-exported",
    )
    pinned_client._next_request_id = method_type(  # type: ignore[method-assign]
        next_request_id, pinned_client
    )
    pinned_client._observed_at = method_type(  # type: ignore[method-assign]
        observed_at, pinned_client
    )
    return _PinnedClient(
        venue_id=venue_id,
        application_key=credentials.application_key,
        session_token=credentials.session_token,
        timeout_seconds=float(timeout),
        post=post,
        next_request_id=pinned_client._next_request_id,
        observed_at=pinned_client._observed_at,
    )


def _read_rpc(
    pinned: _PinnedClient,
    method: str,
    params: Mapping[str, object],
    *,
    endpoint=ACCOUNT_JSON_RPC_ENDPOINT,
    allowed_methods=frozenset(
        {_GET_ACCOUNT_DETAILS, _GET_DEVELOPER_APP_KEYS, _GET_ACCOUNT_STATEMENT}
    ),
    evidence_cls=BetfairEvidence,
    decode_json=_decode_json,
    mapping=_mapping,
    instant=_instant,
    dumps=json.dumps,
    hash_ctor=sha256,
    error_cls=BetfairReadOnlyError,
) -> _RpcRead:
    if method not in allowed_methods:
        raise error_cls(
            "provider billing RPC is outside the strict read-only allowlist"
        )
    request_id = pinned.next_request_id()
    if isinstance(request_id, bool) or not isinstance(request_id, int):
        raise error_cls("canonical request id is invalid")
    body = dumps(
        {
            "jsonrpc": "2.0",
            "method": method,
            "params": dict(params),
            "id": request_id,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    payload = pinned.post(
        endpoint,
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
        raise error_cls("Betfair transport must return bytes")
    observed_at_value = pinned.observed_at()
    instant(observed_at_value, "observed_at")
    evidence = evidence_cls(observed_at_value, hash_ctor(payload).hexdigest())
    envelope = mapping(decode_json(payload), "JSON-RPC response")
    if envelope.get("jsonrpc") != "2.0":
        raise error_cls("Betfair response has invalid jsonrpc version")
    response_id = envelope.get("id")
    if (
        isinstance(response_id, bool)
        or not isinstance(response_id, int)
        or response_id != request_id
    ):
        raise error_cls("Betfair response id does not match request id")
    if "error" in envelope and envelope["error"] is not None:
        raise error_cls("Betfair provider returned an RPC error")
    if "result" not in envelope:
        raise error_cls("Betfair response is missing result")
    return _RpcRead(envelope["result"], evidence)


def _read_betfair_provider_billing_inputs(
    snapshot_client,
    read_rpc,
    mapping,
    provider_text,
    resolve_entitlement,
    statement_params,
    statement_scope_sha256,
    parse_statement_item,
    combined_evidence_sha256,
    statement_cls,
    observation_cls,
    optional_instant,
    instant,
    nonnegative_int,
    statement_record_count,
    error_cls,
    get_account_details_method,
    get_developer_keys_method,
    get_account_statement_method,
    client: BetfairReadOnlyClient,
    *,
    from_record: int = 0,
    record_count: int = 100,
    statement_from: str | None = None,
    statement_to: str | None = None,
) -> BetfairProviderBillingInputsObservation:
    """Capture current-key entitlement and one exact statement page.

    The public callable binds every evidence-producing helper/class used below into
    a non-rebindable ``functools.partial`` before provider I/O. An injected transport
    callback can mutate module mirrors mid-capture without changing this executable
    graph. Missing rows remain absence of evidence, never zero. A page with
    ``more_available=True`` is explicitly partial evidence.
    """

    nonnegative_int(from_record, "from_record")
    statement_record_count(record_count)
    optional_instant(statement_from, "statement_from")
    optional_instant(statement_to, "statement_to")
    if statement_from is not None and statement_to is not None:
        if instant(statement_from, "statement_from") > instant(
            statement_to, "statement_to"
        ):
            raise error_cls("statement_from must not be after statement_to")

    pinned = snapshot_client(client)
    details = read_rpc(pinned, get_account_details_method, {})
    details_result = mapping(details.result, "getAccountDetails result")
    currency_code = provider_text(
        details_result, "currencyCode", "currency_code"
    )
    if not currency_code.isascii() or currency_code != currency_code.upper():
        raise error_cls("currency_code must be uppercase ASCII")

    developer = read_rpc(pinned, get_developer_keys_method, {})
    entitlement = resolve_entitlement(pinned, developer)

    params = statement_params(
        from_record=from_record,
        record_count=record_count,
        statement_from=statement_from,
        statement_to=statement_to,
    )
    request_scope_sha256 = statement_scope_sha256(
        from_record=from_record,
        record_count=record_count,
        statement_from=statement_from,
        statement_to=statement_to,
    )
    statement_rpc = read_rpc(pinned, get_account_statement_method, params)
    statement_result = mapping(
        statement_rpc.result, "getAccountStatement result"
    )
    raw_items = statement_result.get("accountStatement")
    if type(raw_items) is not list:
        raise error_cls("accountStatement must be a JSON array")
    if len(raw_items) > record_count:
        raise error_cls("statement response exceeds requested record_count")
    items = tuple(
        parse_statement_item(item, index)
        for index, item in enumerate(raw_items)
    )
    more_available = statement_result.get("moreAvailable")
    if type(more_available) is not bool:
        raise error_cls("statement moreAvailable must be bool")
    statement = statement_cls(
        venue_id=pinned.venue_id,
        currency_code=currency_code,
        from_record=from_record,
        record_count=record_count,
        statement_from=statement_from,
        statement_to=statement_to,
        request_scope_sha256=request_scope_sha256,
        items=items,
        more_available=more_available,
        account_details_evidence=details.evidence,
        evidence=statement_rpc.evidence,
    )
    observed_at = max(
        entitlement.evidence.observed_at,
        statement.evidence.observed_at,
        statement.account_details_evidence.observed_at,
        key=lambda value: instant(value, "observed_at"),
    )
    evidence_sha256 = combined_evidence_sha256(entitlement, statement)
    result = observation_cls(
        entitlement=entitlement,
        statement=statement,
        observed_at=observed_at,
        evidence_sha256=evidence_sha256,
    )
    if result.evidence_sha256 != combined_evidence_sha256(
        result.entitlement, result.statement
    ):
        raise error_cls("provider billing combined evidence digest mismatch")
    return result


# Freeze the full evidence-producing executable graph before any caller can invoke
# the capability. partial.func/args are read-only, so pre-call or mid-call rebinding
# of module mirrors cannot substitute parser/validator/digest/class semantics.
read_betfair_provider_billing_inputs = partial(
    _read_betfair_provider_billing_inputs,
    _snapshot_client,
    _read_rpc,
    _mapping,
    _provider_text,
    _resolve_exact_entitlement,
    _statement_params,
    _statement_request_scope_sha256,
    _parse_statement_item,
    _combined_evidence_sha256,
    BetfairAccountStatementPageObservation,
    BetfairProviderBillingInputsObservation,
    _optional_instant,
    _instant,
    _nonnegative_int,
    _statement_record_count,
    BetfairReadOnlyError,
    _GET_ACCOUNT_DETAILS,
    _GET_DEVELOPER_APP_KEYS,
    _GET_ACCOUNT_STATEMENT,
)
read_betfair_provider_billing_inputs.__name__ = (
    "read_betfair_provider_billing_inputs"
)
read_betfair_provider_billing_inputs.__qualname__ = (
    "read_betfair_provider_billing_inputs"
)
read_betfair_provider_billing_inputs.__doc__ = (
    _read_betfair_provider_billing_inputs.__doc__
)

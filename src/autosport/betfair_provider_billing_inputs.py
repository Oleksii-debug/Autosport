"""Authenticated Betfair provider/fixed-billing source observations.

This evidence seam deliberately stops before economic attribution or allocation.
It observes the exact authenticated application-key entitlement and one bounded
account-statement page, but it never turns public pricing, a missing row, an
amount/date coincidence, or a caller label into cost truth.

The Betfair account reads consumed here do not expose a provider-owned literal
account identifier. The caller-configured ``BetfairReadOnlyClient.account_id`` is
therefore not emitted. The provider-returned owner on the uniquely matched
application-key version is retained as an authenticated account-origin observation.
Raw application keys are compared only transiently and no key or key fingerprint is
retained; durable entitlement identity is a digest of a canonical non-secret
provider projection.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
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
_CLIENT_AUTHORITY_METHODS = frozenset(
    {"_rpc", "_next_request_id", "_observed_at", "_redact_provider_message"}
)


def _build_capability():
    """Build one closure-sealed provider evidence capability.

    All authority-bearing helpers, DTO classes, canonical client methods, constants,
    and stdlib primitives are captured as closure cells before callers can perform
    provider I/O. Module-level helper names exposed below are mirrors only; rebinding
    them before or during a provider callback cannot change this capability.
    """

    error_cls = BetfairReadOnlyError
    client_cls = BetfairReadOnlyClient
    credentials_cls = BetfairSessionCredentials
    evidence_cls = BetfairEvidence
    endpoint = ACCOUNT_JSON_RPC_ENDPOINT
    get_details_method = _GET_ACCOUNT_DETAILS
    get_keys_method = _GET_DEVELOPER_APP_KEYS
    get_statement_method = _GET_ACCOUNT_STATEMENT
    allowed_methods = frozenset(
        {get_details_method, get_keys_method, get_statement_method}
    )
    authority_methods = _CLIENT_AUTHORITY_METHODS
    next_request_id_impl = client_cls.__dict__["_next_request_id"]
    observed_at_impl = client_cls.__dict__["_observed_at"]
    dumps = json.dumps
    loads = json.loads
    json_decode_error = json.JSONDecodeError
    hash_ctor = sha256
    decimal_cls = Decimal
    datetime_cls = datetime
    utc = timezone.utc
    method_type = MethodType
    namespace_cls = SimpleNamespace
    mapping_cls = Mapping
    sha_pattern = re.compile(r"^[0-9a-f]{64}$")
    transaction_charge_marker = "BETFAIR_TRANSACTION_CHARGE"
    transaction_charge_description_prefix = (
        "Bet Txn Charge for over 5000 per hour on "
    )

    def required_text(value: object, field: str) -> str:
        if (
            type(value) is not str
            or not value
            or value != value.strip()
            or "\x00" in value
        ):
            raise error_cls(f"{field} must be a non-empty canonical string")
        return value

    def sha256_hex(value: object, field: str) -> str:
        text = required_text(value, field)
        if sha_pattern.fullmatch(text) is None:
            raise error_cls(f"{field} must be lowercase SHA-256")
        return text

    def positive_int(value: object, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise error_cls(f"{field} must be a positive integer")
        return value

    def nonnegative_int(value: object, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise error_cls(f"{field} must be a non-negative integer")
        return value

    def statement_record_count(value: object) -> int:
        count = positive_int(value, "record_count")
        if count > 100:
            raise error_cls("record_count exceeds provider statement page limit")
        return count

    def decimal_value(value: object, field: str) -> Decimal:
        if type(value) is not decimal_cls or not value.is_finite():
            raise error_cls(f"{field} must be a finite Decimal")
        return value

    def instant(value: object, field: str) -> datetime:
        if type(value) is not str:
            raise error_cls(f"{field} must be an ISO-8601 string")
        try:
            parsed = datetime_cls.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise error_cls(f"{field} must be valid ISO-8601") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise error_cls(f"{field} must be timezone-aware")
        return parsed.astimezone(utc)

    def optional_instant(value: object, field: str) -> datetime | None:
        return None if value is None else instant(value, field)

    def mapping(value: object, field: str) -> Mapping[str, object]:
        if not isinstance(value, mapping_cls) or any(
            type(key) is not str for key in value
        ):
            raise error_cls(f"{field} must be a JSON object")
        return value

    def provider_text(
        value: Mapping[str, object], key: str, field: str
    ) -> str:
        if key not in value:
            raise error_cls(f"{field} is missing from provider response")
        return required_text(value[key], field)

    def provider_positive_int(
        value: Mapping[str, object], key: str, field: str
    ) -> int:
        if key not in value:
            raise error_cls(f"{field} is missing from provider response")
        return positive_int(value[key], field)

    def provider_bool(
        value: Mapping[str, object], key: str, field: str
    ) -> bool:
        if key not in value or type(value[key]) is not bool:
            raise error_cls(f"{field} must be provider bool")
        return value[key]

    def provider_decimal(
        value: Mapping[str, object], key: str, field: str
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

    def canonical_sha256(value: object) -> str:
        try:
            raw = dumps(
                value,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise error_cls("provider evidence is not canonical JSON") from exc
        return hash_ctor(raw).hexdigest()

    def statement_params(
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

    def statement_scope_sha256(
        *,
        from_record: int,
        record_count: int,
        statement_from: str | None,
        statement_to: str | None,
    ) -> str:
        return canonical_sha256(
            {
                "method": get_statement_method,
                "params": statement_params(
                    from_record=from_record,
                    record_count=record_count,
                    statement_from=statement_from,
                    statement_to=statement_to,
                ),
            }
        )

    @dataclass(frozen=True, slots=True)
    class DeveloperAppEntitlementObservation:
        venue_id: str
        app_id: int
        app_name: str
        version_id: int
        version: str
        delay_data: bool
        subscription_required: bool
        owner_managed: bool
        active: bool
        vendor_id: str | None
        provider_owner: str
        observed_at: str
        source_projection_sha256: str

        def __post_init__(self) -> None:
            required_text(self.venue_id, "venue_id")
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
            required_text(self.provider_owner, "provider_owner")
            instant(self.observed_at, "observed_at")
            sha256_hex(
                self.source_projection_sha256,
                "source_projection_sha256",
            )

    @dataclass(frozen=True, slots=True)
    class AccountStatementItemObservation:
        ref_id: str
        item_date: str
        amount: Decimal
        balance: Decimal
        item_class: str
        item_class_data_sha256: str
        provider_charge_class: str | None = None
        provider_transaction_id: int | None = None

        def __post_init__(self) -> None:
            required_text(self.ref_id, "ref_id")
            instant(self.item_date, "item_date")
            decimal_value(self.amount, "amount")
            decimal_value(self.balance, "balance")
            required_text(self.item_class, "item_class")
            sha256_hex(self.item_class_data_sha256, "item_class_data_sha256")
            if self.provider_charge_class is None:
                if self.provider_transaction_id is not None:
                    raise error_cls(
                        "provider_transaction_id requires provider_charge_class"
                    )
            else:
                if self.provider_charge_class != transaction_charge_marker:
                    raise error_cls("unsupported provider charge class")
                positive_int(
                    self.provider_transaction_id,
                    "provider_transaction_id",
                )

    @dataclass(frozen=True, slots=True)
    class AccountStatementPageObservation:
        venue_id: str
        currency_code: str
        from_record: int
        record_count: int
        statement_from: str | None
        statement_to: str | None
        request_scope_sha256: str
        items: tuple[AccountStatementItemObservation, ...]
        more_available: bool
        account_details_evidence: BetfairEvidence
        statement_evidence: BetfairEvidence

        def __post_init__(self) -> None:
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
            if self.request_scope_sha256 != statement_scope_sha256(
                from_record=self.from_record,
                record_count=self.record_count,
                statement_from=self.statement_from,
                statement_to=self.statement_to,
            ):
                raise error_cls("statement request scope digest mismatch")
            if type(self.items) is not tuple or any(
                type(item) is not AccountStatementItemObservation
                for item in self.items
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
            if type(self.statement_evidence) is not evidence_cls:
                raise error_cls(
                    "statement evidence must be exact BetfairEvidence"
                )

    def aggregate_observed_at(
        entitlement: DeveloperAppEntitlementObservation,
        statement: AccountStatementPageObservation,
    ) -> str:
        observations = (
            entitlement.observed_at,
            statement.account_details_evidence.observed_at,
            statement.statement_evidence.observed_at,
        )
        return max(
            observations,
            key=lambda value: instant(value, "component observed_at"),
        )

    def combined_evidence_sha256(
        entitlement: DeveloperAppEntitlementObservation,
        statement: AccountStatementPageObservation,
    ) -> str:
        observed_at = aggregate_observed_at(entitlement, statement)
        return canonical_sha256(
            {
                "schema": "autosport.betfair_provider_billing_inputs",
                "schema_version": 6,
                "venue_id": entitlement.venue_id,
                "observed_at": observed_at,
                "entitlement": {
                    "app_id": entitlement.app_id,
                    "app_name": entitlement.app_name,
                    "version_id": entitlement.version_id,
                    "version": entitlement.version,
                    "delay_data": entitlement.delay_data,
                    "subscription_required": entitlement.subscription_required,
                    "owner_managed": entitlement.owner_managed,
                    "active": entitlement.active,
                    "vendor_id": entitlement.vendor_id,
                    "provider_owner": entitlement.provider_owner,
                    "observed_at": entitlement.observed_at,
                    "source_projection_sha256": (
                        entitlement.source_projection_sha256
                    ),
                },
                "account_details_evidence": {
                    "observed_at": statement.account_details_evidence.observed_at,
                    "source_payload_sha256": (
                        statement.account_details_evidence.source_payload_sha256
                    ),
                },
                "statement_evidence": {
                    "observed_at": statement.statement_evidence.observed_at,
                    "source_payload_sha256": (
                        statement.statement_evidence.source_payload_sha256
                    ),
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
                        "provider_charge_class": item.provider_charge_class,
                        "provider_transaction_id": item.provider_transaction_id,
                    }
                    for item in statement.items
                ],
            }
        )

    @dataclass(frozen=True, slots=True)
    class ProviderBillingInputsObservation:
        entitlement: DeveloperAppEntitlementObservation
        statement: AccountStatementPageObservation
        observed_at: str
        evidence_sha256: str

        def __post_init__(self) -> None:
            if type(self.entitlement) is not DeveloperAppEntitlementObservation:
                raise error_cls("entitlement must be exact canonical observation")
            if type(self.statement) is not AccountStatementPageObservation:
                raise error_cls("statement must be exact canonical observation")
            if self.entitlement.venue_id != self.statement.venue_id:
                raise error_cls(
                    "provider billing observations disagree on venue identity"
                )
            instant(self.observed_at, "observed_at")
            expected_observed_at = aggregate_observed_at(
                self.entitlement, self.statement
            )
            if self.observed_at != expected_observed_at:
                raise error_cls(
                    "provider billing observed_at must equal latest component observation"
                )
            sha256_hex(self.evidence_sha256, "evidence_sha256")
            if self.evidence_sha256 != combined_evidence_sha256(
                self.entitlement, self.statement
            ):
                raise error_cls(
                    "provider billing combined evidence digest mismatch"
                )

    @dataclass(frozen=True, slots=True, repr=False)
    class PinnedClient:
        venue_id: str
        application_key: str
        session_token: str
        timeout_seconds: float
        post: Callable[..., bytes]
        next_request_id: Callable[[], int]
        observed_at: Callable[[], str]

    @dataclass(frozen=True, slots=True)
    class RpcRead:
        result: object
        evidence: BetfairEvidence

    def decode_json(payload: bytes) -> object:
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
        except (UnicodeDecodeError, json_decode_error):
            raise error_cls("Betfair response is not valid UTF-8 JSON") from None

    def snapshot_client(client: object) -> PinnedClient:
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
            next_request_id_impl, pinned_client
        )
        pinned_client._observed_at = method_type(  # type: ignore[method-assign]
            observed_at_impl, pinned_client
        )
        return PinnedClient(
            venue_id=venue_id,
            application_key=credentials.application_key,
            session_token=credentials.session_token,
            timeout_seconds=float(timeout),
            post=post,
            next_request_id=pinned_client._next_request_id,
            observed_at=pinned_client._observed_at,
        )

    def read_rpc(
        pinned: PinnedClient,
        method: str,
        params: Mapping[str, object],
    ) -> RpcRead:
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
        observed_at = pinned.observed_at()
        instant(observed_at, "observed_at")
        evidence = evidence_cls(observed_at, hash_ctor(payload).hexdigest())
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
        return RpcRead(envelope["result"], evidence)

    def resolve_entitlement(
        pinned: PinnedClient,
        rpc: RpcRead,
    ) -> DeveloperAppEntitlementObservation:
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
        provider_owner = provider_text(version, "owner", "provider_owner")
        app_id = provider_positive_int(app, "appId", "app_id")
        app_name = provider_text(app, "appName", "app_name")
        version_id = provider_positive_int(version, "versionId", "version_id")
        version_name = provider_text(version, "version", "version")
        delay_data = provider_bool(version, "delayData", "delay_data")
        subscription_required = provider_bool(
            version, "subscriptionRequired", "subscription_required"
        )
        owner_managed = provider_bool(
            version, "ownerManaged", "owner_managed"
        )
        active = provider_bool(version, "active", "active")
        projection = {
            "app_id": app_id,
            "app_name": app_name,
            "version_id": version_id,
            "version": version_name,
            "delay_data": delay_data,
            "subscription_required": subscription_required,
            "owner_managed": owner_managed,
            "active": active,
            "vendor_id": vendor_id,
            "provider_owner": provider_owner,
        }
        return DeveloperAppEntitlementObservation(
            venue_id=pinned.venue_id,
            app_id=app_id,
            app_name=app_name,
            version_id=version_id,
            version=version_name,
            delay_data=delay_data,
            subscription_required=subscription_required,
            owner_managed=owner_managed,
            active=active,
            vendor_id=vendor_id,
            provider_owner=provider_owner,
            observed_at=rpc.evidence.observed_at,
            source_projection_sha256=canonical_sha256(projection),
        )

    def transaction_charge_signal(
        *,
        item_class: str,
        amount: Decimal,
        item_class_data: Mapping[str, object],
    ) -> tuple[str | None, int | None]:
        """Derive only the documented Betfair account-level transaction-charge marker.

        The provider's human-readable description is used as a positive discriminator
        but is never retained on the durable DTO. Unknown locales, malformed nested
        data, and near-miss UNKNOWN debits remain unclassified; absence of this signal
        is therefore never evidence that no provider charge exists.
        """

        if item_class != "UNKNOWN" or amount >= 0:
            return None, None
        raw_unknown = item_class_data.get("unknownStatementItem")
        if type(raw_unknown) is not str:
            return None, None
        try:
            nested = mapping(
                decode_json(raw_unknown.encode("utf-8")),
                "statement unknownStatementItem",
            )
        except (error_cls, UnicodeEncodeError):
            return None, None

        for field in ("eventId", "eventTypeId", "selectionId"):
            value = nested.get(field)
            if isinstance(value, bool) or type(value) is not int or value != 0:
                return None, None

        if (
            nested.get("marketName") != "DEBIT"
            or nested.get("marketType") != "NOT_APPLICABLE"
            or nested.get("transactionType") != "ACCOUNT_DEBIT"
            or nested.get("winLose") != "RESULT_NOT_APPLICABLE"
        ):
            return None, None

        description = nested.get("fullMarketName")
        if (
            type(description) is not str
            or not description.startswith(transaction_charge_description_prefix)
        ):
            return None, None

        transaction_id = nested.get("transactionId")
        if (
            isinstance(transaction_id, bool)
            or type(transaction_id) is not int
            or transaction_id <= 0
        ):
            return None, None
        return transaction_charge_marker, transaction_id

    def parse_statement_item(
        value: object, index: int
    ) -> AccountStatementItemObservation:
        row = mapping(value, f"accountStatement[{index}]")
        item_class_data = row.get("itemClassData")
        if item_class_data is None:
            item_class_data = {}
        if not isinstance(item_class_data, mapping_cls) or any(
            type(key) is not str for key in item_class_data
        ):
            raise error_cls("statement itemClassData must be a JSON object")

        amount = provider_decimal(row, "amount", "amount")
        item_class = provider_text(row, "itemClass", "item_class")
        provider_charge_class, provider_transaction_id = transaction_charge_signal(
            item_class=item_class,
            amount=amount,
            item_class_data=item_class_data,
        )
        return AccountStatementItemObservation(
            ref_id=provider_text(row, "refId", "ref_id"),
            item_date=provider_text(row, "itemDate", "item_date"),
            amount=amount,
            balance=provider_decimal(row, "balance", "balance"),
            item_class=item_class,
            item_class_data_sha256=canonical_sha256(item_class_data),
            provider_charge_class=provider_charge_class,
            provider_transaction_id=provider_transaction_id,
        )

    def read(
        client: BetfairReadOnlyClient,
        *,
        from_record: int = 0,
        record_count: int = 100,
        statement_from: str | None = None,
        statement_to: str | None = None,
    ) -> ProviderBillingInputsObservation:
        """Capture current entitlement and one exact statement page.

        Missing rows remain absence of evidence, never zero. ``more_available=True``
        means the page is explicitly partial. No durable field contains the raw
        application key, a key fingerprint, or the raw developer-app response hash.
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
        details = read_rpc(pinned, get_details_method, {})
        details_result = mapping(details.result, "getAccountDetails result")
        currency_code = provider_text(
            details_result, "currencyCode", "currency_code"
        )
        if not currency_code.isascii() or currency_code != currency_code.upper():
            raise error_cls("currency_code must be uppercase ASCII")

        developer = read_rpc(pinned, get_keys_method, {})
        entitlement = resolve_entitlement(pinned, developer)

        params = statement_params(
            from_record=from_record,
            record_count=record_count,
            statement_from=statement_from,
            statement_to=statement_to,
        )
        statement_rpc = read_rpc(pinned, get_statement_method, params)
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
        statement = AccountStatementPageObservation(
            venue_id=pinned.venue_id,
            currency_code=currency_code,
            from_record=from_record,
            record_count=record_count,
            statement_from=statement_from,
            statement_to=statement_to,
            request_scope_sha256=statement_scope_sha256(
                from_record=from_record,
                record_count=record_count,
                statement_from=statement_from,
                statement_to=statement_to,
            ),
            items=items,
            more_available=more_available,
            account_details_evidence=details.evidence,
            statement_evidence=statement_rpc.evidence,
        )
        observed_at = aggregate_observed_at(entitlement, statement)
        return ProviderBillingInputsObservation(
            entitlement=entitlement,
            statement=statement,
            observed_at=observed_at,
            evidence_sha256=combined_evidence_sha256(entitlement, statement),
        )

    # Return authoritative closure objects plus non-authoritative helper mirrors used
    # only by deterministic rebinding falsifiers and independent inspection.
    return (
        read,
        DeveloperAppEntitlementObservation,
        AccountStatementItemObservation,
        AccountStatementPageObservation,
        ProviderBillingInputsObservation,
        provider_text,
        read_rpc,
        resolve_entitlement,
        decode_json,
        parse_statement_item,
    )


(
    read_betfair_provider_billing_inputs,
    BetfairDeveloperAppEntitlementObservation,
    BetfairAccountStatementItemObservation,
    BetfairAccountStatementPageObservation,
    BetfairProviderBillingInputsObservation,
    _provider_text,
    _read_rpc,
    _resolve_exact_entitlement,
    _decode_json,
    _parse_statement_item,
) = _build_capability()

read_betfair_provider_billing_inputs.__name__ = (
    "read_betfair_provider_billing_inputs"
)
read_betfair_provider_billing_inputs.__qualname__ = (
    "read_betfair_provider_billing_inputs"
)
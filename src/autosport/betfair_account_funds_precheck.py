"""K07-bound, fail-closed Betfair account-funds precheck.

The precheck binds one authenticated Betfair session-context identity to one funds read
performed by the same canonical client. It does not claim stable cross-session account
identity, provider acceptance, execution permission, or real-money authority.

The required liability amount/currency are still caller inputs at this boundary. Until
a separate product-issued amount+currency liability authority is composed, the numeric
comparison is diagnostic only: passed remains false even when numeric_sufficient is
true.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import json
from threading import RLock
from weakref import ReferenceType, ref

from . import betfair_account_readonly as _readonly_module
from .betfair_account_identity import (
    BetfairAccountIdentityError,
    BetfairAuthenticatedAccountIdentity,
    build_betfair_authenticated_client,
    require_authoritative_betfair_account_identity,
    resolve_betfair_authenticated_account_identity,
)
from .betfair_account_readonly import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    BetfairAccountFundsObservation,
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)

VENUE_ID = "betfair"
SOURCE_FAMILY = "betfair.account-funds-precheck.k07.v2"
MAX_EVIDENCE_AGE = timedelta(seconds=30)
MAX_CAPTURE_SKEW = timedelta(seconds=30)
_MAX_FUTURE_SKEW = timedelta(seconds=1)
_CONTEXT_PREFIX = "betfair-session-context:"


class BetfairAccountFundsPrecheckError(RuntimeError):
    """Account/funds evidence cannot support this fail-closed precheck."""


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetfairAccountFundsPrecheck:
    venue_id: str
    account_id: str
    adapter_id: str
    adapter_version: str
    required_liability: Decimal
    available_to_bet_balance: Decimal
    currency_code: str
    account_observed_at: datetime
    funds_observed_at: datetime
    evaluated_at: datetime
    account_details_sha256: str
    account_funds_sha256: str

    def __post_init__(self) -> None:
        if self.venue_id != VENUE_ID:
            raise BetfairAccountFundsPrecheckError("venue_id is product-owned")
        _context_id(self.account_id)
        if self.adapter_id != ADAPTER_ID or self.adapter_version != ADAPTER_VERSION:
            raise BetfairAccountFundsPrecheckError("Betfair adapter identity mismatch")
        _money(self.required_liability, "required_liability")
        _money(self.available_to_bet_balance, "available_to_bet_balance")
        _currency(self.currency_code, "currency_code")
        account_time = _utc(self.account_observed_at, "account_observed_at")
        funds_time = _utc(self.funds_observed_at, "funds_observed_at")
        evaluated = _utc(self.evaluated_at, "evaluated_at")
        _temporal(account_time, funds_time, evaluated)
        _sha(self.account_details_sha256, "account_details_sha256")
        _sha(self.account_funds_sha256, "account_funds_sha256")

    @property
    def stable_account_identity_proven(self) -> bool:
        return False

    @property
    def numeric_sufficient(self) -> bool:
        return self.available_to_bet_balance >= self.required_liability

    @property
    def liability_unit_proven(self) -> bool:
        return False

    @property
    def passed(self) -> bool:
        return self.numeric_sufficient and self.liability_unit_proven

    @property
    def execution_authorized(self) -> bool:
        return False

    @property
    def precheck_id(self) -> str:
        return _digest(self)


@dataclass(slots=True)
class _IssuedRecord:
    value_ref: ReferenceType[BetfairAccountFundsPrecheck]
    fingerprint: str
    client: BetfairReadOnlyClient
    identity: BetfairAuthenticatedAccountIdentity


def _make_authority():
    """Closure-hide the only issuance registry and bind it to K07 + canonical funds IO."""

    result_type = BetfairAccountFundsPrecheck
    client_type = BetfairReadOnlyClient
    funds_type = BetfairAccountFundsObservation
    credentials_type = BetfairSessionCredentials
    identity_type = BetfairAuthenticatedAccountIdentity
    identity_error = BetfairAccountIdentityError
    readonly_error = BetfairReadOnlyError
    precheck_error = BetfairAccountFundsPrecheckError

    build_client = build_betfair_authenticated_client
    resolve_identity = resolve_betfair_authenticated_account_identity
    require_identity = require_authoritative_betfair_account_identity
    readonly_module = _readonly_module
    canonical_read_funds = client_type.read_account_funds
    canonical_observed_at = client_type._observed_at
    canonical_mapping = readonly_module._mapping
    canonical_number = readonly_module._number
    canonical_funds_type_binding = readonly_module.BetfairAccountFundsObservation
    canonical_funds_init = funds_type.__init__
    canonical_funds_post = getattr(funds_type, "__post_init__", None)
    money = _money
    currency = _currency
    context_id = _context_id
    sha = _sha
    provider_time = _provider_time
    temporal = _temporal
    utc = _utc
    decimal_text = _decimal_text
    datetime_text = _datetime_text
    digest = _digest
    venue_id = VENUE_ID
    adapter_id = ADAPTER_ID
    adapter_version = ADAPTER_VERSION
    source_family = SOURCE_FAMILY
    context_prefix = _CONTEXT_PREFIX
    max_evidence_age = MAX_EVIDENCE_AGE
    max_capture_skew = MAX_CAPTURE_SKEW
    max_future_skew = _MAX_FUTURE_SKEW
    canonical_result_init = result_type.__init__
    canonical_result_post = result_type.__post_init__
    canonical_stable = result_type.stable_account_identity_proven
    canonical_numeric = result_type.numeric_sufficient
    canonical_unit = result_type.liability_unit_proven
    canonical_passed = result_type.passed
    canonical_execution = result_type.execution_authorized
    canonical_id = result_type.precheck_id
    issued: dict[int, _IssuedRecord] = {}
    lock = RLock()

    def class_is_current() -> bool:
        return (
            result_type.__init__ is canonical_result_init
            and result_type.__post_init__ is canonical_result_post
            and result_type.stable_account_identity_proven is canonical_stable
            and result_type.numeric_sufficient is canonical_numeric
            and result_type.liability_unit_proven is canonical_unit
            and result_type.passed is canonical_passed
            and result_type.execution_authorized is canonical_execution
            and result_type.precheck_id is canonical_id
            and client_type.read_account_funds is canonical_read_funds
            and client_type._observed_at is canonical_observed_at
            and readonly_module._mapping is canonical_mapping
            and readonly_module._number is canonical_number
            and readonly_module.BetfairAccountFundsObservation is canonical_funds_type_binding
            and funds_type.__init__ is canonical_funds_init
            and getattr(funds_type, "__post_init__", None) is canonical_funds_post
            and _money is money
            and _currency is currency
            and _context_id is context_id
            and _sha is sha
            and _provider_time is provider_time
            and _temporal is temporal
            and _utc is utc
            and _decimal_text is decimal_text
            and _datetime_text is datetime_text
            and _digest is digest
            and VENUE_ID == venue_id
            and ADAPTER_ID == adapter_id
            and ADAPTER_VERSION == adapter_version
            and SOURCE_FAMILY == source_family
            and _CONTEXT_PREFIX == context_prefix
            and MAX_EVIDENCE_AGE == max_evidence_age
            and MAX_CAPTURE_SKEW == max_capture_skew
            and _MAX_FUTURE_SKEW == max_future_skew
        )

    def remember(
        value: BetfairAccountFundsPrecheck,
        client: BetfairReadOnlyClient,
        identity: BetfairAuthenticatedAccountIdentity,
    ) -> None:
        key = id(value)

        def discard(dead_ref: ReferenceType[BetfairAccountFundsPrecheck]) -> None:
            with lock:
                record = issued.get(key)
                if record is not None and record.value_ref is dead_ref:
                    issued.pop(key, None)

        with lock:
            issued[key] = _IssuedRecord(ref(value, discard), digest(value), client, identity)

    def evaluate(
        credentials: BetfairSessionCredentials,
        required_liability: Decimal,
        *,
        required_currency_code: str,
        timeout_seconds: float = 10.0,
    ) -> BetfairAccountFundsPrecheck:
        if type(credentials) is not credentials_type:
            raise TypeError("credentials must be BetfairSessionCredentials")
        liability = money(required_liability, "required_liability")
        required_currency = currency(required_currency_code, "required_currency_code")
        if not class_is_current():
            raise precheck_error("canonical account-funds authority was rebound")

        try:
            client = build_client(credentials, timeout_seconds=timeout_seconds)
            if type(client) is not client_type:
                raise precheck_error("K07 client factory returned non-canonical client")
            identity = resolve_identity(client)
            identity = require_identity(identity, client=client)
        except identity_error as exc:
            raise precheck_error("authenticated account-context acquisition failed") from exc

        if required_currency != identity.currency_code:
            raise precheck_error(
                "required liability currency does not match authenticated account currency"
            )
        if client_type.read_account_funds is not canonical_read_funds:
            raise precheck_error("canonical funds-read authority changed")

        try:
            funds = canonical_read_funds(client)
            identity = require_identity(identity, client=client)
        except (readonly_error, identity_error) as exc:
            raise precheck_error("Betfair account-funds acquisition failed closed") from exc
        if type(funds) is not funds_type:
            raise precheck_error("account-funds acquisition returned non-canonical evidence")
        if client_type.read_account_funds is not canonical_read_funds:
            raise precheck_error("canonical funds-read authority changed during acquisition")

        available = money(funds.available_to_bet_balance, "available_to_bet_balance")
        account_time = provider_time(identity.observed_at, "account observed_at")
        funds_time = provider_time(funds.evidence.observed_at, "funds observed_at")
        try:
            evaluated = provider_time(canonical_observed_at(client), "evaluated_at")
        except readonly_error as exc:
            raise precheck_error("canonical account clock failed") from exc
        temporal(account_time, funds_time, evaluated)

        value = result_type(
            venue_id=venue_id,
            account_id=context_id(identity.session_context_id),
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            required_liability=liability,
            available_to_bet_balance=available,
            currency_code=required_currency,
            account_observed_at=account_time,
            funds_observed_at=funds_time,
            evaluated_at=evaluated,
            account_details_sha256=sha(identity.account_details_sha256, "account_details_sha256"),
            account_funds_sha256=sha(funds.evidence.source_payload_sha256, "account_funds_sha256"),
        )
        if not class_is_current():
            raise precheck_error("account-funds authority changed during issuance")
        if (
            value.stable_account_identity_proven
            or value.passed
            or value.liability_unit_proven
            or value.execution_authorized
        ):
            raise precheck_error("fail-closed funds result was widened")
        identity = require_identity(identity, client=client)
        remember(value, client, identity)
        return value

    def is_authoritative(value: object) -> bool:
        if type(value) is not result_type or not class_is_current():
            return False
        with lock:
            record = issued.get(id(value))
            if record is None or record.value_ref() is not value:
                return False
        try:
            identity = require_identity(record.identity, client=record.client)
            if value.account_id != identity.session_context_id:
                return False
            if value.currency_code != identity.currency_code:
                return False
            if value.account_details_sha256 != identity.account_details_sha256:
                return False
            current = provider_time(canonical_observed_at(record.client), "authority checked_at")
            temporal(value.account_observed_at, value.funds_observed_at, current)
            if digest(value) != record.fingerprint:
                return False
            return (
                not value.stable_account_identity_proven
                and not value.passed
                and not value.liability_unit_proven
                and not value.execution_authorized
            )
        except (AttributeError, TypeError, ValueError, identity_error, readonly_error, precheck_error):
            return False

    def require_authoritative(value: object) -> BetfairAccountFundsPrecheck:
        if not is_authoritative(value):
            raise precheck_error(
                "account-funds precheck lacks current same-session source authority"
            )
        assert type(value) is result_type
        return value

    return evaluate, is_authoritative, require_authoritative


def _temporal(account_time: datetime, funds_time: datetime, evaluated: datetime) -> None:
    account_time = _utc(account_time, "account_observed_at")
    funds_time = _utc(funds_time, "funds_observed_at")
    evaluated = _utc(evaluated, "evaluated_at")
    if account_time > evaluated + _MAX_FUTURE_SKEW or funds_time > evaluated + _MAX_FUTURE_SKEW:
        raise BetfairAccountFundsPrecheckError("provider evidence is future-dated")
    if evaluated - account_time > MAX_EVIDENCE_AGE:
        raise BetfairAccountFundsPrecheckError("account evidence is stale")
    if evaluated - funds_time > MAX_EVIDENCE_AGE:
        raise BetfairAccountFundsPrecheckError("funds evidence is stale")
    if abs(funds_time - account_time) > MAX_CAPTURE_SKEW:
        raise BetfairAccountFundsPrecheckError("account/funds capture skew is too large")


def _provider_time(value: str, field: str) -> datetime:
    if type(value) is not str or not value or value != value.strip():
        raise BetfairAccountFundsPrecheckError(f"{field} must be timestamp text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BetfairAccountFundsPrecheckError(f"{field} must be ISO-8601") from exc
    return _utc(parsed, field)


def _utc(value: datetime, field: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise BetfairAccountFundsPrecheckError(f"{field} must be timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _money(value: Decimal, field: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value < 0:
        raise BetfairAccountFundsPrecheckError(f"{field} must be a finite non-negative Decimal")
    return value


def _currency(value: str, field: str) -> str:
    if type(value) is not str or len(value) != 3 or not value.isascii() or not value.isalpha() or value != value.upper():
        raise BetfairAccountFundsPrecheckError(f"{field} must be a three-letter uppercase currency code")
    return value


def _context_id(value: str) -> str:
    if type(value) is not str or not value.startswith(_CONTEXT_PREFIX):
        raise BetfairAccountFundsPrecheckError("account_id must be Betfair session-context identity")
    _sha(value.removeprefix(_CONTEXT_PREFIX), "account context digest")
    return value


def _sha(value: str, field: str) -> str:
    if type(value) is not str or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise BetfairAccountFundsPrecheckError(f"{field} must be lowercase SHA-256 hex")
    return value


def _decimal_text(value: Decimal) -> str:
    return format(_money(value, "precheck Decimal"), "f")


def _datetime_text(value: datetime) -> str:
    return _utc(value, "precheck timestamp").isoformat().replace("+00:00", "Z")


def _digest(value: BetfairAccountFundsPrecheck) -> str:
    payload = {
        "source_family": SOURCE_FAMILY,
        "venue_id": value.venue_id,
        "account_id": value.account_id,
        "adapter_id": value.adapter_id,
        "adapter_version": value.adapter_version,
        "required_liability": _decimal_text(value.required_liability),
        "available_to_bet_balance": _decimal_text(value.available_to_bet_balance),
        "currency_code": value.currency_code,
        "account_observed_at": _datetime_text(value.account_observed_at),
        "funds_observed_at": _datetime_text(value.funds_observed_at),
        "evaluated_at": _datetime_text(value.evaluated_at),
        "account_details_sha256": value.account_details_sha256,
        "account_funds_sha256": value.account_funds_sha256,
        "stable_account_identity_proven": value.stable_account_identity_proven,
        "numeric_sufficient": value.numeric_sufficient,
        "liability_unit_proven": value.liability_unit_proven,
        "passed": value.passed,
        "execution_authorized": value.execution_authorized,
    }
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise BetfairAccountFundsPrecheckError("precheck evidence is not canonical JSON") from exc
    return sha256(encoded).hexdigest()


(
    evaluate_betfair_account_funds,
    is_authoritative_funds_precheck,
    require_authoritative_funds_precheck,
) = _make_authority()
del _make_authority

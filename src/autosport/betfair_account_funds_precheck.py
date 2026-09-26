"""Fail-closed Betfair account-funds precheck authority.

The precheck acquires both account identity evidence and current available funds through
Autosport's canonical read-only Betfair client. Caller-authored balance DTOs are never
accepted as positive authority. Issued results are process-local: persistence, copying,
or reconstruction preserves evidence bytes but not source authority.

This module does not place/cancel/replace orders and never grants execution authority.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from hmac import compare_digest
import json
from weakref import ReferenceType, ref

from .betfair_account_readonly import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    BetfairAccountDetailsObservation,
    BetfairAccountFundsObservation,
    BetfairReadOnlyClient,
    BetfairReadOnlyError,
    BetfairSessionCredentials,
)


VENUE_ID = "betfair"
SOURCE_FAMILY = "betfair.account-funds-precheck.v1"
MAX_FUNDS_EVIDENCE_AGE = timedelta(seconds=30)
_MAX_FUTURE_SKEW = timedelta(seconds=1)


class BetfairAccountFundsPrecheckError(RuntimeError):
    """Raised when account-funds evidence cannot support a fail-closed precheck."""


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetfairAccountFundsPrecheck:
    """Immutable decision-time funds evidence; source authority is process-local."""

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
            raise BetfairAccountFundsPrecheckError("venue_id is production-owned")
        if not self.account_id.startswith("betfair-account-evidence:"):
            raise BetfairAccountFundsPrecheckError(
                "account_id must derive from authenticated account evidence"
            )
        account_digest = self.account_id.removeprefix("betfair-account-evidence:")
        _sha256_hex(account_digest, "account_id evidence digest")
        if account_digest != self.account_details_sha256:
            raise BetfairAccountFundsPrecheckError(
                "account_id does not match account-details evidence"
            )
        if self.adapter_id != ADAPTER_ID or self.adapter_version != ADAPTER_VERSION:
            raise BetfairAccountFundsPrecheckError("Betfair adapter identity mismatch")
        _nonnegative_decimal(self.required_liability, "required_liability")
        _nonnegative_decimal(
            self.available_to_bet_balance, "available_to_bet_balance"
        )
        _currency_code(self.currency_code, "currency_code")
        for label, value in (
            ("account_observed_at", self.account_observed_at),
            ("funds_observed_at", self.funds_observed_at),
            ("evaluated_at", self.evaluated_at),
        ):
            _utc(value, label)
        if self.account_observed_at > self.evaluated_at + _MAX_FUTURE_SKEW:
            raise BetfairAccountFundsPrecheckError(
                "account evidence is future-dated relative to evaluation"
            )
        if self.funds_observed_at > self.evaluated_at + _MAX_FUTURE_SKEW:
            raise BetfairAccountFundsPrecheckError(
                "funds evidence is future-dated relative to evaluation"
            )
        if self.evaluated_at - self.funds_observed_at > MAX_FUNDS_EVIDENCE_AGE:
            raise BetfairAccountFundsPrecheckError("funds evidence is stale")
        _sha256_hex(self.account_details_sha256, "account_details_sha256")
        _sha256_hex(self.account_funds_sha256, "account_funds_sha256")

    @property
    def passed(self) -> bool:
        return self.available_to_bet_balance >= self.required_liability

    @property
    def execution_authorized(self) -> bool:
        """Funds sufficiency never grants provider-write or real-money authority."""
        return False

    @property
    def precheck_id(self) -> str:
        payload = {
            "source_family": SOURCE_FAMILY,
            "venue_id": self.venue_id,
            "account_id": self.account_id,
            "adapter_id": self.adapter_id,
            "adapter_version": self.adapter_version,
            "required_liability": _decimal_text(self.required_liability),
            "available_to_bet_balance": _decimal_text(
                self.available_to_bet_balance
            ),
            "currency_code": self.currency_code,
            "account_observed_at": _datetime_text(self.account_observed_at),
            "funds_observed_at": _datetime_text(self.funds_observed_at),
            "evaluated_at": _datetime_text(self.evaluated_at),
            "account_details_sha256": self.account_details_sha256,
            "account_funds_sha256": self.account_funds_sha256,
            "passed": self.passed,
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class _IssuedFundsPrecheckRecord:
    value_ref: ReferenceType[BetfairAccountFundsPrecheck]
    precheck_id: str


_ISSUED: dict[int, _IssuedFundsPrecheckRecord] = {}


def _remember_issued(result: BetfairAccountFundsPrecheck) -> None:
    identity = id(result)

    def _discard(dead_ref: ReferenceType[BetfairAccountFundsPrecheck]) -> None:
        record = _ISSUED.get(identity)
        if record is not None and record.value_ref is dead_ref:
            _ISSUED.pop(identity, None)

    value_ref = ref(result, _discard)
    _ISSUED[identity] = _IssuedFundsPrecheckRecord(
        value_ref=value_ref,
        precheck_id=result.precheck_id,
    )


def evaluate_betfair_account_funds(
    credentials: BetfairSessionCredentials,
    required_liability: Decimal,
    *,
    required_currency_code: str,
    timeout_seconds: float = 10.0,
) -> BetfairAccountFundsPrecheck:
    """Acquire current provider evidence and issue one process-local precheck result."""

    if type(credentials) is not BetfairSessionCredentials:
        raise TypeError("credentials must be BetfairSessionCredentials")
    _nonnegative_decimal(required_liability, "required_liability")
    required_currency_code = _currency_code(
        required_currency_code, "required_currency_code"
    )

    client = BetfairReadOnlyClient(
        credentials,
        timeout_seconds=timeout_seconds,
        venue_id=VENUE_ID,
        account_id="authenticated-account",
    )
    try:
        details = client.read_account_details()
        funds = client.read_account_funds()
    except BetfairReadOnlyError as exc:
        raise BetfairAccountFundsPrecheckError(
            "Betfair account-funds acquisition failed"
        ) from exc
    if type(details) is not BetfairAccountDetailsObservation:
        raise BetfairAccountFundsPrecheckError(
            "account-details acquisition returned non-canonical evidence"
        )
    if type(funds) is not BetfairAccountFundsObservation:
        raise BetfairAccountFundsPrecheckError(
            "account-funds acquisition returned non-canonical evidence"
        )
    account_currency_code = _currency_code(
        details.currency_code, "account currency_code"
    )
    if required_currency_code != account_currency_code:
        raise BetfairAccountFundsPrecheckError(
            "required liability currency does not match Betfair account currency"
        )

    evaluated_at = _utc_now()
    account_observed_at = _parse_provider_time(
        details.evidence.observed_at, "account observed_at"
    )
    funds_observed_at = _parse_provider_time(
        funds.evidence.observed_at, "funds observed_at"
    )
    result = BetfairAccountFundsPrecheck(
        venue_id=VENUE_ID,
        account_id=(
            "betfair-account-evidence:" + details.evidence.source_payload_sha256
        ),
        adapter_id=ADAPTER_ID,
        adapter_version=ADAPTER_VERSION,
        required_liability=required_liability,
        available_to_bet_balance=funds.available_to_bet_balance,
        currency_code=account_currency_code,
        account_observed_at=account_observed_at,
        funds_observed_at=funds_observed_at,
        evaluated_at=evaluated_at,
        account_details_sha256=details.evidence.source_payload_sha256,
        account_funds_sha256=funds.evidence.source_payload_sha256,
    )
    _remember_issued(result)
    return result


def is_authoritative_funds_precheck(value: object) -> bool:
    """Return whether current, unchanged funds evidence was issued by this process."""

    if type(value) is not BetfairAccountFundsPrecheck:
        return False
    record = _ISSUED.get(id(value))
    if record is None or record.value_ref() is not value:
        return False
    try:
        if not compare_digest(record.precheck_id, value.precheck_id):
            return False
        checked_at = _utc(_utc_now(), "authority checked_at")
        funds_observed_at = _utc(value.funds_observed_at, "funds_observed_at")
        evaluated_at = _utc(value.evaluated_at, "evaluated_at")
    except (AttributeError, TypeError, ValueError, BetfairAccountFundsPrecheckError):
        return False
    if checked_at + _MAX_FUTURE_SKEW < evaluated_at:
        return False
    if funds_observed_at > checked_at + _MAX_FUTURE_SKEW:
        return False
    return checked_at - funds_observed_at <= MAX_FUNDS_EVIDENCE_AGE


def require_authoritative_funds_precheck(
    value: object,
) -> BetfairAccountFundsPrecheck:
    if not is_authoritative_funds_precheck(value):
        raise BetfairAccountFundsPrecheckError(
            "account-funds precheck lacks current-process source authority"
        )
    assert type(value) is BetfairAccountFundsPrecheck
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_provider_time(value: str, field: str) -> datetime:
    if not isinstance(value, str) or not value or value != value.strip():
        raise BetfairAccountFundsPrecheckError(f"{field} must be provider timestamp text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BetfairAccountFundsPrecheckError(f"{field} must be ISO-8601") from exc
    return _utc(parsed, field)


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise BetfairAccountFundsPrecheckError(f"{field} must be timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _nonnegative_decimal(value: Decimal, field: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite() or value < 0:
        raise BetfairAccountFundsPrecheckError(
            f"{field} must be a finite non-negative Decimal"
        )
    return value


def _currency_code(value: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 3
        or not value.isascii()
        or not value.isalpha()
        or value != value.upper()
    ):
        raise BetfairAccountFundsPrecheckError(
            f"{field} must be a three-letter uppercase currency code"
        )
    return value


def _sha256_hex(value: str, field: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise BetfairAccountFundsPrecheckError(
            f"{field} must be lowercase SHA-256 hex"
        )
    return value


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _datetime_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

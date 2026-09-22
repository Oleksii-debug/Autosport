from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
import hashlib
import json
from typing import Iterable
from urllib.parse import urlparse

from .matchbook_wallet_evidence import (
    TransactionType,
    WalletTransaction,
    WalletWindow,
    validate_wallet_window,
)

_MAX_DECIMAL_DIGITS = 256
_MAX_DECIMAL_EXPONENT = 256
_MAX_CANONICAL_DECIMAL_CHARS = 1024


class MatchbookCommissionEvidenceError(ValueError):
    """Commission evidence is malformed, ambiguous, or overclaims authority."""


class CommissionPolicyTier(StrEnum):
    ADVERTISED_POLICY = "ADVERTISED_POLICY"
    ACCOUNT_EFFECTIVE_POLICY = "ACCOUNT_EFFECTIVE_POLICY"


class CommissionCashStatus(StrEnum):
    CASH_ROWS_OBSERVED_UNALLOCATED = "CASH_ROWS_OBSERVED_UNALLOCATED"
    NO_COMMISSION_ROWS_ZERO_NOT_PROVEN = "NO_COMMISSION_ROWS_ZERO_NOT_PROVEN"


@dataclass(frozen=True, slots=True)
class CommissionPolicyEvidence:
    provider: str
    tier: CommissionPolicyTier
    source_url: str
    source_sha256: str
    observed_at: str
    effective_from: str | None
    effective_to: str | None
    rate: Decimal | None
    account_scope_sha256: str | None
    provider_cash_truth: bool
    per_bet_rate_authorized: bool
    evidence_sha256: str


@dataclass(frozen=True, slots=True)
class CurrencyCommissionAmount:
    currency: str
    amount: Decimal
    transaction_count: int


@dataclass(frozen=True, slots=True)
class CommissionCashEvidence:
    provider: str
    wallet_window_sha256: str
    query_after: str
    query_before: str
    observed_at: str
    transaction_ids: tuple[str, ...]
    amounts_by_currency: tuple[CurrencyCommissionAmount, ...]
    status: CommissionCashStatus
    cash_zero_proven: bool
    economic_allocation_authorized: bool
    provider_api_cost_authorized: bool
    positive_net_edge_proven: bool
    evidence_sha256: str


@dataclass(frozen=True, slots=True)
class CommissionEconomicView:
    economic_scope_id: str
    currency: str
    gross_pnl: Decimal
    observed_commission_cash_same_currency: Decimal | None
    commission_transaction_ids: tuple[str, ...]
    net_economic_pnl: None
    commission_allocation_status: str
    positive_net_edge_proven: bool
    evidence_sha256: str


def build_commission_policy_evidence(
    *,
    tier: CommissionPolicyTier,
    source_url: str,
    source_sha256: str,
    observed_at: str,
    rate: Decimal | None = None,
    effective_from: str | None = None,
    effective_to: str | None = None,
    account_scope_sha256: str | None = None,
) -> CommissionPolicyEvidence:
    """Bind descriptive commission policy without promoting it to cash truth."""
    if type(tier) is not CommissionPolicyTier:
        raise MatchbookCommissionEvidenceError("tier must be CommissionPolicyTier")
    source_url = _https_url(source_url)
    source_sha256 = _sha256(source_sha256, "source_sha256")
    observed_at = _utc(observed_at, "observed_at")
    effective_from = (
        None if effective_from is None else _utc(effective_from, "effective_from")
    )
    effective_to = None if effective_to is None else _utc(effective_to, "effective_to")
    if (
        effective_from is not None
        and effective_to is not None
        and _parse_utc(effective_from) >= _parse_utc(effective_to)
    ):
        raise MatchbookCommissionEvidenceError(
            "effective_from must precede effective_to"
        )

    canonical_rate = None if rate is None else _bounded_decimal(rate, "rate")
    if canonical_rate is not None and not Decimal("0") <= canonical_rate <= Decimal("1"):
        raise MatchbookCommissionEvidenceError("rate must be within [0, 1]")

    if tier is CommissionPolicyTier.ADVERTISED_POLICY:
        if account_scope_sha256 is not None:
            raise MatchbookCommissionEvidenceError(
                "advertised policy cannot claim account scope"
            )
        account_scope = None
    else:
        if account_scope_sha256 is None:
            raise MatchbookCommissionEvidenceError(
                "account-effective policy requires account evidence identity"
            )
        account_scope = _sha256(account_scope_sha256, "account_scope_sha256")

    payload = {
        "schema_version": 1,
        "provider": "matchbook",
        "tier": tier.value,
        "source_url": source_url,
        "source_sha256": source_sha256,
        "observed_at": observed_at,
        "effective_from": effective_from,
        "effective_to": effective_to,
        "rate": None if canonical_rate is None else _canonical_decimal(canonical_rate),
        "account_scope_sha256": account_scope,
        "provider_cash_truth": False,
        "per_bet_rate_authorized": False,
    }
    return CommissionPolicyEvidence(
        provider="matchbook",
        tier=tier,
        source_url=source_url,
        source_sha256=source_sha256,
        observed_at=observed_at,
        effective_from=effective_from,
        effective_to=effective_to,
        rate=canonical_rate,
        account_scope_sha256=account_scope,
        provider_cash_truth=False,
        per_bet_rate_authorized=False,
        evidence_sha256=_digest(payload),
    )


def derive_commission_cash_evidence(window: WalletWindow) -> CommissionCashEvidence:
    """Extract typed commission cash rows from canonical Matchbook wallet evidence."""
    if type(window) is not WalletWindow or not validate_wallet_window(window):
        raise MatchbookCommissionEvidenceError("wallet window failed validation")
    if TransactionType.COMMISSION not in window.transaction_types:
        raise MatchbookCommissionEvidenceError(
            "wallet query did not include commission transactions"
        )

    rows = tuple(
        row
        for row in window.transactions
        if row.transaction_type is TransactionType.COMMISSION
    )
    for row in rows:
        if type(row) is not WalletTransaction:
            raise MatchbookCommissionEvidenceError(
                "commission rows require exact WalletTransaction"
            )

    grouped: dict[str, list[Decimal]] = {}
    for row in rows:
        grouped.setdefault(_currency(row.currency), []).append(
            _bounded_decimal(row.cash_delta, "commission cash_delta")
        )

    amounts = tuple(
        CurrencyCommissionAmount(
            currency=currency,
            amount=_exact_sum(values),
            transaction_count=len(values),
        )
        for currency, values in sorted(grouped.items())
    )
    transaction_ids = tuple(sorted(row.transaction_id for row in rows))
    if len(set(transaction_ids)) != len(transaction_ids):
        raise MatchbookCommissionEvidenceError(
            "duplicate commission transaction identity"
        )

    status = (
        CommissionCashStatus.CASH_ROWS_OBSERVED_UNALLOCATED
        if rows
        else CommissionCashStatus.NO_COMMISSION_ROWS_ZERO_NOT_PROVEN
    )
    payload = {
        "schema_version": 1,
        "provider": "matchbook",
        "wallet_window_sha256": _sha256(
            window.evidence_sha256, "wallet_window_sha256"
        ),
        "query_after": _utc(window.after, "query_after"),
        "query_before": _utc(window.before, "query_before"),
        "observed_at": _utc(window.observed_at, "observed_at"),
        "transaction_ids": list(transaction_ids),
        "amounts_by_currency": [
            {
                "currency": item.currency,
                "amount": _canonical_decimal(item.amount),
                "transaction_count": item.transaction_count,
            }
            for item in amounts
        ],
        "status": status.value,
        "cash_zero_proven": False,
        "economic_allocation_authorized": False,
        "provider_api_cost_authorized": False,
        "positive_net_edge_proven": False,
    }
    return CommissionCashEvidence(
        provider="matchbook",
        wallet_window_sha256=payload["wallet_window_sha256"],
        query_after=payload["query_after"],
        query_before=payload["query_before"],
        observed_at=payload["observed_at"],
        transaction_ids=transaction_ids,
        amounts_by_currency=amounts,
        status=status,
        cash_zero_proven=False,
        economic_allocation_authorized=False,
        provider_api_cost_authorized=False,
        positive_net_edge_proven=False,
        evidence_sha256=_digest(payload),
    )


def build_unallocated_commission_economic_view(
    *,
    economic_scope_id: str,
    currency: str,
    gross_pnl: Decimal,
    commission: CommissionCashEvidence,
) -> CommissionEconomicView:
    """Expose known gross and account/provider cash without fabricating attribution."""
    scope = _identifier(economic_scope_id, "economic_scope_id")
    currency = _currency(currency)
    gross = _bounded_decimal(gross_pnl, "gross_pnl")
    if type(commission) is not CommissionCashEvidence:
        raise MatchbookCommissionEvidenceError(
            "commission must be CommissionCashEvidence"
        )
    _validate_commission_cash_evidence(commission)

    same_currency = next(
        (item.amount for item in commission.amounts_by_currency if item.currency == currency),
        None,
    )
    status = (
        "UNALLOCATED_PROVIDER_COMMISSION"
        if commission.transaction_ids
        else "COMMISSION_APPLICABILITY_UNRESOLVED"
    )
    payload = {
        "schema_version": 1,
        "economic_scope_id": scope,
        "currency": currency,
        "gross_pnl": _canonical_decimal(gross),
        "commission_evidence_sha256": commission.evidence_sha256,
        "observed_commission_cash_same_currency": (
            None if same_currency is None else _canonical_decimal(same_currency)
        ),
        "commission_transaction_ids": list(commission.transaction_ids),
        "net_economic_pnl": None,
        "commission_allocation_status": status,
        "positive_net_edge_proven": False,
    }
    return CommissionEconomicView(
        economic_scope_id=scope,
        currency=currency,
        gross_pnl=gross,
        observed_commission_cash_same_currency=same_currency,
        commission_transaction_ids=commission.transaction_ids,
        net_economic_pnl=None,
        commission_allocation_status=status,
        positive_net_edge_proven=False,
        evidence_sha256=_digest(payload),
    )


def _validate_commission_cash_evidence(value: CommissionCashEvidence) -> None:
    if value.provider != "matchbook":
        raise MatchbookCommissionEvidenceError("provider must be matchbook")
    _sha256(value.wallet_window_sha256, "wallet_window_sha256")
    _sha256(value.evidence_sha256, "evidence_sha256")
    _utc(value.query_after, "query_after")
    _utc(value.query_before, "query_before")
    _utc(value.observed_at, "observed_at")
    if _parse_utc(value.query_after) >= _parse_utc(value.query_before):
        raise MatchbookCommissionEvidenceError("invalid commission query window")
    if value.cash_zero_proven is not False:
        raise MatchbookCommissionEvidenceError("cash zero authority is not available")
    if value.economic_allocation_authorized is not False:
        raise MatchbookCommissionEvidenceError(
            "economic allocation authority is not available"
        )
    if value.provider_api_cost_authorized is not False:
        raise MatchbookCommissionEvidenceError(
            "commission evidence cannot authorize API-cost attribution"
        )
    if value.positive_net_edge_proven is not False:
        raise MatchbookCommissionEvidenceError(
            "commission evidence cannot prove positive net edge"
        )

    ids = tuple(sorted(_identifier(item, "transaction_id") for item in value.transaction_ids))
    if ids != value.transaction_ids or len(set(ids)) != len(ids):
        raise MatchbookCommissionEvidenceError(
            "commission transaction ids must be unique sorted identities"
        )

    previous = ""
    for item in value.amounts_by_currency:
        if type(item) is not CurrencyCommissionAmount:
            raise MatchbookCommissionEvidenceError(
                "amounts_by_currency requires exact CurrencyCommissionAmount"
            )
        currency = _currency(item.currency)
        if currency <= previous:
            raise MatchbookCommissionEvidenceError(
                "commission currency buckets must be strictly sorted"
            )
        previous = currency
        _bounded_decimal(item.amount, "commission amount")
        if type(item.transaction_count) is not int or item.transaction_count <= 0:
            raise MatchbookCommissionEvidenceError(
                "transaction_count must be positive integer"
            )

    if value.status is CommissionCashStatus.CASH_ROWS_OBSERVED_UNALLOCATED:
        if not ids or not value.amounts_by_currency:
            raise MatchbookCommissionEvidenceError(
                "observed commission status requires rows"
            )
        if sum(item.transaction_count for item in value.amounts_by_currency) != len(ids):
            raise MatchbookCommissionEvidenceError(
                "currency bucket counts do not cover commission rows"
            )
    elif value.status is CommissionCashStatus.NO_COMMISSION_ROWS_ZERO_NOT_PROVEN:
        if ids or value.amounts_by_currency:
            raise MatchbookCommissionEvidenceError(
                "no-row status cannot contain commission rows"
            )
    else:
        raise MatchbookCommissionEvidenceError("unsupported commission status")

    payload = {
        "schema_version": 1,
        "provider": value.provider,
        "wallet_window_sha256": value.wallet_window_sha256,
        "query_after": value.query_after,
        "query_before": value.query_before,
        "observed_at": value.observed_at,
        "transaction_ids": list(value.transaction_ids),
        "amounts_by_currency": [
            {
                "currency": item.currency,
                "amount": _canonical_decimal(item.amount),
                "transaction_count": item.transaction_count,
            }
            for item in value.amounts_by_currency
        ],
        "status": value.status.value,
        "cash_zero_proven": value.cash_zero_proven,
        "economic_allocation_authorized": value.economic_allocation_authorized,
        "provider_api_cost_authorized": value.provider_api_cost_authorized,
        "positive_net_edge_proven": value.positive_net_edge_proven,
    }
    if value.evidence_sha256 != _digest(payload):
        raise MatchbookCommissionEvidenceError(
            "commission evidence digest mismatch"
        )


def _bounded_decimal(value: Decimal, label: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise MatchbookCommissionEvidenceError(f"{label} must be finite Decimal")
    _, digits, exponent = value.as_tuple()
    if len(digits) > _MAX_DECIMAL_DIGITS or abs(exponent) > _MAX_DECIMAL_EXPONENT:
        raise MatchbookCommissionEvidenceError(
            f"{label} exceeds bounded exact-decimal domain"
        )
    if len(_canonical_decimal(value)) > _MAX_CANONICAL_DECIMAL_CHARS:
        raise MatchbookCommissionEvidenceError(
            f"{label} exceeds canonical decimal size"
        )
    return value


def _exact_sum(values: Iterable[Decimal]) -> Decimal:
    vals = tuple(_bounded_decimal(value, "commission amount") for value in values)
    if not vals:
        return Decimal("0")
    exponents = [value.as_tuple().exponent for value in vals]
    minimum = min(exponents)
    if max(exponents) - minimum > _MAX_DECIMAL_EXPONENT * 2:
        raise MatchbookCommissionEvidenceError(
            "commission exponent spread exceeds bounded exact domain"
        )
    total = 0
    for value in vals:
        sign, digits, exponent = value.as_tuple()
        coefficient = int("".join(str(digit) for digit in digits)) if digits else 0
        if sign:
            coefficient = -coefficient
        total += coefficient * (10 ** (exponent - minimum))
    sign = 1 if total < 0 else 0
    digits = tuple(int(char) for char in str(abs(total))) if total else (0,)
    return _bounded_decimal(Decimal((sign, digits, minimum)), "commission total")


def _canonical_decimal(value: Decimal) -> str:
    if type(value) is not Decimal or not value.is_finite():
        raise MatchbookCommissionEvidenceError("decimal must be finite")
    sign, raw_digits, exponent = value.as_tuple()
    digits = list(raw_digits) or [0]
    if all(digit == 0 for digit in digits):
        return "0"
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exponent += 1
    coefficient = "".join(str(digit) for digit in digits)
    if exponent >= 0:
        text = coefficient + ("0" * exponent)
    else:
        point = len(coefficient) + exponent
        if point > 0:
            text = coefficient[:point] + "." + coefficient[point:]
        else:
            text = "0." + ("0" * (-point)) + coefficient
    return ("-" if sign else "") + text


def _utc(value: str, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise MatchbookCommissionEvidenceError(f"{label} must be timestamp text")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MatchbookCommissionEvidenceError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise MatchbookCommissionEvidenceError(f"{label} requires timezone")
    normalized = parsed.astimezone(timezone.utc)
    if normalized.microsecond:
        return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")
    return normalized.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _sha256(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise MatchbookCommissionEvidenceError(
            f"{label} must be lowercase SHA-256"
        )
    return value


def _https_url(value: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise MatchbookCommissionEvidenceError("source_url must be text")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.fragment:
        raise MatchbookCommissionEvidenceError(
            "source_url must be absolute fragment-free HTTPS URL"
        )
    return value


def _currency(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 3
        or value != value.upper()
        or not value.isalpha()
    ):
        raise MatchbookCommissionEvidenceError(
            "currency must be uppercase ISO-like code"
        )
    return value


def _identifier(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 256
        or any(ord(char) < 32 for char in value)
    ):
        raise MatchbookCommissionEvidenceError(f"{label} is invalid")
    return value


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()

"""Read-only provider-account funds feasibility from point-in-time balance evidence.

This module deliberately does not create execution, reservation, transfer, bankroll,
or P&L authority.  A positive result means only that the provider reported enough
available balance in the supplied fresh account snapshot for the exact proposed
allocation.  The funds are not reserved and may change before any later action.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
import json

from .bookmaker_capability import (
    BookmakerAccountSnapshot,
    BookmakerBalanceObservation,
    BookmakerCapability,
    BookmakerCapabilityFact,
    BookmakerCapabilityProfile,
)


class ProviderFundsFeasibilityError(ValueError):
    """Raised when the projection request itself is ambiguous or non-canonical."""


class ProviderFundsState(str, Enum):
    SNAPSHOT_SUFFICIENT_BUT_UNRESERVED = "snapshot_sufficient_but_unreserved"
    INSUFFICIENT = "insufficient"
    UNKNOWN = "unknown"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderFundsFeasibilityError(
            f"{name} must be a non-empty canonical string"
        )
    if "\x00" in value:
        raise ProviderFundsFeasibilityError(f"{name} must not contain NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise ProviderFundsFeasibilityError(
            f"{name} must be valid UTF-8 text"
        ) from exc
    return value


def _timestamp(value: object, name: str) -> tuple[str, datetime]:
    raw = _text(value, name)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderFundsFeasibilityError(
            f"{name} must be timezone-aware ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderFundsFeasibilityError(
            f"{name} must be timezone-aware ISO-8601"
        )
    return raw, parsed


def _currency(value: object, name: str) -> str:
    raw = _text(value, name)
    if (
        len(raw) != 3
        or not raw.isascii()
        or not raw.isalpha()
        or raw != raw.upper()
    ):
        raise ProviderFundsFeasibilityError(
            f"{name} must be a three-letter uppercase ASCII code"
        )
    return raw


def _nonnegative_decimal(value: object, name: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise ProviderFundsFeasibilityError(
            f"{name} must be a non-negative finite exact Decimal"
        )
    return value


def _positive_decimal(value: object, name: str) -> Decimal:
    result = _nonnegative_decimal(value, name)
    if result <= 0:
        raise ProviderFundsFeasibilityError(
            f"{name} must be a positive finite exact Decimal"
        )
    return result


def _decimal_text(value: Decimal) -> str:
    _nonnegative_decimal(value, "decimal value")
    sign, digits, exponent = value.as_tuple()
    if sign:
        raise ProviderFundsFeasibilityError("decimal value must be non-negative")
    if not digits or all(digit == 0 for digit in digits):
        return "0"
    canonical_digits = list(digits)
    canonical_exponent = exponent
    while canonical_digits[-1] == 0:
        canonical_digits.pop()
        canonical_exponent += 1
    return str(Decimal((0, tuple(canonical_digits), canonical_exponent)))


def _exact_positive_sum(values: tuple[Decimal, ...]) -> Decimal:
    if not values:
        return Decimal("0")
    for value in values:
        _positive_decimal(value, "allocation amount")
    min_exponent = min(int(value.as_tuple().exponent) for value in values)
    total = 0
    for value in values:
        sign, digits, exponent = value.as_tuple()
        if sign:
            raise ProviderFundsFeasibilityError(
                "allocation amount must be non-negative"
            )
        coefficient = 0
        for digit in digits:
            coefficient = coefficient * 10 + digit
        total += coefficient * (10 ** (int(exponent) - min_exponent))
    if total == 0:
        return Decimal("0")
    return Decimal((0, tuple(int(ch) for ch in str(total)), min_exponent))


def _seconds(delta_seconds: int, microseconds: int) -> Decimal:
    return Decimal(delta_seconds) + Decimal(microseconds) / Decimal("1000000")


@dataclass(frozen=True, slots=True)
class ProviderFundsAllocation:
    allocation_id: str
    venue_id: str
    account_id: str
    adapter_id: str
    currency: str
    amount: Decimal

    def __post_init__(self) -> None:
        _text(self.allocation_id, "allocation_id")
        _text(self.venue_id, "venue_id")
        _text(self.account_id, "account_id")
        _text(self.adapter_id, "adapter_id")
        _currency(self.currency, "currency")
        _positive_decimal(self.amount, "amount")

    @property
    def account_key(self) -> tuple[str, str, str]:
        return (self.venue_id, self.account_id, self.adapter_id)


@dataclass(frozen=True, slots=True)
class ProviderFundsAssessment:
    venue_id: str
    account_id: str
    adapter_id: str
    currency: str
    requested_amount: Decimal
    state: ProviderFundsState
    reason: str
    available_balance: Decimal | None = None
    balance_observation_id: str | None = None
    balance_source_payload_sha256: str | None = None
    balance_observed_at: str | None = None

    def __post_init__(self) -> None:
        _text(self.venue_id, "assessment venue_id")
        _text(self.account_id, "assessment account_id")
        _text(self.adapter_id, "assessment adapter_id")
        _currency(self.currency, "assessment currency")
        _positive_decimal(self.requested_amount, "assessment requested_amount")
        if not isinstance(self.state, ProviderFundsState):
            raise ProviderFundsFeasibilityError(
                "assessment state must be ProviderFundsState"
            )
        _text(self.reason, "assessment reason")
        evidence_fields = (
            self.available_balance,
            self.balance_observation_id,
            self.balance_source_payload_sha256,
            self.balance_observed_at,
        )
        has_evidence = any(value is not None for value in evidence_fields)
        if has_evidence and any(value is None for value in evidence_fields):
            raise ProviderFundsFeasibilityError(
                "balance assessment evidence fields must be present together"
            )
        if self.available_balance is not None:
            _nonnegative_decimal(
                self.available_balance,
                "assessment available_balance",
            )
            _text(
                self.balance_observation_id,
                "assessment balance_observation_id",
            )
            digest = _text(
                self.balance_source_payload_sha256,
                "assessment balance_source_payload_sha256",
            )
            if (
                len(digest) != 64
                or digest != digest.lower()
                or any(ch not in "0123456789abcdef" for ch in digest)
            ):
                raise ProviderFundsFeasibilityError(
                    "assessment balance_source_payload_sha256 must be SHA-256 hex"
                )
            _timestamp(
                self.balance_observed_at,
                "assessment balance_observed_at",
            )

    @property
    def account_key(self) -> tuple[str, str, str]:
        return (self.venue_id, self.account_id, self.adapter_id)


@dataclass(frozen=True, slots=True)
class ProviderFundsFeasibilityReport:
    decision_ts: str
    max_balance_age_seconds: Decimal
    assessments: tuple[ProviderFundsAssessment, ...]

    def __post_init__(self) -> None:
        _timestamp(self.decision_ts, "decision_ts")
        _nonnegative_decimal(
            self.max_balance_age_seconds,
            "max_balance_age_seconds",
        )
        if type(self.assessments) is not tuple or not self.assessments:
            raise ProviderFundsFeasibilityError(
                "assessments must be a non-empty canonical tuple"
            )
        if any(
            type(item) is not ProviderFundsAssessment
            for item in self.assessments
        ):
            raise ProviderFundsFeasibilityError(
                "assessments must contain exact ProviderFundsAssessment values"
            )
        keys = tuple(item.account_key for item in self.assessments)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ProviderFundsFeasibilityError(
                "assessments must be sorted and unique by account identity"
            )

    @property
    def all_snapshot_sufficient(self) -> bool:
        return all(
            item.state
            is ProviderFundsState.SNAPSHOT_SUFFICIENT_BUT_UNRESERVED
            for item in self.assessments
        )

    @property
    def provider_write_authorized(self) -> bool:
        return False

    @property
    def real_money_execution(self) -> bool:
        return False

    @property
    def funds_reserved(self) -> bool:
        return False

    @property
    def report_sha256(self) -> str:
        payload = {
            "schema": "autosport.provider_funds_feasibility",
            "schema_version": 1,
            "decision_ts": self.decision_ts,
            "max_balance_age_seconds": _decimal_text(
                self.max_balance_age_seconds
            ),
            "provider_write_authorized": False,
            "real_money_execution": False,
            "funds_reserved": False,
            "assessments": [
                {
                    "venue_id": item.venue_id,
                    "account_id": item.account_id,
                    "adapter_id": item.adapter_id,
                    "currency": item.currency,
                    "requested_amount": _decimal_text(item.requested_amount),
                    "state": item.state.value,
                    "reason": item.reason,
                    "available_balance": (
                        None
                        if item.available_balance is None
                        else _decimal_text(item.available_balance)
                    ),
                    "balance_observation_id": item.balance_observation_id,
                    "balance_source_payload_sha256": (
                        item.balance_source_payload_sha256
                    ),
                    "balance_observed_at": item.balance_observed_at,
                }
                for item in self.assessments
            ],
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()


def _validate_snapshot_shape(snapshot: object) -> BookmakerAccountSnapshot:
    if type(snapshot) is not BookmakerAccountSnapshot:
        raise ProviderFundsFeasibilityError(
            "snapshots must contain exact BookmakerAccountSnapshot values"
        )
    if type(snapshot.profile) is not BookmakerCapabilityProfile:
        raise ProviderFundsFeasibilityError(
            "snapshot profile must be exact BookmakerCapabilityProfile"
        )
    if any(type(fact) is not BookmakerCapabilityFact for fact in snapshot.profile.facts):
        raise ProviderFundsFeasibilityError(
            "snapshot profile facts must be exact BookmakerCapabilityFact values"
        )
    if type(snapshot.observed_capabilities) is not frozenset:
        raise ProviderFundsFeasibilityError(
            "snapshot observed_capabilities must be an exact frozenset"
        )
    if snapshot.balance is not None and type(snapshot.balance) is not BookmakerBalanceObservation:
        raise ProviderFundsFeasibilityError(
            "snapshot balance must be exact BookmakerBalanceObservation"
        )
    return snapshot


def assess_provider_funds(
    allocations: tuple[ProviderFundsAllocation, ...],
    snapshots: tuple[BookmakerAccountSnapshot, ...],
    *,
    decision_ts: str,
    max_balance_age_seconds: Decimal,
) -> ProviderFundsFeasibilityReport:
    """Project provider-local balance sufficiency without reserving or moving funds."""

    decision_raw, decision_time = _timestamp(decision_ts, "decision_ts")
    max_age = _nonnegative_decimal(
        max_balance_age_seconds,
        "max_balance_age_seconds",
    )
    if type(allocations) is not tuple or not allocations:
        raise ProviderFundsFeasibilityError(
            "allocations must be a non-empty canonical tuple"
        )
    if any(type(item) is not ProviderFundsAllocation for item in allocations):
        raise ProviderFundsFeasibilityError(
            "allocations must contain exact ProviderFundsAllocation values"
        )
    allocation_ids = tuple(item.allocation_id for item in allocations)
    if len(allocation_ids) != len(set(allocation_ids)):
        raise ProviderFundsFeasibilityError("allocation_id values must be unique")

    if type(snapshots) is not tuple:
        raise ProviderFundsFeasibilityError(
            "snapshots must be a canonical tuple"
        )
    validated_snapshots = tuple(_validate_snapshot_shape(item) for item in snapshots)
    by_account: dict[tuple[str, str, str], BookmakerAccountSnapshot] = {}
    for snapshot in validated_snapshots:
        key = (
            snapshot.profile.venue_id,
            snapshot.profile.account_id,
            snapshot.profile.adapter_id,
        )
        if key in by_account:
            raise ProviderFundsFeasibilityError(
                "multiple snapshots for one provider account are ambiguous"
            )
        by_account[key] = snapshot

    grouped: dict[
        tuple[str, str, str, str],
        list[ProviderFundsAllocation],
    ] = {}
    for allocation in allocations:
        key = (
            allocation.venue_id,
            allocation.account_id,
            allocation.adapter_id,
            allocation.currency,
        )
        grouped.setdefault(key, []).append(allocation)

    assessments: list[ProviderFundsAssessment] = []
    for key in sorted(grouped):
        venue_id, account_id, adapter_id, currency = key
        amount = _exact_positive_sum(
            tuple(item.amount for item in grouped[key])
        )
        snapshot = by_account.get((venue_id, account_id, adapter_id))
        if snapshot is None:
            assessments.append(
                ProviderFundsAssessment(
                    venue_id=venue_id,
                    account_id=account_id,
                    adapter_id=adapter_id,
                    currency=currency,
                    requested_amount=amount,
                    state=ProviderFundsState.UNKNOWN,
                    reason="no exact provider-account balance snapshot was supplied",
                )
            )
            continue

        _, snapshot_time = _timestamp(
            snapshot.observed_at,
            "snapshot observed_at",
        )
        if snapshot_time > decision_time:
            assessments.append(
                ProviderFundsAssessment(
                    venue_id=venue_id,
                    account_id=account_id,
                    adapter_id=adapter_id,
                    currency=currency,
                    requested_amount=amount,
                    state=ProviderFundsState.UNKNOWN,
                    reason="account snapshot is from the future relative to decision time",
                )
            )
            continue

        if (
            BookmakerCapability.BALANCE_READ
            not in snapshot.observed_capabilities
            or snapshot.balance is None
        ):
            assessments.append(
                ProviderFundsAssessment(
                    venue_id=venue_id,
                    account_id=account_id,
                    adapter_id=adapter_id,
                    currency=currency,
                    requested_amount=amount,
                    state=ProviderFundsState.UNKNOWN,
                    reason="balance_read was not completely observed for this snapshot",
                )
            )
            continue

        balance = snapshot.balance
        if balance.currency != currency:
            assessments.append(
                ProviderFundsAssessment(
                    venue_id=venue_id,
                    account_id=account_id,
                    adapter_id=adapter_id,
                    currency=currency,
                    requested_amount=amount,
                    state=ProviderFundsState.UNKNOWN,
                    reason="provider balance currency does not match allocation currency",
                )
            )
            continue

        _, balance_time = _timestamp(
            balance.observed_at,
            "balance observed_at",
        )
        if balance_time > decision_time:
            assessments.append(
                ProviderFundsAssessment(
                    venue_id=venue_id,
                    account_id=account_id,
                    adapter_id=adapter_id,
                    currency=currency,
                    requested_amount=amount,
                    state=ProviderFundsState.UNKNOWN,
                    reason="balance observation is from the future relative to decision time",
                )
            )
            continue

        age = decision_time - balance_time
        age_seconds = _seconds(
            age.days * 86400 + age.seconds,
            age.microseconds,
        )
        if age_seconds > max_age:
            assessments.append(
                ProviderFundsAssessment(
                    venue_id=venue_id,
                    account_id=account_id,
                    adapter_id=adapter_id,
                    currency=currency,
                    requested_amount=amount,
                    state=ProviderFundsState.UNKNOWN,
                    reason="provider balance observation exceeds the allowed age",
                )
            )
            continue

        sufficient = balance.available_balance >= amount
        assessments.append(
            ProviderFundsAssessment(
                venue_id=venue_id,
                account_id=account_id,
                adapter_id=adapter_id,
                currency=currency,
                requested_amount=amount,
                state=(
                    ProviderFundsState.SNAPSHOT_SUFFICIENT_BUT_UNRESERVED
                    if sufficient
                    else ProviderFundsState.INSUFFICIENT
                ),
                reason=(
                    "fresh provider snapshot reports enough available balance; "
                    "funds are not reserved"
                    if sufficient
                    else "fresh provider snapshot reports insufficient available balance"
                ),
                available_balance=balance.available_balance,
                balance_observation_id=balance.observation_id,
                balance_source_payload_sha256=balance.source_payload_sha256,
                balance_observed_at=balance.observed_at,
            )
        )

    return ProviderFundsFeasibilityReport(
        decision_ts=decision_raw,
        max_balance_age_seconds=max_age,
        assessments=tuple(assessments),
    )

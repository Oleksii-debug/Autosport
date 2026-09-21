from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Protocol


_DECIMAL_PRECISION = 80


class ForwardEconomicEvidenceError(ValueError):
    """Fail-closed validation error for prospective economic evidence."""


class BetSide(StrEnum):
    BACK = "BACK"
    LAY = "LAY"
    NONE = "NONE"


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ForwardEconomicEvidenceError(f"{name} must be a non-empty canonical string")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ForwardEconomicEvidenceError(f"{name} must be a SHA-256 hex digest")
    return text


def _instant(value: object, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ForwardEconomicEvidenceError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(timezone.utc)


def _decimal(value: object, name: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise ForwardEconomicEvidenceError(f"{name} must be a finite Decimal")
    return value


def _positive_decimal(value: object, name: str) -> Decimal:
    result = _decimal(value, name)
    if result <= 0:
        raise ForwardEconomicEvidenceError(f"{name} must be positive")
    return result


def _decimal_text(value: Decimal) -> str:
    value = _decimal(value, "decimal")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _instant_text(value: datetime) -> str:
    return _instant(value, "instant").isoformat().replace("+00:00", "Z")


def _canonical_digest(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _log_threshold(alpha: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = _DECIMAL_PRECISION
        return -(+alpha).ln()


def _log_e_increment(lam: Decimal, value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    if low > value or value > high:
        raise ForwardEconomicEvidenceError("normalized payoff lies outside its pre-outcome bound")
    with localcontext() as context:
        context.prec = _DECIMAL_PRECISION
        width = high - low
        return +(lam * value - (lam * lam * width * width) / Decimal(8))


@dataclass(frozen=True, slots=True)
class AlphaAllocation:
    challenger_id: str
    alpha: Decimal

    def __post_init__(self) -> None:
        _text(self.challenger_id, "challenger_id")
        alpha = _positive_decimal(self.alpha, "alpha")
        if alpha >= 1:
            raise ForwardEconomicEvidenceError("alpha must be less than one")

    def to_payload(self) -> dict[str, object]:
        return {
            "challenger_id": self.challenger_id,
            "alpha": _decimal_text(self.alpha),
        }


@dataclass(frozen=True, slots=True)
class FamilywiseAlphaRegistry:
    family_id: str
    total_alpha: Decimal
    allocations: tuple[AlphaAllocation, ...]
    sealed_at: datetime

    def __post_init__(self) -> None:
        _text(self.family_id, "family_id")
        total = _positive_decimal(self.total_alpha, "total_alpha")
        if total >= 1:
            raise ForwardEconomicEvidenceError("total_alpha must be less than one")
        _instant(self.sealed_at, "sealed_at")
        if not self.allocations:
            raise ForwardEconomicEvidenceError("alpha registry must allocate at least one challenger")
        if any(type(item) is not AlphaAllocation for item in self.allocations):
            raise ForwardEconomicEvidenceError("alpha allocations must be exact AlphaAllocation values")
        challenger_ids = tuple(item.challenger_id for item in self.allocations)
        if challenger_ids != tuple(sorted(challenger_ids)):
            raise ForwardEconomicEvidenceError("alpha allocations must be sorted by challenger_id")
        if len(set(challenger_ids)) != len(challenger_ids):
            raise ForwardEconomicEvidenceError("alpha allocations must have unique challenger_id values")
        with localcontext() as context:
            context.prec = _DECIMAL_PRECISION
            allocated = sum((item.alpha for item in self.allocations), Decimal(0))
        if allocated > total:
            raise ForwardEconomicEvidenceError("familywise alpha allocations exceed total_alpha")

    def allocation_for(self, challenger_id: str) -> Decimal:
        challenger_id = _text(challenger_id, "challenger_id")
        for item in self.allocations:
            if item.challenger_id == challenger_id:
                return item.alpha
        raise ForwardEconomicEvidenceError("challenger is not preallocated in alpha registry")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "family_id": self.family_id,
            "total_alpha": _decimal_text(self.total_alpha),
            "sealed_at": _instant_text(self.sealed_at),
            "allocations": [item.to_payload() for item in self.allocations],
        }

    @property
    def identity_sha256(self) -> str:
        return _canonical_digest(self.to_payload())


@dataclass(frozen=True, slots=True, init=False)
class ForwardEconomicProtocol:
    protocol_id: str
    challenger_id: str
    champion_id: str
    universe_id: str
    universe_sha256: str
    authority_binding_sha256: str
    alpha_registry_sha256: str
    challenger_alpha: Decimal
    minimum_events: int
    risk_unit_currency: Decimal
    maximum_accepted_odds: Decimal
    maximum_drawdown_currency: Decimal
    absolute_lambda: Decimal
    paired_lambda: Decimal
    start_sequence: int
    frozen_at: datetime

    def __init__(
        self,
        *,
        protocol_id: str,
        challenger_id: str,
        champion_id: str,
        universe_id: str,
        universe_sha256: str,
        authority_binding_sha256: str,
        alpha_registry: FamilywiseAlphaRegistry,
        minimum_events: int,
        risk_unit_currency: Decimal,
        maximum_accepted_odds: Decimal,
        maximum_drawdown_currency: Decimal,
        absolute_lambda: Decimal,
        paired_lambda: Decimal,
        start_sequence: int,
        frozen_at: datetime,
    ) -> None:
        if type(alpha_registry) is not FamilywiseAlphaRegistry:
    
"""Betfair commission granularity and explicit attribution contract.

Betfair provider evidence exposes settled bet gross economics and market-level
commission at different granularities. This module keeps those facts separate.
It never invents a per-bet commission allocation and never grants provider-origin
or settlement authority to caller-constructed structural evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
import json
from typing import Any, Mapping


_HEX = frozenset("0123456789abcdef")


class BetfairCommissionAttributionError(ValueError):
    """Raised when economic granularity or allocation evidence is inconsistent."""


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise BetfairCommissionAttributionError(
            f"{field} must be a non-empty canonical string"
        )
    if "\x00" in value:
        raise BetfairCommissionAttributionError(f"{field} must not contain NUL")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise BetfairCommissionAttributionError(
            f"{field} must be valid UTF-8 text"
        ) from exc
    return value


def _currency(value: object) -> str:
    text = _text(value, "currency")
    if text != text.upper() or not text.isascii() or not text.isalnum():
        raise BetfairCommissionAttributionError(
            "currency must be uppercase ASCII alphanumeric text"
        )
    return text


def _sha256(value: object, field: str) -> str:
    digest = _text(value, field)
    if (
        len(digest) != 64
        or digest != digest.lower()
        or any(character not in _HEX for character in digest)
    ):
        raise BetfairCommissionAttributionError(
            f"{field} must be a lowercase SHA-256 hex digest"
        )
    return digest


def _decimal(value: object, field: str) -> Decimal:
    if type(value) is not Decimal or not value.is_finite():
        raise BetfairCommissionAttributionError(
            f"{field} must be a finite Decimal"
        )
    return value


def _decimal_text(value: Decimal) -> str:
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    if normalized in ("", "-0"):
        return "0"
    return normalized


def _exact_sum(values: tuple[Decimal, ...]) -> Decimal:
    """Add finite Decimals without consulting the process Decimal context."""
    if not values:
        return Decimal("0")
    parts: list[tuple[int, int]] = []
    common_exponent = 0
    initialized = False
    for value in values:
        _decimal(value, "exact_sum value")
        decimal_tuple = value.as_tuple()
        coefficient = 0
        for digit in decimal_tuple.digits:
            coefficient = (coefficient * 10) + digit
        if decimal_tuple.sign:
            coefficient = -coefficient
        exponent = int(decimal_tuple.exponent)
        parts.append((coefficient, exponent))
        if not initialized or exponent < common_exponent:
            common_exponent = exponent
            initialized = True
    total = sum(
        coefficient * (10 ** (exponent - common_exponent))
        for coefficient, exponent in parts
    )
    if total == 0:
        return Decimal("0")
    sign = "-" if total < 0 else ""
    digits = str(abs(total))
    if common_exponent >= 0:
        return Decimal(sign + digits + ("0" * common_exponent))
    places = -common_exponent
    if len(digits) > places:
        text = digits[:-places] + "." + digits[-places:]
    else:
        text = "0." + ("0" * (places - len(digits))) + digits
    return Decimal(sign + text)


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(payload: Mapping[str, Any]) -> str:
    return sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class BetGrossAmount:
    """Structural reference to one bet-level gross realized amount."""

    bet_id: str
    market_id: str
    currency: str
    gross_profit: Decimal
    evidence_sha256: str

    def __post_init__(self) -> None:
        _text(self.bet_id, "bet_id")
        _text(self.market_id, "market_id")
        _currency(self.currency)
        _decimal(self.gross_profit, "gross_profit")
        _sha256(self.evidence_sha256, "evidence_sha256")

    def to_payload(self) -> dict[str, object]:
        return {
            "bet_id": self.bet_id,
            "market_id": self.market_id,
            "currency": self.currency,
            "gross_profit": _decimal_text(self.gross_profit),
            "evidence_sha256": self.evidence_sha256,
        }


@dataclass(frozen=True, slots=True)
class MarketCommissionAmount:
    """Structural reference to signed market-level commission charge evidence.

    Positive values are charges subtracted from gross P&L. Negative values are
    credits/reversals added back to gross P&L.
    """

    market_id: str
    currency: str
    commission_charge: Decimal
    evidence_sha256: str

    def __post_init__(self) -> None:
        _text(self.market_id, "market_id")
        _currency(self.currency)
        _decimal(self.commission_charge, "commission_charge")
        _sha256(self.evidence_sha256, "evidence_sha256")

    def to_payload(self) -> dict[str, object]:
        return {
            "market_id": self.market_id,
            "currency": self.currency,
            "commission_charge": _decimal_text(self.commission_charge),
            "evidence_sha256": self.evidence_sha256,
        }


@dataclass(frozen=True, slots=True)
class CommissionAllocationPolicyRef:
    policy_id: str
    policy_version: int
    policy_sha256: str

    def __post_init__(self) -> None:
        _text(self.policy_id, "policy_id")
        if type(self.policy_version) is not int or self.policy_version < 1:
            raise BetfairCommissionAttributionError(
                "policy_version must be a positive integer"
            )
        _sha256(self.policy_sha256, "policy_sha256")

    def to_payload(self) -> dict[str, object]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy_sha256": self.policy_sha256,
        }


@dataclass(frozen=True, slots=True)
class BetCommissionAllocation:
    bet_id: str
    allocated_commission: Decimal

    def __post_init__(self) -> None:
        _text(self.bet_id, "bet_id")
        _decimal(self.allocated_commission, "allocated_commission")

    def to_payload(self) -> dict[str, object]:
        return {
            "bet_id": self.bet_id,
            "allocated_commission": _decimal_text(self.allocated_commission),
        }


@dataclass(frozen=True, slots=True)
class BetAttributedNet:
    bet_id: str
    gross_profit: Decimal
    allocated_commission: Decimal
    derived_net_profit: Decimal
    allocation_policy_id: str
    allocation_policy_version: int
    provider_exact: bool = False

    def __post_init__(self) -> None:
        _text(self.bet_id, "bet_id")
        _decimal(self.gross_profit, "gross_profit")
        _decimal(self.allocated_commission, "allocated_commission")
        _decimal(self.derived_net_profit, "derived_net_profit")
        _text(self.allocation_policy_id, "allocation_policy_id")
        if (
            type(self.allocation_policy_version) is not int
            or self.allocation_policy_version < 1
        ):
            raise BetfairCommissionAttributionError(
                "allocation_policy_version must be a positive integer"
            )
        if self.provider_exact is not False:
            raise BetfairCommissionAttributionError(
                "per-bet allocated net must remain non-provider-exact"
            )
        if self.derived_net_profit != _exact_sum(
            (self.gross_profit, self.allocated_commission.copy_negate())
        ):
            raise BetfairCommissionAttributionError(
                "derived_net_profit must equal gross_profit - allocated_commission"
            )


@dataclass(frozen=True, slots=True)
class MarketCommissionAttribution:
    """One market's structural gross/commission granularity contract."""

    gross_bets: tuple[BetGrossAmount, ...]
    market_commission: MarketCommissionAmount
    allocations: tuple[BetCommissionAllocation, ...] = ()
    allocation_policy: CommissionAllocationPolicyRef | None = None

    def __post_init__(self) -> None:
        if type(self.gross_bets) is not tuple or not self.gross_bets:
            raise BetfairCommissionAttributionError(
                "gross_bets must be a non-empty tuple"
            )
        if type(self.market_commission) is not MarketCommissionAmount:
            raise BetfairCommissionAttributionError(
                "market_commission must be exact MarketCommissionAmount"
            )
        if type(self.allocations) is not tuple:
            raise BetfairCommissionAttributionError("allocations must be a tuple")

        bet_ids: set[str] = set()
        market_id = self.market_commission.market_id
        currency = self.market_commission.currency
        for bet in self.gross_bets:
            if type(bet) is not BetGrossAmount:
                raise BetfairCommissionAttributionError(
                    "gross_bets must contain exact BetGrossAmount values"
                )
            if bet.bet_id in bet_ids:
                raise BetfairCommissionAttributionError("duplicate gross bet_id")
            bet_ids.add(bet.bet_id)
            if bet.market_id != market_id:
                raise BetfairCommissionAttributionError(
                    "gross bet and commission market_id differ"
                )
            if bet.currency != currency:
                raise BetfairCommissionAttributionError(
                    "gross bet and commission currency differ"
                )

        if not self.allocations:
            if self.allocation_policy is not None:
                raise BetfairCommissionAttributionError(
                    "allocation_policy requires explicit per-bet allocations"
                )
            return

        if type(self.allocation_policy) is not CommissionAllocationPolicyRef:
            raise BetfairCommissionAttributionError(
                "per-bet allocations require exact allocation policy identity"
            )
        allocated_ids: set[str] = set()
        allocated_values: list[Decimal] = []
        for item in self.allocations:
            if type(item) is not BetCommissionAllocation:
                raise BetfairCommissionAttributionError(
                    "allocations must contain exact BetCommissionAllocation values"
                )
            if item.bet_id in allocated_ids:
                raise BetfairCommissionAttributionError(
                    "duplicate allocated bet_id"
                )
            allocated_ids.add(item.bet_id)
            allocated_values.append(item.allocated_commission)
        if allocated_ids != bet_ids:
            raise BetfairCommissionAttributionError(
                "allocations must cover exactly the gross bet_id set"
            )
        if _exact_sum(tuple(allocated_values)) != self.market_commission.commission_charge:
            raise BetfairCommissionAttributionError(
                "allocated commission must exactly conserve market commission"
            )

    @property
    def grants_provider_origin_authority(self) -> bool:
        return False

    @property
    def market_gross_profit(self) -> Decimal:
        return _exact_sum(tuple(item.gross_profit for item in self.gross_bets))

    @property
    def market_net_profit(self) -> Decimal:
        return _exact_sum(
            (
                self.market_gross_profit,
                self.market_commission.commission_charge.copy_negate(),
            )
        )

    def per_bet_net(self) -> tuple[BetAttributedNet, ...]:
        if not self.allocations or self.allocation_policy is None:
            raise BetfairCommissionAttributionError(
                "per-bet net requires explicit versioned commission allocation"
            )
        gross_by_id = {item.bet_id: item for item in self.gross_bets}
        allocated_by_id = {item.bet_id: item for item in self.allocations}
        policy = self.allocation_policy
        return tuple(
            BetAttributedNet(
                bet_id=bet_id,
                gross_profit=gross_by_id[bet_id].gross_profit,
                allocated_commission=allocated_by_id[bet_id].allocated_commission,
                derived_net_profit=_exact_sum(
                    (
                        gross_by_id[bet_id].gross_profit,
                        allocated_by_id[bet_id].allocated_commission.copy_negate(),
                    )
                ),
                allocation_policy_id=policy.policy_id,
                allocation_policy_version=policy.policy_version,
                provider_exact=False,
            )
            for bet_id in sorted(gross_by_id)
        )

    @property
    def attribution_id(self) -> str:
        payload: dict[str, object] = {
            "schema": "autosport.betfair_commission_attribution",
            "schema_version": 1,
            "gross_bets": [
                item.to_payload()
                for item in sorted(self.gross_bets, key=lambda item: item.bet_id)
            ],
            "market_commission": self.market_commission.to_payload(),
            "allocations": [
                item.to_payload()
                for item in sorted(self.allocations, key=lambda item: item.bet_id)
            ],
            "allocation_policy": (
                None
                if self.allocation_policy is None
                else self.allocation_policy.to_payload()
            ),
        }
        return _digest(payload)

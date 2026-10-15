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
from weakref import ref


_HEX = frozenset("0123456789abcdef")
_MONEY_MAX_SIGNIFICANT_DIGITS = 64
_MONEY_MIN_EXPONENT = -18
_MONEY_MAX_EXPONENT = 36
_MONEY_MAX_ADJUSTED_EXPONENT = 36


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
    value_tuple = value.as_tuple()
    exponent = int(value_tuple.exponent)
    if (
        len(value_tuple.digits) > _MONEY_MAX_SIGNIFICANT_DIGITS
        or exponent < _MONEY_MIN_EXPONENT
        or exponent > _MONEY_MAX_EXPONENT
        or (value != 0 and value.adjusted() > _MONEY_MAX_ADJUSTED_EXPONENT)
    ):
        raise BetfairCommissionAttributionError(
            f"{field} exceeds the bounded monetary Decimal domain"
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
        result = Decimal(sign + digits + ("0" * common_exponent))
        return _decimal(result, "exact_sum result")
    places = -common_exponent
    if len(digits) > places:
        text = digits[:-places] + "." + digits[-places:]
    else:
        text = "0." + ("0" * (places - len(digits))) + digits
    return _decimal(Decimal(sign + text), "exact_sum result")


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

    venue_id: str
    account_id: str
    bet_id: str
    market_id: str
    currency: str
    gross_profit: Decimal
    evidence_sha256: str

    def __post_init__(self) -> None:
        _text(self.venue_id, "venue_id")
        _text(self.account_id, "account_id")
        _text(self.bet_id, "bet_id")
        _text(self.market_id, "market_id")
        _currency(self.currency)
        _decimal(self.gross_profit, "gross_profit")
        _sha256(self.evidence_sha256, "evidence_sha256")

    def to_payload(self) -> dict[str, object]:
        return {
            "venue_id": self.venue_id,
            "account_id": self.account_id,
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

    venue_id: str
    account_id: str
    market_id: str
    currency: str
    commission_charge: Decimal
    evidence_sha256: str

    def __post_init__(self) -> None:
        _text(self.venue_id, "venue_id")
        _text(self.account_id, "account_id")
        _text(self.market_id, "market_id")
        _currency(self.currency)
        _decimal(self.commission_charge, "commission_charge")
        _sha256(self.evidence_sha256, "evidence_sha256")

    def to_payload(self) -> dict[str, object]:
        return {
            "venue_id": self.venue_id,
            "account_id": self.account_id,
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


@dataclass(frozen=True, slots=True, weakref_slot=True)
class BetAttributedNet:
    venue_id: str
    account_id: str
    market_id: str
    bet_id: str
    gross_profit: Decimal
    allocated_commission: Decimal
    derived_net_profit: Decimal
    attribution_id: str
    allocation_policy_id: str
    allocation_policy_version: int
    allocation_policy_sha256: str
    provider_exact: bool = False

    def __post_init__(self) -> None:
        _text(self.venue_id, "venue_id")
        _text(self.account_id, "account_id")
        _text(self.market_id, "market_id")
        _text(self.bet_id, "bet_id")
        _decimal(self.gross_profit, "gross_profit")
        _decimal(self.allocated_commission, "allocated_commission")
        _decimal(self.derived_net_profit, "derived_net_profit")
        _sha256(self.attribution_id, "attribution_id")
        _text(self.allocation_policy_id, "allocation_policy_id")
        if (
            type(self.allocation_policy_version) is not int
            or self.allocation_policy_version < 1
        ):
            raise BetfairCommissionAttributionError(
                "allocation_policy_version must be a positive integer"
            )
        _sha256(self.allocation_policy_sha256, "allocation_policy_sha256")
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

    def _binding_fingerprint(self) -> str:
        return _digest(
            {
                "schema": "autosport.betfair_attributed_net",
                "schema_version": 2,
                "venue_id": self.venue_id,
                "account_id": self.account_id,
                "market_id": self.market_id,
                "bet_id": self.bet_id,
                "gross_profit": _decimal_text(self.gross_profit),
                "allocated_commission": _decimal_text(
                    self.allocated_commission
                ),
                "derived_net_profit": _decimal_text(self.derived_net_profit),
                "attribution_id": self.attribution_id,
                "allocation_policy_id": self.allocation_policy_id,
                "allocation_policy_version": self.allocation_policy_version,
                "allocation_policy_sha256": self.allocation_policy_sha256,
                "provider_exact": self.provider_exact,
            }
        )

    def assert_derived_from(
        self,
        attribution: "MarketCommissionAttribution",
    ) -> None:
        if type(attribution) is not MarketCommissionAttribution:
            raise BetfairCommissionAttributionError(
                "derived net parent must be exact MarketCommissionAttribution"
            )
        issued = _DERIVED_NET_ISSUED.get(id(self))
        if (
            issued is None
            or issued[0]() is not self
            or issued[1] != self._binding_fingerprint()
        ):
            raise BetfairCommissionAttributionError(
                "per-bet net was not issued by canonical attribution derivation"
            )
        policy = attribution.allocation_policy
        if (
            policy is None
            or self.attribution_id != attribution.attribution_id
            or self.allocation_policy_id != policy.policy_id
            or self.allocation_policy_version != policy.policy_version
            or self.allocation_policy_sha256 != policy.policy_sha256
            or self.venue_id != attribution.market_commission.venue_id
            or self.account_id != attribution.market_commission.account_id
            or self.market_id != attribution.market_commission.market_id
        ):
            raise BetfairCommissionAttributionError(
                "per-bet net is not bound to the supplied parent attribution"
            )
        parent_rows = {row.bet_id: row for row in attribution.gross_bets}
        parent_allocations = {
            row.bet_id: row for row in attribution.allocations
        }
        gross = parent_rows.get(self.bet_id)
        allocation = parent_allocations.get(self.bet_id)
        if (
            gross is None
            or allocation is None
            or self.gross_profit != gross.gross_profit
            or self.allocated_commission != allocation.allocated_commission
        ):
            raise BetfairCommissionAttributionError(
                "per-bet net economics do not match parent attribution"
            )


_DERIVED_NET_ISSUED: dict[int, tuple[object, str]] = {}


def _issue_bet_attributed_net(value: BetAttributedNet) -> BetAttributedNet:
    key = id(value)

    def forget(_weakref: object, *, issued_key: int = key) -> None:
        _DERIVED_NET_ISSUED.pop(issued_key, None)

    _DERIVED_NET_ISSUED[key] = (
        ref(value, forget),
        value._binding_fingerprint(),
    )
    return value


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
        venue_id = self.market_commission.venue_id
        account_id = self.market_commission.account_id
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
            if bet.venue_id != venue_id:
                raise BetfairCommissionAttributionError(
                    "gross bet and commission venue_id differ"
                )
            if bet.account_id != account_id:
                raise BetfairCommissionAttributionError(
                    "gross bet and commission account_id differ"
                )
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
        raise BetfairCommissionAttributionError(
            "complete market population authority is required before commission allocation"
        )

    @property
    def grants_provider_origin_authority(self) -> bool:
        return False

    @property
    def supplied_gross_profit(self) -> Decimal:
        """Exact sum of caller-supplied BET rows; not whole-market completeness."""

        return _exact_sum(tuple(item.gross_profit for item in self.gross_bets))

    @property
    def market_gross_profit(self) -> Decimal:
        raise BetfairCommissionAttributionError(
            "exact market gross requires complete market population authority"
        )

    @property
    def market_net_profit(self) -> Decimal:
        raise BetfairCommissionAttributionError(
            "exact market net requires complete market population authority"
        )

    def per_bet_net(self) -> tuple[BetAttributedNet, ...]:
        raise BetfairCommissionAttributionError(
            "per-bet net requires complete market population authority"
        )

    @property
    def attribution_id(self) -> str:
        payload: dict[str, object] = {
            "schema": "autosport.betfair_commission_attribution",
            "schema_version": 2,
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

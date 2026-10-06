"""Deterministic Betfair listMarketBook/listRunnerBook request-budget preflight.

This module is deliberately transport-free.  It does not issue provider requests,
authenticate, qualify market data, or grant execution authority.  It only models the
published Betfair market-data request-weight rule so an oversized request can fail
closed before transport.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from fractions import Fraction
import hashlib
import json
from typing import Sequence

MAX_REQUEST_POINTS = 200
MAX_BEST_PRICES_DEPTH = 10
DEFAULT_BEST_PRICES_DEPTH = 3

_PRICE_WEIGHTS: dict[str, int] = {
    "SP_AVAILABLE": 3,
    "SP_TRADED": 7,
    "EX_BEST_OFFERS": 5,
    "EX_ALL_OFFERS": 17,
    "EX_TRADED": 17,
}


class MarketBookBudgetError(ValueError):
    """Raised when request-budget evidence cannot be computed canonically."""


class MarketBookBudgetStatus(str, Enum):
    WITHIN_LIMIT = "WITHIN_LIMIT"
    TOO_MUCH_DATA_RISK = "TOO_MUCH_DATA_RISK"


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_tokens(values: Sequence[str], field: str) -> tuple[str, ...]:
    # This is a pre-dispatch authority boundary. Do not invoke arbitrary Sequence,
    # str-subclass iteration/comparison/strip hooks while deciding provider weight.
    if type(values) not in (list, tuple):
        raise MarketBookBudgetError(
            f"{field} must be an exact list or tuple sequence of strings"
        )
    normalized: list[str] = []
    for value in values:
        if type(value) is not str or not value:
            raise MarketBookBudgetError(
                f"{field} values must be non-empty exact strings"
            )
        if value != value.strip():
            raise MarketBookBudgetError(
                f"{field} values must not contain surrounding whitespace"
            )
        normalized.append(value)
    if len(normalized) != len(set(normalized)):
        raise MarketBookBudgetError(f"{field} contains duplicates")
    return tuple(sorted(normalized))


def _validate_request_inputs(
    market_ids: Sequence[str],
    price_data: Sequence[str],
    best_prices_depth: object,
    operation: object,
) -> tuple[tuple[str, ...], tuple[str, ...], int | None, str]:
    canonical_market_ids = _canonical_tokens(market_ids, "market_ids")
    if not canonical_market_ids:
        raise MarketBookBudgetError("market_ids must contain at least one market")

    if type(operation) is not str or operation not in {"listMarketBook", "listRunnerBook"}:
        raise MarketBookBudgetError("operation must be listMarketBook or listRunnerBook")
    if operation == "listRunnerBook" and len(canonical_market_ids) != 1:
        raise MarketBookBudgetError("listRunnerBook requires exactly one market_id")

    canonical_price_data = _canonical_tokens(price_data, "price_data")
    unknown = tuple(value for value in canonical_price_data if value not in _PRICE_WEIGHTS)
    if unknown:
        raise MarketBookBudgetError(
            "unsupported Betfair PriceData value(s): " + ", ".join(unknown)
        )

    depth = best_prices_depth
    if depth is not None:
        if type(depth) is not int:
            raise MarketBookBudgetError("best_prices_depth must be an exact integer")
        if not (1 <= depth <= MAX_BEST_PRICES_DEPTH):
            raise MarketBookBudgetError("best_prices_depth must be in 1..10")
        if "EX_BEST_OFFERS" not in canonical_price_data or "EX_ALL_OFFERS" in canonical_price_data:
            raise MarketBookBudgetError(
                "best_prices_depth requires effective EX_BEST_OFFERS without EX_ALL_OFFERS"
            )

    return canonical_market_ids, canonical_price_data, depth, operation


@dataclass(frozen=True, slots=True)
class _MarketBookBudgetCalculation:
    market_ids: tuple[str, ...]
    requested_price_data: tuple[str, ...]
    effective_price_data: tuple[str, ...]
    best_prices_depth: int | None
    operation: str
    weight_per_market: Fraction
    total_points: Fraction
    status: MarketBookBudgetStatus


@dataclass(frozen=True, slots=True)
class MarketBookRequestBudget:
    """Immutable evidence for one Betfair market-data request weight calculation.

    Every derived property revalidates one snapshot of the stored request fields.
    Frozen-dataclass construction is therefore not the only fail-closed boundary:
    constructor bypass, deserialization mistakes, or post-init tampering cannot turn
    malformed request state into positive budget evidence.
    """

    market_ids: tuple[str, ...]
    price_data: tuple[str, ...] = ()
    best_prices_depth: int | None = None
    operation: str = "listMarketBook"

    def __post_init__(self) -> None:
        market_ids, price_data, depth, operation = _validate_request_inputs(
            self.market_ids,
            self.price_data,
            self.best_prices_depth,
            self.operation,
        )
        object.__setattr__(self, "market_ids", market_ids)
        object.__setattr__(self, "price_data", price_data)
        object.__setattr__(self, "best_prices_depth", depth)
        object.__setattr__(self, "operation", operation)

    def _calculation(self) -> _MarketBookBudgetCalculation:
        market_ids, price_data, depth, operation = _validate_request_inputs(
            self.market_ids,
            self.price_data,
            self.best_prices_depth,
            self.operation,
        )

        if "EX_ALL_OFFERS" in price_data and "EX_BEST_OFFERS" in price_data:
            effective_price_data = tuple(
                value for value in price_data if value != "EX_BEST_OFFERS"
            )
        else:
            effective_price_data = price_data

        effective = set(effective_price_data)
        if not effective:
            weight = Fraction(2, 1)
        else:
            weight = Fraction(0, 1)
            exchange = effective & {"EX_BEST_OFFERS", "EX_ALL_OFFERS", "EX_TRADED"}
            if "EX_ALL_OFFERS" in exchange and "EX_TRADED" in exchange:
                weight += 32
                exchange -= {"EX_ALL_OFFERS", "EX_TRADED"}
            elif "EX_BEST_OFFERS" in exchange and "EX_TRADED" in exchange:
                weight += 20
                exchange -= {"EX_BEST_OFFERS", "EX_TRADED"}

            for item in exchange:
                weight += _PRICE_WEIGHTS[item]
            for item in effective & {"SP_AVAILABLE", "SP_TRADED"}:
                weight += _PRICE_WEIGHTS[item]

            if depth is not None:
                weight *= Fraction(depth, DEFAULT_BEST_PRICES_DEPTH)

        total_points = weight * len(market_ids)
        status = (
            MarketBookBudgetStatus.WITHIN_LIMIT
            if total_points <= MAX_REQUEST_POINTS
            else MarketBookBudgetStatus.TOO_MUCH_DATA_RISK
        )
        return _MarketBookBudgetCalculation(
            market_ids=market_ids,
            requested_price_data=price_data,
            effective_price_data=effective_price_data,
            best_prices_depth=depth,
            operation=operation,
            weight_per_market=weight,
            total_points=total_points,
            status=status,
        )

    @property
    def effective_price_data(self) -> tuple[str, ...]:
        return self._calculation().effective_price_data

    @property
    def weight_per_market(self) -> Fraction:
        return self._calculation().weight_per_market

    @property
    def total_points(self) -> Fraction:
        return self._calculation().total_points

    @property
    def status(self) -> MarketBookBudgetStatus:
        return self._calculation().status

    @property
    def allowed(self) -> bool:
        return self._calculation().status is MarketBookBudgetStatus.WITHIN_LIMIT

    @property
    def evidence_payload(self) -> dict[str, object]:
        calculation = self._calculation()
        weight = calculation.weight_per_market
        points = calculation.total_points
        return {
            "provider": "BETFAIR",
            "operation": calculation.operation,
            "market_ids": list(calculation.market_ids),
            "requested_price_data": list(calculation.requested_price_data),
            "effective_price_data": list(calculation.effective_price_data),
            "best_prices_depth": calculation.best_prices_depth,
            "weight_per_market": {
                "numerator": weight.numerator,
                "denominator": weight.denominator,
            },
            "market_count": len(calculation.market_ids),
            "total_points": {
                "numerator": points.numerator,
                "denominator": points.denominator,
            },
            "max_points": MAX_REQUEST_POINTS,
            "status": calculation.status.value,
            "allowed": calculation.status is MarketBookBudgetStatus.WITHIN_LIMIT,
            "execution_authorized": False,
        }

    @property
    def evidence_id(self) -> str:
        return hashlib.sha256(
            _canonical_json(self.evidence_payload).encode("utf-8")
        ).hexdigest()

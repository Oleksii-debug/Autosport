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
    if isinstance(values, (str, bytes)):
        raise MarketBookBudgetError(f"{field} must be a sequence of strings, not one string")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise MarketBookBudgetError(f"{field} values must be non-empty strings")
        if value != value.strip():
            raise MarketBookBudgetError(f"{field} values must not contain surrounding whitespace")
        normalized.append(value)
    if len(normalized) != len(set(normalized)):
        raise MarketBookBudgetError(f"{field} contains duplicates")
    return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True)
class MarketBookRequestBudget:
    """Immutable evidence for one Betfair market-data request weight calculation."""

    market_ids: tuple[str, ...]
    price_data: tuple[str, ...] = ()
    best_prices_depth: int | None = None
    operation: str = "listMarketBook"

    def __post_init__(self) -> None:
        market_ids = _canonical_tokens(self.market_ids, "market_ids")
        if not market_ids:
            raise MarketBookBudgetError("market_ids must contain at least one market")
        operation = self.operation
        if operation not in {"listMarketBook", "listRunnerBook"}:
            raise MarketBookBudgetError("operation must be listMarketBook or listRunnerBook")
        if operation == "listRunnerBook" and len(market_ids) != 1:
            raise MarketBookBudgetError("listRunnerBook requires exactly one market_id")
        price_data = _canonical_tokens(self.price_data, "price_data")
        unknown = tuple(value for value in price_data if value not in _PRICE_WEIGHTS)
        if unknown:
            raise MarketBookBudgetError("unsupported Betfair PriceData value(s): " + ", ".join(unknown))

        depth = self.best_prices_depth
        if depth is not None:
            if isinstance(depth, bool) or not isinstance(depth, int):
                raise MarketBookBudgetError("best_prices_depth must be an integer")
            if not (1 <= depth <= MAX_BEST_PRICES_DEPTH):
                raise MarketBookBudgetError("best_prices_depth must be in 1..10")
            # Betfair documents bestPricesDepth as applicable only to EX_BEST_OFFERS.
            # EX_ALL_OFFERS trumps EX_BEST_OFFERS, so a depth override would not have
            # the semantics represented by this budget if both were supplied.
            if "EX_BEST_OFFERS" not in price_data or "EX_ALL_OFFERS" in price_data:
                raise MarketBookBudgetError(
                    "best_prices_depth requires effective EX_BEST_OFFERS without EX_ALL_OFFERS"
                )

        object.__setattr__(self, "market_ids", market_ids)
        object.__setattr__(self, "price_data", price_data)

    @property
    def effective_price_data(self) -> tuple[str, ...]:
        # Official Betfair semantics: EX_ALL_OFFERS trumps EX_BEST_OFFERS.
        if "EX_ALL_OFFERS" in self.price_data and "EX_BEST_OFFERS" in self.price_data:
            return tuple(value for value in self.price_data if value != "EX_BEST_OFFERS")
        return self.price_data

    @property
    def weight_per_market(self) -> Fraction:
        effective = set(self.effective_price_data)
        if not effective:
            return Fraction(2, 1)  # documented Null / no PriceProjection weight

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

        if self.best_prices_depth is not None:
            # Betfair's request-limit contract scales the request weight by
            # requestedDepth/3 when exBestOffersOverrides is used.  Exact Fraction
            # arithmetic avoids boundary errors at 200 points.
            weight *= Fraction(self.best_prices_depth, DEFAULT_BEST_PRICES_DEPTH)
        return weight

    @property
    def total_points(self) -> Fraction:
        return self.weight_per_market * len(self.market_ids)

    @property
    def status(self) -> MarketBookBudgetStatus:
        if self.total_points <= MAX_REQUEST_POINTS:
            return MarketBookBudgetStatus.WITHIN_LIMIT
        return MarketBookBudgetStatus.TOO_MUCH_DATA_RISK

    @property
    def allowed(self) -> bool:
        return self.status is MarketBookBudgetStatus.WITHIN_LIMIT

    @property
    def evidence_payload(self) -> dict[str, object]:
        weight = self.weight_per_market
        points = self.total_points
        return {
            "provider": "BETFAIR",
            "operation": self.operation,
            "market_ids": list(self.market_ids),
            "requested_price_data": list(self.price_data),
            "effective_price_data": list(self.effective_price_data),
            "best_prices_depth": self.best_prices_depth,
            "weight_per_market": {"numerator": weight.numerator, "denominator": weight.denominator},
            "market_count": len(self.market_ids),
            "total_points": {"numerator": points.numerator, "denominator": points.denominator},
            "max_points": MAX_REQUEST_POINTS,
            "status": self.status.value,
            "allowed": self.allowed,
            "execution_authorized": False,
        }

    @property
    def evidence_id(self) -> str:
        return hashlib.sha256(_canonical_json(self.evidence_payload).encode("utf-8")).hexdigest()

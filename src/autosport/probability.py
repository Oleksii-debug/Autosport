from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .domain import MarketEvent


@dataclass(frozen=True, slots=True)
class ProbabilityEstimate:
    quote_key: str
    raw_implied: Decimal
    fair_probability: Decimal
    overround: Decimal


@dataclass(frozen=True, slots=True)
class PaperValueEstimate:
    quote_key: str
    probability: Decimal
    decimal_odds: Decimal
    expected_profit_per_unit: Decimal


def implied_probability(decimal_odds: Decimal | str) -> Decimal:
    odds = Decimal(str(decimal_odds))
    if odds <= 1:
        raise ValueError("decimal odds must be greater than 1")
    return Decimal("1") / odds


def normalize_two_or_more_way_market(quotes: Iterable[MarketEvent]) -> dict[str, ProbabilityEstimate]:
    values = list(quotes)
    if len(values) < 2:
        raise ValueError("normalization requires at least two selections")
    event_market = {(quote.event_id, quote.market_id) for quote in values}
    if len(event_market) != 1:
        raise ValueError("all quotes must belong to one event/market")
    raw = {quote.quote_key: implied_probability(quote.decimal_odds) for quote in values}
    total = sum(raw.values(), Decimal("0"))
    if total <= 0:
        raise ValueError("invalid implied probability total")
    return {
        key: ProbabilityEstimate(key, probability, probability / total, total)
        for key, probability in raw.items()
    }


def paper_value(quote_key: str, probability: Decimal | str, decimal_odds: Decimal | str) -> PaperValueEstimate:
    p = Decimal(str(probability))
    odds = Decimal(str(decimal_odds))
    if p < 0 or p > 1:
        raise ValueError("probability must be between 0 and 1")
    if odds <= 1:
        raise ValueError("decimal odds must be greater than 1")
    return PaperValueEstimate(quote_key, p, odds, p * odds - Decimal("1"))


@dataclass(frozen=True, slots=True)
class ForecastObservation:
    probability: float
    outcome: int


def brier_score(observations: Iterable[ForecastObservation]) -> float:
    values = list(observations)
    if not values:
        raise ValueError("observations required")
    for item in values:
        if not 0.0 <= item.probability <= 1.0 or item.outcome not in (0, 1):
            raise ValueError("invalid forecast observation")
    return sum((item.probability - item.outcome) ** 2 for item in values) / len(values)


def log_loss(observations: Iterable[ForecastObservation], epsilon: float = 1e-15) -> float:
    values = list(observations)
    if not values:
        raise ValueError("observations required")
    losses = []
    for item in values:
        if not 0.0 <= item.probability <= 1.0 or item.outcome not in (0, 1):
            raise ValueError("invalid forecast observation")
        p = min(1.0 - epsilon, max(epsilon, item.probability))
        losses.append(-(item.outcome * math.log(p) + (1 - item.outcome) * math.log(1 - p)))
    return sum(losses) / len(losses)

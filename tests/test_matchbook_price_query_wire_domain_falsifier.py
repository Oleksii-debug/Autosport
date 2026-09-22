from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.matchbook_price_query_contract import (
    MatchbookExchangeType,
    MatchbookOddsType,
    MatchbookPriceMode,
    MatchbookPriceQueryContract,
    MatchbookPriceQueryError,
    MatchbookPriceSide,
)


def _query(**changes: object) -> MatchbookPriceQueryContract:
    values: dict[str, object] = {
        "event_id": 101,
        "market_id": 202,
        "runner_id": 303,
        "exchange_type": MatchbookExchangeType.BACK_LAY,
        "odds_type": MatchbookOddsType.DECIMAL,
        "currency": "EUR",
        "side": MatchbookPriceSide.BOTH,
        "depth": 5,
        "price_mode": MatchbookPriceMode.EXPANDED,
        "minimum_liquidity": Decimal("2"),
        "exclude_mirrored_prices": True,
    }
    values.update(changes)
    return MatchbookPriceQueryContract(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("event_id", 1 << 63),
        ("market_id", 1 << 63),
        ("runner_id", 1 << 63),
    ),
)
def test_path_ids_cannot_exceed_documented_int64_domain(
    field: str,
    value: int,
) -> None:
    with pytest.raises(MatchbookPriceQueryError):
        _query(**{field: value})


def test_depth_cannot_exceed_documented_int32_domain() -> None:
    with pytest.raises(MatchbookPriceQueryError):
        _query(depth=1 << 31)


def test_minimum_liquidity_cannot_exceed_documented_double_domain() -> None:
    # Matchbook documents minimum-liquidity as a provider double.  A Decimal
    # above the maximum finite binary64 magnitude must not mint a canonical
    # request identity for an unrepresentable provider value.
    with pytest.raises(MatchbookPriceQueryError):
        _query(minimum_liquidity=Decimal("1e309"))

from decimal import Decimal

import pytest

import autosport.exchange_exposure as exposure
from autosport.exchange_exposure import locked_capital_for_exchange_side


def test_back_locked_capital_equals_stake() -> None:
    assert locked_capital_for_exchange_side(
        stake=Decimal("10"),
        odds=Decimal("3.00"),
        exchange_side="BACK",
    ) == Decimal("10")


def test_lay_locked_capital_is_stake_times_odds_minus_one() -> None:
    assert locked_capital_for_exchange_side(
        stake=Decimal("10"),
        odds=Decimal("3.00"),
        exchange_side="LAY",
    ) == Decimal("20")


@pytest.mark.parametrize(
    ("stake", "odds", "side"),
    [
        (Decimal("0"), Decimal("2.0"), "LAY"),
        (Decimal("10"), Decimal("1.0"), "LAY"),
        (Decimal("NaN"), Decimal("2.0"), "LAY"),
        (Decimal("10"), Decimal("Infinity"), "LAY"),
        (Decimal("10"), Decimal("2.0"), "UNKNOWN"),
    ],
)
def test_exchange_capital_rejects_non_canonical_resources(
    stake: Decimal,
    odds: Decimal,
    side: str,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        locked_capital_for_exchange_side(
            stake=stake,
            odds=odds,
            exchange_side=side,
        )


def test_exchange_capital_rejects_helper_code_mutation() -> None:
    helper = exposure._subtract_exact
    original_code = helper.__code__

    def hostile(*_args, **_kwargs):
        raise AssertionError("mutated exchange arithmetic helper executed")

    try:
        helper.__code__ = hostile.__code__
        with pytest.raises(
            ValueError,
            match="exchange exposure arithmetic authority drifted",
        ):
            locked_capital_for_exchange_side(
                stake=Decimal("10"),
                odds=Decimal("3.00"),
                exchange_side="LAY",
            )
    finally:
        helper.__code__ = original_code


def test_exchange_capital_rejects_calculator_code_mutation() -> None:
    calculator = exposure.locked_capital_for_exchange_side
    original_code = calculator.__code__

    def hostile(*_args, **_kwargs):
        raise AssertionError("mutated locked-capital calculator executed")

    try:
        calculator.__code__ = hostile.__code__
        with pytest.raises(AssertionError, match="mutated locked-capital calculator"):
            calculator(
                stake=Decimal("10"),
                odds=Decimal("3.00"),
                exchange_side="LAY",
            )
    finally:
        calculator.__code__ = original_code

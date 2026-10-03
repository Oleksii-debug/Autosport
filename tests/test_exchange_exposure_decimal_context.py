from decimal import Decimal, localcontext

from autosport.exchange_exposure import locked_capital_for_exchange_side


def test_lay_liability_is_independent_of_ambient_decimal_precision() -> None:
    stake = Decimal("123456789.123456789")
    odds = Decimal("9.87654321987654321")
    expected = Decimal("1095869524.44154852447203169112635269")

    with localcontext() as context:
        context.prec = 6
        actual = locked_capital_for_exchange_side(
            stake=stake,
            odds=odds,
            exchange_side="LAY",
        )

    assert actual == expected

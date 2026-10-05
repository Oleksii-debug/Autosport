from decimal import Decimal, localcontext

import pytest

import autosport.exchange_exposure as exchange_exposure
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


@pytest.mark.parametrize(
    "exchange_side",
    ("lay", " Lay ", "BACK ", " back", "LAY\t", "BACK\n"),
)
def test_exposure_boundary_rejects_noncanonical_exchange_side(
    exchange_side: str,
) -> None:
    with pytest.raises(
        ValueError,
        match="exact canonical BACK or LAY",
    ):
        locked_capital_for_exchange_side(
            stake=Decimal("10"),
            odds=Decimal("5"),
            exchange_side=exchange_side,
        )


def test_back_locked_capital_requires_exact_canonical_side() -> None:
    assert locked_capital_for_exchange_side(
        stake=Decimal("10"),
        odds=Decimal("5"),
        exchange_side="BACK",
    ) == Decimal("10")


def test_lay_liability_ignores_module_helper_rebinding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        exchange_exposure,
        "_multiply_exact",
        lambda left, right: Decimal("0"),
    )

    assert locked_capital_for_exchange_side(
        stake=Decimal("10"),
        odds=Decimal("5"),
        exchange_side="LAY",
    ) == Decimal("40")


def test_lay_liability_rejects_captured_helper_code_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forged_multiply(left: Decimal, right: Decimal) -> Decimal:
        del left, right
        return Decimal("0")

    monkeypatch.setattr(
        exchange_exposure._multiply_exact,
        "__code__",
        forged_multiply.__code__,
    )

    with pytest.raises(ValueError, match="arithmetic authority drifted"):
        locked_capital_for_exchange_side(
            stake=Decimal("10"),
            odds=Decimal("5"),
            exchange_side="LAY",
        )

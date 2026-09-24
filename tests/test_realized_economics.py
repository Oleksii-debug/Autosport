from decimal import Decimal

import pytest

from autosport.realized_economics import summarize_realized_economics


def test_realized_path_reports_growth_turnover_and_drawdown_without_authority():
    report = summarize_realized_economics(
        starting_bankroll=Decimal("1000"),
        realized_pnls=(
            Decimal("100"), Decimal("-250"), Decimal("50"), Decimal("200"),
        ),
        stakes=(
            Decimal("100"), Decimal("150"), Decimal("100"), Decimal("200"),
        ),
    )

    assert report.ending_bankroll == Decimal("1100")
    assert report.net_pnl == Decimal("100")
    assert report.gross_turnover == Decimal("550")
    assert report.return_on_start == Decimal("0.1")
    assert report.return_on_turnover == Decimal("100") / Decimal("550")
    assert report.peak_bankroll == Decimal("1100")
    assert report.max_drawdown_amount == Decimal("250")
    assert report.max_drawdown_fraction == Decimal("250") / Decimal("1100")
    assert report.observation_count == 4
    assert report.profitability_proven is False
    assert report.promotion_authority is False
    assert report.real_money_authority is False


def test_zero_turnover_has_no_turnover_return():
    report = summarize_realized_economics(
        starting_bankroll=Decimal("500"), realized_pnls=(), stakes=()
    )
    assert report.ending_bankroll == Decimal("500")
    assert report.return_on_turnover is None
    assert report.max_drawdown_amount == Decimal("0")


@pytest.mark.parametrize(
    ("pnls", "stakes"),
    [
        ((Decimal("1"),), ()),
        ((Decimal("NaN"),), (Decimal("1"),)),
        ((Decimal("1"),), (Decimal("-1"),)),
    ],
)
def test_invalid_observations_fail_closed(pnls, stakes):
    with pytest.raises(ValueError):
        summarize_realized_economics(
            starting_bankroll=Decimal("100"), realized_pnls=pnls, stakes=stakes
        )


def test_negative_bankroll_path_is_rejected():
    with pytest.raises(ValueError, match="negative bankroll"):
        summarize_realized_economics(
            starting_bankroll=Decimal("100"),
            realized_pnls=(Decimal("-101"),),
            stakes=(Decimal("100"),),
        )

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


def _finite_decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field} must be a finite Decimal")
    return value


@dataclass(frozen=True, slots=True)
class RealizedEconomicReport:
    """Deterministic realized-path economics with no forecast/promotion authority."""

    starting_bankroll: Decimal
    ending_bankroll: Decimal
    net_pnl: Decimal
    gross_turnover: Decimal
    return_on_start: Decimal
    return_on_turnover: Decimal | None
    peak_bankroll: Decimal
    max_drawdown_amount: Decimal
    max_drawdown_fraction: Decimal
    observation_count: int

    @property
    def profitability_proven(self) -> bool:
        return False

    @property
    def promotion_authority(self) -> bool:
        return False

    @property
    def real_money_authority(self) -> bool:
        return False


def summarize_realized_economics(
    *,
    starting_bankroll: Decimal,
    realized_pnls: tuple[Decimal, ...],
    stakes: tuple[Decimal, ...],
) -> RealizedEconomicReport:
    """Summarize one realized economic path without statistical extrapolation.

    The two observation tuples must be positionally aligned. The function computes
    arithmetic truths of the supplied realized path only; it does not claim
    independence, expected value, persistence, promotion, or execution authority.
    """

    start = _finite_decimal(starting_bankroll, "starting_bankroll")
    if start <= 0:
        raise ValueError("starting_bankroll must be positive")
    if type(realized_pnls) is not tuple or type(stakes) is not tuple:
        raise ValueError("realized_pnls and stakes must be exact tuples")
    if len(realized_pnls) != len(stakes):
        raise ValueError("realized_pnls and stakes must have equal length")

    balance = start
    peak = start
    max_drawdown = Decimal("0")
    max_drawdown_peak = start
    turnover = Decimal("0")

    for index, (pnl_raw, stake_raw) in enumerate(zip(realized_pnls, stakes)):
        pnl = _finite_decimal(pnl_raw, f"realized_pnls[{index}]")
        stake = _finite_decimal(stake_raw, f"stakes[{index}]")
        if stake < 0:
            raise ValueError(f"stakes[{index}] must be non-negative")

        turnover += stake
        balance += pnl
        if balance < 0:
            raise ValueError("realized economic path cannot produce negative bankroll")
        if balance > peak:
            peak = balance
        drawdown = peak - balance
        if drawdown > max_drawdown:
            max_drawdown = drawdown
            max_drawdown_peak = peak

    net_pnl = balance - start
    return_on_start = net_pnl / start
    return_on_turnover = None if turnover == 0 else net_pnl / turnover
    max_drawdown_fraction = max_drawdown / max_drawdown_peak

    return RealizedEconomicReport(
        starting_bankroll=start,
        ending_bankroll=balance,
        net_pnl=net_pnl,
        gross_turnover=turnover,
        return_on_start=return_on_start,
        return_on_turnover=return_on_turnover,
        peak_bankroll=peak,
        max_drawdown_amount=max_drawdown,
        max_drawdown_fraction=max_drawdown_fraction,
        observation_count=len(realized_pnls),
    )


__all__ = ["RealizedEconomicReport", "summarize_realized_economics"]

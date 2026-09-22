from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.real_execution_ledger import ExecutionAction
from autosport.supervised_execution import (
    ExecutionLegConstraint,
    SupervisedExecutionError,
    _validate_slippage,
)


ACTION_ID = "a" * 64
OBSERVED_AT = "2026-09-22T10:00:00+00:00"
EXPIRES_AT = "2026-09-22T10:01:00+00:00"


def _back_action() -> ExecutionAction:
    return ExecutionAction(
        action_id=ACTION_ID,
        bookmaker_id="provider-1",
        account_id="account-1",
        event_id="event-1",
        market_id="market-1",
        selection_id="selection-1",
        side="BACK",
        requested_odds=Decimal("2.00"),
        requested_stake=Decimal("10"),
        quote_id="quote-1",
        quote_observed_at=OBSERVED_AT,
        expires_at=EXPIRES_AT,
    )


def _constraint() -> ExecutionLegConstraint:
    return ExecutionLegConstraint(
        leg_id=ACTION_ID,
        side="BACK",
        quote_expires_at=EXPIRES_AT,
        max_slippage_fraction=Decimal("0.01"),
    )


def test_accepted_odds_guard_never_uses_two_decimal_display_alias() -> None:
    action = _back_action()
    constraint = _constraint()
    exact_floor = Decimal("1.98")
    below_floor = Decimal("1.9799")
    above_floor = Decimal("1.9801")

    assert action.requested_odds * (
        Decimal("1") - constraint.max_slippage_fraction
    ) == exact_floor

    # These economically distinct provider prices are visually identical after
    # common two-decimal presentation. A display-rounded comparator would admit
    # the below-floor value and silently widen owner-approved slippage.
    assert format(below_floor, ".2f") == "1.98"
    assert format(above_floor, ".2f") == "1.98"
    assert Decimal(format(below_floor, ".2f")) >= exact_floor

    with pytest.raises(
        SupervisedExecutionError,
        match="accepted BACK odds exceed approved slippage",
    ):
        _validate_slippage(action, constraint, below_floor)

    _validate_slippage(action, constraint, above_floor)


def test_accepted_odds_guard_keeps_exact_floor_inclusive() -> None:
    action = _back_action()
    constraint = _constraint()

    _validate_slippage(action, constraint, Decimal("1.9800"))

    with pytest.raises(
        SupervisedExecutionError,
        match="accepted BACK odds exceed approved slippage",
    ):
        _validate_slippage(action, constraint, Decimal("1.979999999999999999"))

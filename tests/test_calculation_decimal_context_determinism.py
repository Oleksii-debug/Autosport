from __future__ import annotations

import decimal
from decimal import (
    DivisionByZero,
    Inexact,
    InvalidOperation,
    Overflow,
    ROUND_DOWN,
    ROUND_HALF_EVEN,
    ROUND_UP,
    Underflow,
)

import autosport.calculation as calculation


_EXPECTED_TRAPS = {InvalidOperation, DivisionByZero, Overflow, Underflow}


def _restore_default_context(snapshot: decimal.Context) -> None:
    target = decimal.DefaultContext
    target.prec = snapshot.prec
    target.rounding = snapshot.rounding
    target.Emin = snapshot.Emin
    target.Emax = snapshot.Emax
    target.capitals = snapshot.capitals
    target.clamp = snapshot.clamp
    for signal, enabled in snapshot.flags.items():
        target.flags[signal] = enabled
    for signal, enabled in snapshot.traps.items():
        target.traps[signal] = enabled


def _result_snapshot(context: decimal.Context) -> tuple[tuple[tuple[tuple[str, str], ...], str], ...]:
    previous = calculation._CONTEXT
    try:
        calculation._CONTEXT = context
        engine = calculation.CalculationEngine()
        results = (
            engine.implied_probability("3"),
            engine.american_to_decimal_odds("-150"),
            engine.multiplicative_devig({"a": "2.4", "b": "3.7", "c": "4.1"}),
        )
        return tuple((result.outputs, result.result_hash) for result in results)
    finally:
        calculation._CONTEXT = previous


def test_private_decimal_context_ignores_mutable_default_context() -> None:
    original = decimal.DefaultContext.copy()
    try:
        decimal.DefaultContext.prec = 7
        decimal.DefaultContext.rounding = ROUND_UP
        decimal.DefaultContext.Emin = -3
        decimal.DefaultContext.Emax = 3
        decimal.DefaultContext.capitals = 0
        decimal.DefaultContext.clamp = 1
        decimal.DefaultContext.traps[Inexact] = True
        decimal.DefaultContext.traps[DivisionByZero] = False
        decimal.DefaultContext.traps[InvalidOperation] = False
        decimal.DefaultContext.traps[Overflow] = False
        decimal.DefaultContext.traps[Underflow] = False
        first = calculation._build_decimal_context()

        decimal.DefaultContext.prec = 9
        decimal.DefaultContext.rounding = ROUND_DOWN
        decimal.DefaultContext.Emin = -7
        decimal.DefaultContext.Emax = 7
        decimal.DefaultContext.capitals = 0
        decimal.DefaultContext.clamp = 1
        decimal.DefaultContext.traps[Inexact] = True
        decimal.DefaultContext.traps[DivisionByZero] = False
        decimal.DefaultContext.traps[InvalidOperation] = False
        decimal.DefaultContext.traps[Overflow] = False
        decimal.DefaultContext.traps[Underflow] = False
        second = calculation._build_decimal_context()
    finally:
        _restore_default_context(original)

    for context in (first, second):
        assert context.prec == 160
        assert context.rounding == ROUND_HALF_EVEN
        assert context.Emin == -999
        assert context.Emax == 999
        assert context.capitals == 1
        assert context.clamp == 0
        assert not any(context.flags.values())
        assert {signal for signal, enabled in context.traps.items() if enabled} == _EXPECTED_TRAPS

    assert _result_snapshot(first) == _result_snapshot(second)

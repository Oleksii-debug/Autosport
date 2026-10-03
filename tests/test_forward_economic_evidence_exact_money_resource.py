from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

import autosport.forward_economic_evidence as evidence
from autosport.forward_economic_evidence import (
    BetSide,
    ForwardEconomicEvidenceError,
    ResolvedPolicyOutcome,
    _decimal_text,
    _exact_decimal_sum,
)


@pytest.mark.parametrize("large_value", ("1E+6000", "-1E+6000"))
def test_exact_money_extreme_exponent_gap_fails_closed_before_expansion(
    large_value: str,
) -> None:
    """Compact finite Decimals must not escape as raw bigint/string resource errors."""

    with pytest.raises(ForwardEconomicEvidenceError):
        _exact_decimal_sum(Decimal(large_value), Decimal("-0.1"))


def test_exact_money_resource_guard_preserves_small_loss_after_huge_gain() -> None:
    """The resource bound must retain the already-required exact-money regression."""

    result = _exact_decimal_sum(Decimal("1E+100"), Decimal("-1E-50"))
    expected = Decimal((0, (9,) * 150, -50))

    assert result == expected


@pytest.mark.parametrize("zero", ("0E-1000000", "-0E-1000000", "0E+1000000"))
def test_exact_money_zero_scale_does_not_create_artificial_exponent_gap(
    zero: str,
) -> None:
    assert _exact_decimal_sum(Decimal("1.25"), Decimal(zero)) == Decimal("1.25")


def test_exact_money_oversized_significand_fails_closed() -> None:
    oversized = Decimal("1" * 513)

    with pytest.raises(ForwardEconomicEvidenceError):
        _exact_decimal_sum(oversized, Decimal("0"))


@pytest.mark.parametrize(
    "value",
    ("1E+6000", "-1E+6000", "1E-6000", "-1E-6000"),
)
def test_decimal_text_extreme_scale_fails_closed_before_fixed_point_expansion(
    value: str,
) -> None:
    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="canonical decimal text exceeds resource bound",
    ):
        _decimal_text(Decimal(value))


@pytest.mark.parametrize(
    "zero",
    ("0E-1000000", "-0E-1000000", "0E+1000000"),
)
def test_decimal_text_zero_short_circuits_extreme_scale(zero: str) -> None:
    assert _decimal_text(Decimal(zero)) == "0"


def test_decimal_text_resource_guard_preserves_existing_canonical_values() -> None:
    assert _decimal_text(Decimal("1E+100")) == "1" + ("0" * 100)
    assert _decimal_text(Decimal("1E-50")) == "0." + ("0" * 49) + "1"
    assert _decimal_text(Decimal("1.2300")) == "1.23"
    assert _decimal_text(Decimal("123.00")) == "123"


T0 = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def _guard_fraction_against_oversized_decimal(
    monkeypatch: pytest.MonkeyPatch,
) -> list[bool]:
    real_fraction = evidence.Fraction
    oversized_seen = [False]

    def guarded_fraction(value: object):
        if type(value) is Decimal:
            exponent = value.as_tuple().exponent
            if type(exponent) is int and abs(exponent) > 512:
                oversized_seen[0] = True
                raise RuntimeError("oversized Decimal reached Fraction materialization")
        return real_fraction(value)

    monkeypatch.setattr(evidence, "Fraction", guarded_fraction)
    return oversized_seen


def test_resolved_cost_money_is_bounded_before_fraction_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized_seen = _guard_fraction_against_oversized_decimal(monkeypatch)

    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="canonical decimal text exceeds resource bound",
    ):
        ResolvedPolicyOutcome(
            policy_id="challenger",
            sequence=0,
            universe_event_sha256="a" * 64,
            decision_sha256="b" * 64,
            decision_committed_at=T0,
            side=BetSide.NONE,
            accepted_odds=None,
            accepted_stake=None,
            net_pnl_currency=Decimal("-1E-6000"),
            execution_evidence_sha256=None,
            execution_accepted_at=None,
            settlement_evidence_sha256=None,
            settlement_available_at=None,
            economic_cost_currency=Decimal("1E-6000"),
            economic_cost_evidence_sha256="c" * 64,
            economic_cost_incurred_at=T0,
            economic_cost_available_at=T0 + timedelta(seconds=1),
        )

    assert oversized_seen == [False]


def test_resolved_explicit_wager_money_is_bounded_before_fraction_materialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    oversized_seen = _guard_fraction_against_oversized_decimal(monkeypatch)

    with pytest.raises(
        ForwardEconomicEvidenceError,
        match="canonical decimal text exceeds resource bound",
    ):
        ResolvedPolicyOutcome(
            policy_id="challenger",
            sequence=0,
            universe_event_sha256="a" * 64,
            decision_sha256="b" * 64,
            decision_committed_at=T0,
            side=BetSide.BACK,
            accepted_odds=Decimal("2"),
            accepted_stake=Decimal("1"),
            net_pnl_currency=Decimal("0"),
            execution_evidence_sha256="d" * 64,
            execution_accepted_at=T0 + timedelta(seconds=1),
            settlement_evidence_sha256="e" * 64,
            settlement_available_at=T0 + timedelta(seconds=2),
            wager_pnl_currency=Decimal("1E-6000"),
        )

    assert oversized_seen == [False]

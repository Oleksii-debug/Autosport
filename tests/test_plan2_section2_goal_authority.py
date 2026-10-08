"""Plan 2 Section 2: an untrusted scalar is not owner financial authority."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from autosport.economic_goal import (
    EconomicGoalContract,
    EconomicGoalContractError,
    validate_automatic_transition,
)


def _goal(**changes: object) -> EconomicGoalContract:
    values: dict[str, object] = dict(
        goal_id="owner-eur",
        revision=1,
        bankroll_id="paper-bank",
        currency="EUR",
        max_stake_fraction=Decimal("0.02"),
        max_session_loss_fraction=Decimal("0.05"),
        max_turnover_fraction=Decimal("1.50"),
        max_drawdown_fraction=Decimal("0.10"),
        max_risk_of_ruin=Decimal("0.01"),
    )
    values.update(changes)
    return EconomicGoalContract(**values)  # type: ignore[arg-type]


class UntrustedDecimal(Decimal):
    def is_finite(self):
        raise AssertionError("subclass virtual monetary method executed")


class UntrustedText(str):
    def strip(self, *args):
        raise AssertionError("subclass virtual identity method executed")


class UntrustedInt(int):
    def __lt__(self, other):
        raise AssertionError("subclass virtual revision method executed")


class UntrustedRestrictions(frozenset):
    def __iter__(self):
        raise AssertionError("subclass virtual permissions method executed")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_stake_fraction", UntrustedDecimal("0.01")),
        ("max_turnover_fraction", UntrustedDecimal("0.50")),
        ("max_risk_of_ruin", UntrustedDecimal("0.001")),
        ("goal_id", UntrustedText("owner-eur")),
        ("currency", UntrustedText("EUR")),
        ("revision", UntrustedInt(1)),
        ("max_concurrent_positions", UntrustedInt(1)),
        ("blocked_providers", UntrustedRestrictions(("provider-a",))),
    ],
)
def test_owner_authority_rejects_subclasses_before_virtual_dispatch(field, value):
    with pytest.raises(EconomicGoalContractError):
        _goal(**{field: value})


def test_goal_contract_subclass_is_not_owner_authority():
    class DerivedGoal(EconomicGoalContract):
        pass

    with pytest.raises(EconomicGoalContractError, match="exact contract type"):
        DerivedGoal(goal_id="owner-eur", revision=1, bankroll_id="paper-bank", currency="EUR")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_stake_fraction", float("0.2")),
        ("max_risk_of_ruin", UntrustedDecimal("0.0")),
        ("currency", UntrustedText("USD")),
        ("blocked_markets", UntrustedRestrictions()),
        ("automation_level", 3),
    ],
)
def test_postconstruction_corruption_cannot_be_laundered_through_transition(field, value):
    existing = _goal()
    candidate = replace(existing, revision=2, max_stake_fraction=Decimal("0.01"))
    object.__setattr__(candidate, field, value)
    with pytest.raises(EconomicGoalContractError):
        validate_automatic_transition(existing, candidate)


def test_valid_tightening_is_exact_and_restarts_from_same_durable_fields():
    old = _goal()
    changed = replace(
        old, revision=2, max_stake_fraction=Decimal("0.01"),
        max_turnover_fraction=Decimal("1.25"), max_risk_of_ruin=Decimal("0.005"),
        blocked_providers=frozenset({"provider-a"}),
    )
    validate_automatic_transition(old, changed)
    # Simulates a roundtrip through the typed canonical persisted snapshot.
    recovered = replace(changed)
    validate_automatic_transition(old, recovered)
    assert recovered.max_stake_fraction == Decimal("0.01")
    assert recovered.max_turnover_fraction == Decimal("1.25")
    assert recovered.max_risk_of_ruin == Decimal("0.005")


def test_zero_risk_is_valid_but_owner_expansion_and_stop_clear_are_denied():
    old = _goal(max_stake_fraction=Decimal("0"), max_risk_of_ruin=Decimal("0"), emergency_stop=True)
    tightened = replace(old, revision=2)
    validate_automatic_transition(old, tightened)
    for attempted in (
        replace(old, revision=2, max_stake_fraction=Decimal("0.0001")),
        replace(old, revision=2, max_risk_of_ruin=Decimal("0.0001")),
        replace(old, revision=2, emergency_stop=False),
    ):
        with pytest.raises(EconomicGoalContractError):
            validate_automatic_transition(old, attempted)

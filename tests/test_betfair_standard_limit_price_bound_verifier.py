from __future__ import annotations

from dataclasses import fields
from decimal import Decimal

import pytest

from autosport.betfair_standard_limit_price_bound import (
    BetfairStandardLimitPriceBoundError,
    BetfairStandardLimitPriceBoundEvidence,
)
from autosport.betfair_standard_limit_price_bound_verifier import (
    verify_betfair_standard_limit_price_bound,
)

from test_betfair_standard_limit_price_bound import _evidence


def _forge_clone(
    evidence: BetfairStandardLimitPriceBoundEvidence,
) -> BetfairStandardLimitPriceBoundEvidence:
    forged = object.__new__(BetfairStandardLimitPriceBoundEvidence)
    for field in fields(BetfairStandardLimitPriceBoundEvidence):
        object.__setattr__(forged, field.name, getattr(evidence, field.name))
    return forged


def test_object_new_forge_with_changed_price_is_not_accepted() -> None:
    bound, action, evidence = _evidence()
    forged = _forge_clone(evidence)
    object.__setattr__(forged, "price_floor_odds", Decimal("1.99"))

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="does not match fresh canonical re-resolution",
    ):
        verify_betfair_standard_limit_price_bound(
            evidence=forged,
            bound=bound,
            action_id=action.action_id,
        )


def test_object_new_forge_with_changed_plan_identity_is_not_accepted() -> None:
    bound, action, evidence = _evidence()
    forged = _forge_clone(evidence)
    object.__setattr__(forged, "execution_plan_id", "attacker-plan")

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="does not match fresh canonical re-resolution",
    ):
        verify_betfair_standard_limit_price_bound(
            evidence=forged,
            bound=bound,
            action_id=action.action_id,
        )


def test_post_issuance_object_setattr_tamper_is_not_accepted() -> None:
    bound, action, evidence = _evidence()
    object.__setattr__(evidence, "requested_stake", Decimal("999.00"))

    with pytest.raises(
        BetfairStandardLimitPriceBoundError,
        match="does not match fresh canonical re-resolution",
    ):
        verify_betfair_standard_limit_price_bound(
            evidence=evidence,
            bound=bound,
            action_id=action.action_id,
        )


def test_exact_forged_clone_is_replaced_by_fresh_canonical_record() -> None:
    bound, action, evidence = _evidence()
    forged = _forge_clone(evidence)

    verified = verify_betfair_standard_limit_price_bound(
        evidence=forged,
        bound=bound,
        action_id=action.action_id,
    )

    assert verified is not forged
    assert verified.action_id == action.action_id
    assert verified.price_floor_odds == action.requested_odds
    assert verified.requested_stake == action.requested_stake
    assert verified.execution_feasibility_proven is False
    assert verified.realized_price_exact is False

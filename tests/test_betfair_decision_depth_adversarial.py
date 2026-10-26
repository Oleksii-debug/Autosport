from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, localcontext

import pytest

from autosport.betfair_decision_depth import (
    BetfairDecisionDepthError,
    BetfairDecisionDepthSnapshot,
    BetfairDepthLevel,
    issue_betfair_decision_depth_snapshot,
)


OBSERVED_AT = datetime(2026, 9, 21, 13, 15, 0, tzinfo=timezone.utc)


def _market_book() -> dict[str, object]:
    return {
        "marketId": "1.24681012",
        "status": "OPEN",
        "inplay": False,
        "betDelay": 0,
        "runners": [
            {
                "selectionId": 42,
                "handicap": 0.0,
                "status": "ACTIVE",
                "ex": {
                    "availableToBack": [{"price": "2.04", "size": "3.25"}],
                    "availableToLay": [{"price": "2.06", "size": "4.5"}],
                },
            }
        ],
    }


def test_depth_level_constructor_normalizes_supported_numeric_inputs_to_decimal() -> None:
    level = BetfairDepthLevel(price="2.040", size=3)

    assert level.price == Decimal("2.040")
    assert type(level.price) is Decimal
    assert level.size == Decimal("3")
    assert type(level.size) is Decimal
    assert level.to_dict() == {"price": "2.04", "size": "3"}


def test_boolean_runner_selection_id_cannot_alias_integer_selection() -> None:
    book = _market_book()
    book["runners"][0]["selectionId"] = True

    with pytest.raises(
        BetfairDecisionDepthError, match="selectionId must be a positive integer"
    ):
        issue_betfair_decision_depth_snapshot(
            book,
            market_id="1.24681012",
            selection_id=1,
            handicap=Decimal("0"),
            side="BACK",
            observed_at=OBSERVED_AT,
        )



def test_hard_false_authority_claims_cannot_be_rebound_or_instance_shadowed() -> None:
    snapshot = issue_betfair_decision_depth_snapshot(
        _market_book(),
        market_id="1.24681012",
        selection_id=42,
        handicap=Decimal("0"),
        side="BACK",
        observed_at=OBSERVED_AT,
    )
    names = (
        "provider_snapshot_origin_proven",
        "observation_time_proven",
        "request_projection_proven",
        "virtual_prices_included_proven",
        "full_ladder_proven",
        "provider_acceptance_proven",
        "fill_proven",
        "accepted_odds_proven",
    )
    for name in names:
        descriptor = BetfairDecisionDepthSnapshot.__dict__[name]
        assert isinstance(descriptor, property)
        assert descriptor.fget is not None
        assert not hasattr(descriptor.fget, "__code__")
        assert getattr(snapshot, name) is False
        with pytest.raises(AttributeError):
            object.__setattr__(snapshot, name, True)

    with pytest.raises(TypeError, match="authority surface is sealed"):
        BetfairDecisionDepthSnapshot._provider_acceptance_proven_constant = True

    with pytest.raises(TypeError, match="authority surface is sealed"):
        BetfairDecisionDepthSnapshot.provider_acceptance_proven = property(
            lambda _self: True
        )


def test_returned_size_and_marginal_price_ignore_ambient_decimal_precision() -> None:
    book = _market_book()
    book["runners"][0]["ex"]["availableToBack"] = [
        {"price": "2.04", "size": "1.26"},
        {"price": "2.02", "size": "1.26"},
    ]
    snapshot = issue_betfair_decision_depth_snapshot(
        book,
        market_id="1.24681012",
        selection_id=42,
        handicap=Decimal("0"),
        side="BACK",
        observed_at=OBSERVED_AT,
    )

    with localcontext() as context:
        context.prec = 2
        # Ordinary Decimal accumulation rounds 1.26 + 1.26 to 2.6 at this
        # precision, which would incorrectly claim that 2.55 is covered.
        assert snapshot.returned_size_at_or_better(Decimal("2.02")) == Decimal("2.52")
        assert not snapshot.returned_capacity_covers(
            Decimal("2.55"), Decimal("2.02")
        )
        assert snapshot.worst_returned_price_for_size(
            Decimal("2.55"), Decimal("2.02")
        ) is None

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from autosport.betfair_decision_depth import (
    BetfairDecisionDepthError,
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
            side="BACK",
            observed_at=OBSERVED_AT,
        )

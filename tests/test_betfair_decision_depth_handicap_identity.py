from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from autosport.betfair_decision_depth import (
    BetfairDecisionDepthError,
    issue_betfair_decision_depth_snapshot,
)


OBSERVED_AT = datetime(2026, 9, 21, 14, 15, 0, tzinfo=timezone.utc)


def _runner(handicap: object, *, price: str = "2.04") -> dict[str, object]:
    return {
        "selectionId": 42,
        "handicap": handicap,
        "status": "ACTIVE",
        "ex": {
            "availableToBack": [{"price": price, "size": "10.00"}],
            "availableToLay": [{"price": "2.06", "size": "10.00"}],
        },
    }


def _market_book(*runners: dict[str, object]) -> dict[str, object]:
    return {
        "marketId": "1.24681012",
        "status": "OPEN",
        "isMarketDataDelayed": False,
        "inplay": False,
        "betDelay": 0,
        "runners": list(runners),
    }


def _snapshot(book: dict[str, object], *, handicap: Decimal):
    return issue_betfair_decision_depth_snapshot(
        book,
        market_id="1.24681012",
        selection_id=42,
        handicap=handicap,
        side="BACK",
        observed_at=OBSERVED_AT,
    )


def test_same_selection_id_is_resolved_by_exact_handicap() -> None:
    snapshot = _snapshot(
        _market_book(
            _runner("-1.5", price="1.90"),
            _runner("1.5", price="2.04"),
        ),
        handicap=Decimal("1.5"),
    )

    assert snapshot.handicap == Decimal("1.5")
    assert [level.price for level in snapshot.levels] == [Decimal("2.04")]
    assert snapshot.to_dict()["handicap"] == "1.5"


def test_wrong_handicap_single_runner_fails_closed() -> None:
    with pytest.raises(BetfairDecisionDepthError):
        _snapshot(
            _market_book(_runner("-1.5")),
            handicap=Decimal("1.5"),
        )


def test_handicap_is_content_bound_into_evidence_identity() -> None:
    positive = _snapshot(
        _market_book(_runner("1.5")),
        handicap=Decimal("1.5"),
    )
    negative = _snapshot(
        _market_book(_runner("-1.5")),
        handicap=Decimal("-1.5"),
    )

    # All other serialized decision inputs deliberately coincide. Distinct Betfair
    # handicap lines must still remain distinct durable evidence identities.
    assert positive.market_id == negative.market_id
    assert positive.selection_id == negative.selection_id
    assert positive.side == negative.side
    assert positive.observed_at == negative.observed_at
    assert positive.levels == negative.levels
    assert positive.evidence_id != negative.evidence_id

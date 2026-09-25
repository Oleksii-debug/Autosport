from __future__ import annotations

from decimal import Decimal

import pytest

from autosport.betfair_account_readonly import (
    BetfairCurrentOrderObservation,
    BetfairEvidence,
    BetfairReadOnlyError,
)


EVIDENCE = BetfairEvidence(
    observed_at="2026-09-23T08:25:00+00:00",
    source_payload_sha256="a" * 64,
)


def _order(
    *,
    status: str,
    size_matched: str,
    size_remaining: str,
) -> BetfairCurrentOrderObservation:
    matched = Decimal(size_matched)
    return BetfairCurrentOrderObservation(
        bet_id="bet-status-remaining",
        market_id="1.234",
        selection_id=42,
        side="BACK",
        status=status,
        placed_date="2026-09-23T08:24:00+00:00",
        price=Decimal("2"),
        requested_size=Decimal("10"),
        average_price_matched=Decimal("2") if matched > 0 else Decimal("0"),
        size_matched=matched,
        size_remaining=Decimal(size_remaining),
        customer_order_ref="b" * 32,
        customer_strategy_ref=None,
        evidence=EVIDENCE,
    )


@pytest.mark.parametrize(
    ("status", "size_matched", "size_remaining"),
    (
        ("EXECUTABLE", "1", "9"),
        ("EXECUTION_COMPLETE", "10", "0"),
    ),
)
def test_provider_consistent_current_order_status_remaining_pairs_are_admitted(
    status: str,
    size_matched: str,
    size_remaining: str,
) -> None:
    observation = _order(
        status=status,
        size_matched=size_matched,
        size_remaining=size_remaining,
    )

    assert observation.status == status
    assert observation.size_remaining == Decimal(size_remaining)


@pytest.mark.parametrize(
    ("status", "size_matched", "size_remaining", "message"),
    (
        (
            "EXECUTABLE",
            "10",
            "0",
            "EXECUTABLE current order must retain positive size_remaining",
        ),
        (
            "EXECUTION_COMPLETE",
            "9",
            "1",
            "EXECUTION_COMPLETE current order must have zero size_remaining",
        ),
    ),
)
def test_provider_contradictory_current_order_status_remaining_pairs_fail_closed(
    status: str,
    size_matched: str,
    size_remaining: str,
    message: str,
) -> None:
    with pytest.raises(BetfairReadOnlyError, match=message):
        _order(
            status=status,
            size_matched=size_matched,
            size_remaining=size_remaining,
        )

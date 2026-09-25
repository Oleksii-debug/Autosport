from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from autosport.betfair_decision_depth import (
    BetfairDecisionDepthError,
    BetfairDecisionDepthStatus,
    BetfairOrderSide,
    issue_betfair_decision_depth_snapshot,
)


OBSERVED_AT = datetime(2026, 9, 21, 13, 15, 0, tzinfo=timezone.utc)


def _market_book() -> dict[str, object]:
    return {
        "marketId": "1.24681012",
        "status": "OPEN",
        "isMarketDataDelayed": False,
        "inplay": False,
        "betDelay": 0,
        "runners": [
            {
                "selectionId": 42,
                "status": "ACTIVE",
                "ex": {
                    "availableToBack": [
                        {"price": 2.04, "size": 3.25},
                        {"price": "2.02", "size": "7.10"},
                        {"price": 2.0, "size": 100},
                    ],
                    "availableToLay": [
                        {"price": 2.06, "size": 4.5},
                        {"price": "2.08", "size": "9.75"},
                        {"price": 2.1, "size": 100},
                    ],
                },
            }
        ],
    }


def test_back_snapshot_uses_exact_decimal_threshold_and_best_to_worst_order() -> None:
    book = _market_book()
    # Deliberately scramble provider order: evidence ordering remains deterministic.
    book["runners"][0]["ex"]["availableToBack"] = [
        {"price": "2.00", "size": "100.00"},
        {"price": "2.04", "size": "3.25"},
        {"price": "2.02", "size": "7.10"},
    ]

    snapshot = issue_betfair_decision_depth_snapshot(
        book,
        market_id="1.24681012",
        selection_id=42,
        side="BACK",
        observed_at=OBSERVED_AT,
    )

    assert snapshot.side is BetfairOrderSide.BACK
    assert [level.price for level in snapshot.levels] == [
        Decimal("2.04"),
        Decimal("2.02"),
        Decimal("2.00"),
    ]
    assert snapshot.returned_size_at_or_better(Decimal("2.02")) == Decimal("10.35")
    assert snapshot.returned_capacity_covers(Decimal("10.35"), Decimal("2.02"))
    assert not snapshot.returned_capacity_covers(Decimal("10.36"), Decimal("2.02"))
    assert snapshot.worst_returned_price_for_size(
        Decimal("10.35"), Decimal("2.02")
    ) == Decimal("2.02")
    assert snapshot.worst_returned_price_for_size(
        Decimal("10.36"), Decimal("2.02")
    ) is None


def test_lay_snapshot_uses_lower_price_as_better() -> None:
    snapshot = issue_betfair_decision_depth_snapshot(
        _market_book(),
        market_id="1.24681012",
        selection_id=42,
        side=BetfairOrderSide.LAY,
        observed_at=OBSERVED_AT,
    )

    assert [level.price for level in snapshot.levels] == [
        Decimal("2.06"),
        Decimal("2.08"),
        Decimal("2.1"),
    ]
    assert snapshot.returned_size_at_or_better(Decimal("2.08")) == Decimal("14.25")
    assert snapshot.worst_returned_price_for_size(
        Decimal("14.25"), Decimal("2.08")
    ) == Decimal("2.08")


def test_evidence_is_explicitly_non_authorizing_and_stable() -> None:
    first = issue_betfair_decision_depth_snapshot(
        _market_book(),
        market_id="1.24681012",
        selection_id=42,
        side="BACK",
        observed_at=OBSERVED_AT,
    )
    second = issue_betfair_decision_depth_snapshot(
        _market_book(),
        market_id="1.24681012",
        selection_id=42,
        side="BACK",
        observed_at=OBSERVED_AT,
    )

    assert first.status is BetfairDecisionDepthStatus.PARSED_RETURNED_BEST_OFFERS
    assert first.provider_snapshot_origin_proven is False
    assert first.observation_time_proven is False
    assert first.request_projection_proven is False
    assert first.virtual_prices_included_proven is False
    assert first.full_ladder_proven is False
    assert first.provider_acceptance_proven is False
    assert first.fill_proven is False
    assert first.accepted_odds_proven is False
    assert first.evidence_id == second.evidence_id
    payload = first.to_dict()
    assert payload["evidence_id"] == first.evidence_id
    assert payload["observed_at"] == "2026-09-21T13:15:00Z"
    assert payload["levels"][0] == {"price": "2.04", "size": "3.25"}


def test_empty_present_ladder_is_valid_zero_displayed_depth() -> None:
    book = _market_book()
    book["runners"][0]["ex"]["availableToBack"] = []

    snapshot = issue_betfair_decision_depth_snapshot(
        book,
        market_id="1.24681012",
        selection_id=42,
        side="BACK",
        observed_at=OBSERVED_AT,
    )

    assert snapshot.levels == ()
    assert snapshot.returned_size_at_or_better(Decimal("2")) == Decimal("0")
    assert not snapshot.returned_capacity_covers(Decimal("0.01"), Decimal("2"))


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda book: book.update(marketId="1.WRONG"), "does not match"),
        (lambda book: book.update(status="SUSPENDED"), "status must be OPEN"),
        (
            lambda book: book["runners"][0].update(status="REMOVED"),
            "runner.status must be ACTIVE",
        ),
        (lambda book: book.update(runners=[]), "exactly once"),
        (
            lambda book: book["runners"].append(book["runners"][0].copy()),
            "exactly once",
        ),
        (
            lambda book: book["runners"][0]["ex"].pop("availableToBack"),
            "availableToBack is required",
        ),
        (
            lambda book: book["runners"][0]["ex"]["availableToBack"].__setitem__(
                0, {"price": 2.04, "size": 0}
            ),
            "finite positive number",
        ),
        (
            lambda book: book["runners"][0]["ex"]["availableToBack"].append(
                {"price": 2.04, "size": 1}
            ),
            "duplicate price",
        ),
    ],
)
def test_malformed_or_nontradable_market_book_fails_closed(mutate, match: str) -> None:
    book = _market_book()
    mutate(book)

    with pytest.raises(BetfairDecisionDepthError, match=match):
        issue_betfair_decision_depth_snapshot(
            book,
            market_id="1.24681012",
            selection_id=42,
            side="BACK",
            observed_at=OBSERVED_AT,
        )


@pytest.mark.parametrize("side", ["back", "", "BUY", 1, None])
def test_unsupported_side_fails_closed(side: object) -> None:
    with pytest.raises(BetfairDecisionDepthError, match="side must be BACK or LAY"):
        issue_betfair_decision_depth_snapshot(
            _market_book(),
            market_id="1.24681012",
            selection_id=42,
            side=side,
            observed_at=OBSERVED_AT,
        )


def test_naive_observation_time_fails_closed() -> None:
    with pytest.raises(BetfairDecisionDepthError, match="timezone-aware"):
        issue_betfair_decision_depth_snapshot(
            _market_book(),
            market_id="1.24681012",
            selection_id=42,
            side="BACK",
            observed_at=datetime(2026, 9, 21, 13, 15, 0),
        )


def test_positive_authority_flags_cannot_be_minted_by_constructor() -> None:
    snapshot = issue_betfair_decision_depth_snapshot(
        _market_book(),
        market_id="1.24681012",
        selection_id=42,
        side="BACK",
        observed_at=OBSERVED_AT,
    )
    snapshot_type = type(snapshot)
    kwargs = {
        "market_id": snapshot.market_id,
        "selection_id": snapshot.selection_id,
        "side": snapshot.side,
        "observed_at": snapshot.observed_at,
        "market_status": snapshot.market_status,
        "runner_status": snapshot.runner_status,
        "market_data_delayed": snapshot.market_data_delayed,
        "inplay": snapshot.inplay,
        "bet_delay_seconds": snapshot.bet_delay_seconds,
        "levels": snapshot.levels,
    }
    for forbidden in (
        "provider_snapshot_origin_proven",
        "observation_time_proven",
        "request_projection_proven",
        "virtual_prices_included_proven",
        "full_ladder_proven",
        "provider_acceptance_proven",
        "fill_proven",
        "accepted_odds_proven",
    ):
        with pytest.raises(TypeError):
            snapshot_type(**kwargs, **{forbidden: True})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price", True),
        ("size", False),
        ("price", "NaN"),
        ("price", "Infinity"),
        ("size", "-Infinity"),
    ],
)
def test_nonfinite_and_boolean_ladder_values_fail_closed(field: str, value: object) -> None:
    book = _market_book()
    book["runners"][0]["ex"]["availableToBack"][0][field] = value

    with pytest.raises(BetfairDecisionDepthError, match="finite positive number"):
        issue_betfair_decision_depth_snapshot(
            book,
            market_id="1.24681012",
            selection_id=42,
            side="BACK",
            observed_at=OBSERVED_AT,
        )


def test_boolean_selection_id_fails_closed() -> None:
    with pytest.raises(BetfairDecisionDepthError, match="positive integer"):
        issue_betfair_decision_depth_snapshot(
            _market_book(),
            market_id="1.24681012",
            selection_id=True,
            side="BACK",
            observed_at=OBSERVED_AT,
        )


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda book: book.update(inplay="false"), "inplay must be bool"),
        (lambda book: book.update(betDelay=-1), "betDelay must be a non-negative integer"),
        (lambda book: book.update(betDelay=True), "betDelay must be a non-negative integer"),
    ],
)
def test_market_metadata_types_fail_closed(mutate, match: str) -> None:
    book = _market_book()
    mutate(book)

    with pytest.raises(BetfairDecisionDepthError, match=match):
        issue_betfair_decision_depth_snapshot(
            book,
            market_id="1.24681012",
            selection_id=42,
            side="BACK",
            observed_at=OBSERVED_AT,
        )


def test_delayed_market_data_is_preserved_not_upgraded_to_live_truth() -> None:
    book = _market_book()
    book["isMarketDataDelayed"] = True

    snapshot = issue_betfair_decision_depth_snapshot(
        book,
        market_id="1.24681012",
        selection_id=42,
        side="BACK",
        observed_at=OBSERVED_AT,
    )

    assert snapshot.market_data_delayed is True
    assert snapshot.provider_snapshot_origin_proven is False
    assert snapshot.observation_time_proven is False


def test_missing_market_data_delay_marker_stays_unknown_not_false() -> None:
    book = _market_book()
    book.pop("isMarketDataDelayed")

    snapshot = issue_betfair_decision_depth_snapshot(
        book,
        market_id="1.24681012",
        selection_id=42,
        side="BACK",
        observed_at=OBSERVED_AT,
    )

    assert snapshot.market_data_delayed is None


@pytest.mark.parametrize("value", [0, 1, "false", "true"])
def test_market_data_delay_marker_must_be_boolean_when_present(value: object) -> None:
    book = _market_book()
    book["isMarketDataDelayed"] = value

    with pytest.raises(
        BetfairDecisionDepthError, match="isMarketDataDelayed must be bool when present"
    ):
        issue_betfair_decision_depth_snapshot(
            book,
            market_id="1.24681012",
            selection_id=42,
            side="BACK",
            observed_at=OBSERVED_AT,
        )

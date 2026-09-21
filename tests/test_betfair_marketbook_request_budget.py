from __future__ import annotations

from decimal import Decimal
from fractions import Fraction

import pytest

from autosport.betfair_marketbook_request_budget import (
    MAX_REQUEST_WEIGHT,
    MarketBookProjection,
    MarketBookRequestBudgetError,
    MarketBookTarget,
    plan_market_book_reads,
)


def _targets(count: int, status: str = "OPEN") -> tuple[MarketBookTarget, ...]:
    return tuple(
        MarketBookTarget(f"1.{index:09d}", status)
        for index in range(1, count + 1)
    )


@pytest.mark.parametrize(
    ("price_data", "expected_weight", "expected_max"),
    [
        ((), Fraction(2), 100),
        (("SP_AVAILABLE",), Fraction(3), 66),
        (("SP_TRADED",), Fraction(7), 28),
        (("EX_BEST_OFFERS",), Fraction(5), 40),
        (("EX_ALL_OFFERS",), Fraction(17), 11),
        (("EX_TRADED",), Fraction(17), 11),
        (("EX_BEST_OFFERS", "EX_TRADED"), Fraction(20), 10),
        (("EX_ALL_OFFERS", "EX_TRADED"), Fraction(32), 6),
    ],
)
def test_documented_weights_are_exact_and_bound_batch_size(
    price_data: tuple[str, ...],
    expected_weight: Fraction,
    expected_max: int,
) -> None:
    projection = MarketBookProjection(price_data=price_data)

    assert projection.request_weight == expected_weight
    assert projection.max_markets_per_request == expected_max
    assert expected_weight * expected_max <= MAX_REQUEST_WEIGHT
    assert expected_weight * (expected_max + 1) > MAX_REQUEST_WEIGHT


def test_ex_all_offers_11_fit_and_12_split_without_drop() -> None:
    projection = MarketBookProjection(price_data=("EX_ALL_OFFERS",))

    eleven = plan_market_book_reads(_targets(11), projection)
    twelve = plan_market_book_reads(_targets(12), projection)

    assert [batch.total_weight for batch in eleven.batches] == [Fraction(187)]
    assert [len(batch.market_ids) for batch in twelve.batches] == [11, 1]
    assert tuple(
        market_id
        for batch in twelve.batches
        for market_id in batch.market_ids
    ) == twelve.market_ids


def test_ex_all_offers_plus_traded_6_fit_and_7_split() -> None:
    projection = MarketBookProjection(
        price_data=("EX_ALL_OFFERS", "EX_TRADED")
    )

    six = plan_market_book_reads(_targets(6), projection)
    seven = plan_market_book_reads(_targets(7), projection)

    assert six.batches[0].total_weight == Fraction(192)
    assert [len(batch.market_ids) for batch in seven.batches] == [6, 1]


def test_best_offers_40_fit_and_41_split() -> None:
    projection = MarketBookProjection(price_data=("EX_BEST_OFFERS",))

    plan = plan_market_book_reads(_targets(41), projection)

    assert [len(batch.market_ids) for batch in plan.batches] == [40, 1]
    assert plan.batches[0].total_weight == Fraction(200)


def test_best_offer_depth_override_recomputes_exact_fractional_weight() -> None:
    default = MarketBookProjection(price_data=("EX_BEST_OFFERS",))
    depth_ten = MarketBookProjection(
        price_data=("EX_BEST_OFFERS",),
        best_prices_depth=10,
    )

    assert default.request_weight == Fraction(5)
    assert depth_ten.request_weight == Fraction(50, 3)
    assert depth_ten.max_markets_per_request == 12
    assert depth_ten.request_weight * 12 == Fraction(200)


def test_override_identity_binds_rollup_virtualise_and_order_semantics() -> None:
    first = plan_market_book_reads(
        _targets(3),
        MarketBookProjection(
            price_data=("EX_BEST_OFFERS",),
            best_prices_depth=2,
            rollup_model="STAKE",
            rollup_limit=Decimal("20"),
            virtualise=False,
            order_projection="EXECUTABLE",
            match_projection="ROLLED_UP_BY_AVG_PRICE",
        ),
    )
    changed = plan_market_book_reads(
        _targets(3),
        MarketBookProjection(
            price_data=("EX_BEST_OFFERS",),
            best_prices_depth=2,
            rollup_model="STAKE",
            rollup_limit=Decimal("21"),
            virtualise=False,
            order_projection="EXECUTABLE",
            match_projection="ROLLED_UP_BY_AVG_PRICE",
        ),
    )

    assert first.plan_id != changed.plan_id
    assert first.batches[0].batch_id != changed.batches[0].batch_id


@pytest.mark.parametrize(
    "price_data",
    [
        ("EX_BEST_OFFERS", "SP_AVAILABLE"),
        ("SP_AVAILABLE", "EX_BEST_OFFERS"),
        ("EX_BEST_OFFERS", "EX_ALL_OFFERS"),
        ("EX_TRADED", "EX_BEST_OFFERS"),
        ("UNKNOWN",),
    ],
)
def test_unsupported_or_noncanonical_projection_fails_closed(
    price_data: tuple[str, ...],
) -> None:
    with pytest.raises(MarketBookRequestBudgetError):
        MarketBookProjection(price_data=price_data)


def test_duplicate_market_id_is_rejected_not_polled_twice() -> None:
    duplicate = (
        MarketBookTarget("1.100", "OPEN"),
        MarketBookTarget("1.100", "OPEN"),
    )

    with pytest.raises(MarketBookRequestBudgetError, match="duplicate market_id"):
        plan_market_book_reads(
            duplicate,
            MarketBookProjection(price_data=("EX_BEST_OFFERS",)),
        )


def test_open_and_closed_intents_are_never_mixed_in_one_batch() -> None:
    targets = (
        MarketBookTarget("1.1", "OPEN"),
        MarketBookTarget("1.2", "OPEN"),
        MarketBookTarget("1.3", "CLOSED"),
        MarketBookTarget("1.4", "CLOSED"),
        MarketBookTarget("1.5", "OPEN"),
    )

    plan = plan_market_book_reads(
        targets,
        MarketBookProjection(price_data=("EX_BEST_OFFERS",)),
    )

    assert [(batch.status_intent, batch.market_ids) for batch in plan.batches] == [
        ("OPEN", ("1.1", "1.2")),
        ("CLOSED", ("1.3", "1.4")),
        ("OPEN", ("1.5",)),
    ]
    assert all(
        all(
            next(
                target.status_intent
                for target in targets
                if target.market_id == market_id
            )
            == batch.status_intent
            for market_id in batch.market_ids
        )
        for batch in plan.batches
    )


def test_same_exact_plan_is_restart_deterministic() -> None:
    targets = _targets(41)
    projection = MarketBookProjection(
        price_data=("EX_BEST_OFFERS",),
        best_prices_depth=3,
        virtualise=True,
    )

    first = plan_market_book_reads(targets, projection)
    replay = plan_market_book_reads(targets, projection)

    assert first == replay
    assert first.plan_id == replay.plan_id
    assert tuple(batch.batch_id for batch in first.batches) == tuple(
        batch.batch_id for batch in replay.batches
    )


def test_market_order_is_bound_and_never_silently_canonicalized() -> None:
    projection = MarketBookProjection(price_data=("EX_BEST_OFFERS",))
    a = plan_market_book_reads(
        (
            MarketBookTarget("1.1", "OPEN"),
            MarketBookTarget("1.2", "OPEN"),
        ),
        projection,
    )
    b = plan_market_book_reads(
        (
            MarketBookTarget("1.2", "OPEN"),
            MarketBookTarget("1.1", "OPEN"),
        ),
        projection,
    )

    assert a.plan_id != b.plan_id
    assert a.market_ids == ("1.1", "1.2")
    assert b.market_ids == ("1.2", "1.1")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"best_prices_depth": 0},
        {"best_prices_depth": 11},
        {"best_prices_depth": True},
        {"rollup_model": "STAKE"},
        {"rollup_limit": Decimal("1")},
        {"virtualise": "false"},
        {"match_projection": "NO_ROLLUP"},
    ],
)
def test_invalid_projection_override_contract_fails_closed(kwargs: dict[str, object]) -> None:
    with pytest.raises(MarketBookRequestBudgetError):
        MarketBookProjection(price_data=("EX_BEST_OFFERS",), **kwargs)

from fractions import Fraction
import itertools

import pytest

from autosport.betfair_marketbook_request_budget import (
    MarketBookBudgetError,
    MarketBookBudgetStatus,
    MarketBookRequestBudget,
)


def mids(count: int) -> tuple[str, ...]:
    return tuple(f"1.{index:09d}" for index in range(count))


def budget(count: int, price_data=(), *, depth=None) -> MarketBookRequestBudget:
    return MarketBookRequestBudget(mids(count), tuple(price_data), depth)


def test_null_projection_exact_200_point_boundary():
    exact = budget(100)
    assert exact.weight_per_market == 2
    assert exact.total_points == 200
    assert exact.status is MarketBookBudgetStatus.WITHIN_LIMIT
    assert budget(101).status is MarketBookBudgetStatus.TOO_MUCH_DATA_RISK


def test_documented_single_projection_weights_and_boundary():
    cases = {
        "SP_AVAILABLE": (3, 66, 67),
        "SP_TRADED": (7, 28, 29),
        "EX_BEST_OFFERS": (5, 40, 41),
        "EX_ALL_OFFERS": (17, 11, 12),
        "EX_TRADED": (17, 11, 12),
    }
    for projection, (weight, passing, failing) in cases.items():
        assert budget(1, (projection,)).weight_per_market == weight
        assert budget(passing, (projection,)).allowed is True
        assert budget(failing, (projection,)).allowed is False


def test_documented_exchange_combination_weights_are_not_naive_sum():
    best_traded = budget(10, ("EX_BEST_OFFERS", "EX_TRADED"))
    assert best_traded.weight_per_market == 20
    assert best_traded.total_points == 200
    assert budget(11, ("EX_BEST_OFFERS", "EX_TRADED")).allowed is False

    all_traded = budget(6, ("EX_ALL_OFFERS", "EX_TRADED"))
    assert all_traded.weight_per_market == 32
    assert all_traded.total_points == 192
    assert budget(7, ("EX_ALL_OFFERS", "EX_TRADED")).allowed is False


def test_sp_weights_add_to_special_exchange_combination():
    request = budget(5, ("EX_ALL_OFFERS", "EX_TRADED", "SP_AVAILABLE", "SP_TRADED"))
    assert request.weight_per_market == 42
    assert request.total_points == 210
    assert request.allowed is False


def test_all_offers_trumps_best_offers_for_effective_budget():
    both = budget(11, ("EX_BEST_OFFERS", "EX_ALL_OFFERS"))
    all_only = budget(11, ("EX_ALL_OFFERS",))
    assert both.effective_price_data == ("EX_ALL_OFFERS",)
    assert both.weight_per_market == all_only.weight_per_market == 17
    assert both.total_points == all_only.total_points


def test_best_prices_depth_scales_weight_exactly_without_float_rounding():
    depth_6 = budget(20, ("EX_BEST_OFFERS",), depth=6)
    assert depth_6.weight_per_market == 10
    assert depth_6.total_points == 200

    depth_10 = budget(12, ("EX_BEST_OFFERS",), depth=10)
    assert depth_10.weight_per_market == Fraction(50, 3)
    assert depth_10.total_points == 200
    assert budget(13, ("EX_BEST_OFFERS",), depth=10).allowed is False


def test_depth_multiplier_applies_after_documented_best_plus_traded_combination():
    request = budget(5, ("EX_BEST_OFFERS", "EX_TRADED"), depth=6)
    assert request.weight_per_market == 40
    assert request.total_points == 200
    assert budget(6, ("EX_BEST_OFFERS", "EX_TRADED"), depth=6).allowed is False


def test_depth_must_be_canonical_and_effective_best_offers():
    for bad in (0, 11, -1):
        with pytest.raises(MarketBookBudgetError, match="1..10"):
            budget(1, ("EX_BEST_OFFERS",), depth=bad)
    with pytest.raises(MarketBookBudgetError, match="integer"):
        budget(1, ("EX_BEST_OFFERS",), depth=True)
    with pytest.raises(MarketBookBudgetError, match="requires effective EX_BEST_OFFERS"):
        budget(1, (), depth=3)
    with pytest.raises(MarketBookBudgetError, match="requires effective EX_BEST_OFFERS"):
        budget(1, ("EX_BEST_OFFERS", "EX_ALL_OFFERS"), depth=3)


def test_unknown_noncanonical_or_duplicate_price_data_fails_closed():
    with pytest.raises(MarketBookBudgetError, match="unsupported"):
        budget(1, ("EX_FAKE",))
    with pytest.raises(MarketBookBudgetError, match="unsupported"):
        budget(1, ("ex_best_offers",))
    with pytest.raises(MarketBookBudgetError, match="duplicates"):
        budget(1, ("EX_TRADED", "EX_TRADED"))
    with pytest.raises(MarketBookBudgetError, match="sequence"):
        MarketBookRequestBudget(("1.1",), "EX_TRADED")


def test_market_ids_are_nonempty_unique_canonical_tokens():
    with pytest.raises(MarketBookBudgetError, match="at least one"):
        MarketBookRequestBudget(())
    with pytest.raises(MarketBookBudgetError, match="duplicates"):
        MarketBookRequestBudget(("1.1", "1.1"))
    with pytest.raises(MarketBookBudgetError, match="surrounding whitespace"):
        MarketBookRequestBudget((" 1.1",))
    with pytest.raises(MarketBookBudgetError, match="sequence"):
        MarketBookRequestBudget("1.1")


def test_identity_is_order_independent_but_sensitive_to_budget_semantics():
    first = MarketBookRequestBudget(("1.2", "1.1"), ("EX_TRADED", "SP_AVAILABLE"))
    second = MarketBookRequestBudget(("1.1", "1.2"), ("SP_AVAILABLE", "EX_TRADED"))
    assert first.evidence_id == second.evidence_id
    assert first.evidence_payload == second.evidence_payload

    deeper = MarketBookRequestBudget(("1.1", "1.2"), ("EX_BEST_OFFERS",), 6)
    shallower = MarketBookRequestBudget(("1.1", "1.2"), ("EX_BEST_OFFERS",), 3)
    assert deeper.evidence_id != shallower.evidence_id


def test_operation_is_exact_and_list_runner_book_is_single_market_only():
    market_book = MarketBookRequestBudget(("1.1",), ("EX_BEST_OFFERS",), operation="listMarketBook")
    runner_book = MarketBookRequestBudget(("1.1",), ("EX_BEST_OFFERS",), operation="listRunnerBook")
    assert market_book.evidence_payload["operation"] == "listMarketBook"
    assert runner_book.evidence_payload["operation"] == "listRunnerBook"
    assert market_book.evidence_id != runner_book.evidence_id
    with pytest.raises(MarketBookBudgetError, match="exactly one market_id"):
        MarketBookRequestBudget(("1.1", "1.2"), operation="listRunnerBook")
    with pytest.raises(MarketBookBudgetError, match="operation must"):
        MarketBookRequestBudget(("1.1",), operation="listMarketCatalogue")


def test_budget_evidence_never_grants_execution_authority():
    request = budget(1, ("EX_BEST_OFFERS",))
    assert request.allowed is True
    assert request.evidence_payload["execution_authorized"] is False


@pytest.mark.parametrize(
    "price_data",
    [
        (),
        ("SP_AVAILABLE",),
        ("SP_TRADED",),
        ("EX_BEST_OFFERS",),
        ("EX_ALL_OFFERS",),
        ("EX_TRADED",),
        ("EX_BEST_OFFERS", "EX_TRADED"),
        ("EX_ALL_OFFERS", "EX_TRADED"),
        ("SP_AVAILABLE", "EX_BEST_OFFERS", "EX_TRADED"),
    ],
)
def test_status_matches_exact_fraction_boundary_for_many_market_counts(price_data):
    for count in range(1, 125):
        request = budget(count, price_data)
        assert request.allowed is (request.total_points <= 200)


def test_every_permutation_has_one_deterministic_evidence_identity():
    projections = ("SP_AVAILABLE", "EX_BEST_OFFERS", "EX_TRADED")
    ids = {
        MarketBookRequestBudget(("1.3", "1.1", "1.2"), tuple(order)).evidence_id
        for order in itertools.permutations(projections)
    }
    assert len(ids) == 1

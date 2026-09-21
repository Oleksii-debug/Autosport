from decimal import Decimal

import pytest

from autosport.provider_liquidity_rebalancing import (
    LiquidityReviewState,
    ProviderLiquidityAccount,
    review_provider_liquidity,
)


D = Decimal


def account(provider_id: str, available: str, target: str) -> ProviderLiquidityAccount:
    return ProviderLiquidityAccount(provider_id, D(available), D(target))


def test_balanced_accounts_need_no_rebalancing_authority() -> None:
    review = review_provider_liquidity(
        central_cash=D("25"),
        protected_reserve=D("10"),
        accounts=[account("beta", "40", "30"), account("alpha", "20", "20")],
    )

    assert review.state is LiquidityReviewState.BALANCED
    assert [item.provider_id for item in review.accounts] == ["alpha", "beta"]
    assert review.total_provider_need == D("0")
    assert review.modeled_provider_surplus == D("10")
    assert review.requires_transfer_feasibility_check is False
    assert review.authorizes_transfer is False
    assert review.authorizes_execution is False


def test_central_cash_can_cover_provider_need_without_spending_reserve() -> None:
    review = review_provider_liquidity(
        central_cash=D("50"),
        protected_reserve=D("20"),
        accounts=[account("book-a", "5", "30")],
    )

    assert review.state is LiquidityReviewState.CENTRAL_CASH_COVERS
    assert review.central_deployable_cash == D("30")
    assert review.total_provider_need == D("25")
    assert review.need_after_central_cash == D("0")
    assert review.modeled_shortfall_after_surplus == D("0")
    assert review.requires_transfer_feasibility_check is False


def test_provider_surplus_is_only_rebalance_candidate_not_transfer_authority() -> None:
    review = review_provider_liquidity(
        central_cash=D("10"),
        protected_reserve=D("10"),
        accounts=[
            account("exchange-a", "80", "30"),
            account("book-b", "10", "55"),
        ],
    )

    assert review.state is LiquidityReviewState.PROVIDER_REBALANCE_CANDIDATE
    assert review.total_provider_need == D("45")
    assert review.modeled_provider_surplus == D("50")
    assert review.need_after_central_cash == D("45")
    assert review.modeled_shortfall_after_surplus == D("0")
    assert review.requires_transfer_feasibility_check is True
    assert review.authorizes_transfer is False
    assert review.authorizes_execution is False


def test_global_shortfall_stays_explicit_even_after_modeled_surplus() -> None:
    review = review_provider_liquidity(
        central_cash=D("15"),
        protected_reserve=D("5"),
        accounts=[
            account("book-a", "10", "50"),
            account("book-b", "35", "20"),
        ],
    )

    assert review.state is LiquidityReviewState.GLOBAL_SHORTFALL
    assert review.central_deployable_cash == D("10")
    assert review.total_provider_need == D("40")
    assert review.modeled_provider_surplus == D("15")
    assert review.need_after_central_cash == D("30")
    assert review.modeled_shortfall_after_surplus == D("15")
    assert review.requires_transfer_feasibility_check is False


def test_protected_reserve_cannot_be_silently_used() -> None:
    review = review_provider_liquidity(
        central_cash=D("100"),
        protected_reserve=D("90"),
        accounts=[account("book-a", "0", "20")],
    )

    assert review.state is LiquidityReviewState.GLOBAL_SHORTFALL
    assert review.central_deployable_cash == D("10")
    assert review.modeled_shortfall_after_surplus == D("10")


@pytest.mark.parametrize(
    ("central", "reserve"),
    [
        (D("-1"), D("0")),
        (D("NaN"), D("0")),
        (D("Infinity"), D("0")),
        (D("1"), D("-1")),
        (D("1"), D("2")),
    ],
)
def test_invalid_central_money_fails_closed(central: Decimal, reserve: Decimal) -> None:
    with pytest.raises((TypeError, ValueError)):
        review_provider_liquidity(
            central_cash=central,
            protected_reserve=reserve,
            accounts=[],
        )


@pytest.mark.parametrize(
    "bad_account",
    [
        ProviderLiquidityAccount("", D("1"), D("1")),
        ProviderLiquidityAccount("   ", D("1"), D("1")),
        ProviderLiquidityAccount("book", D("-1"), D("1")),
        ProviderLiquidityAccount("book", D("1"), D("-1")),
        ProviderLiquidityAccount("book", D("NaN"), D("1")),
    ],
)
def test_invalid_provider_observations_fail_closed(
    bad_account: ProviderLiquidityAccount,
) -> None:
    with pytest.raises((TypeError, ValueError)):
        review_provider_liquidity(
            central_cash=D("0"),
            protected_reserve=D("0"),
            accounts=[bad_account],
        )


def test_duplicate_provider_identity_fails_closed_after_whitespace_normalization() -> None:
    with pytest.raises(ValueError, match="duplicate provider_id"):
        review_provider_liquidity(
            central_cash=D("0"),
            protected_reserve=D("0"),
            accounts=[
                account("book-a", "1", "1"),
                account(" book-a ", "1", "1"),
            ],
        )


def test_float_money_is_rejected_to_avoid_binary_money_semantics() -> None:
    with pytest.raises(TypeError, match="central_cash must be Decimal"):
        review_provider_liquidity(
            central_cash=1.0,  # type: ignore[arg-type]
            protected_reserve=D("0"),
            accounts=[],
        )
